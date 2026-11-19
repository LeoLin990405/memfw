"""memfw-wiki-gate (packages/memfw-wiki-gate): deterministic checks of wiki pages (W1-2).

Everything is synthetic and offline. ``tests/fixtures/wiki_gate/corpus.json``
holds a made-up job (concept, sources with texts, an open question with a
conflict, approved sentences for the loop check) and a valid base page;
``attacks.json`` holds one-claim attacks migrated from the v2 design's R1-R5
write-gate attacks plus the v3 rules, and controls that must survive. The
headline number is the false-accept count over the attacks: it must be 0.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import random
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_SRC = ROOT / "packages" / "memfw-wiki-gate" / "src"
if str(PKG_SRC) not in sys.path:  # a workspace member; CI installs only memfw[dev]
    sys.path.insert(0, str(PKG_SRC))

from memfw_wiki_gate import (  # noqa: E402
    CHECKER_VERSION,
    CLAIM_REASONS,
    LEXICON,
    PAGE_REASONS,
    TOKEN_KINDS,
    GateOptions,
    GateReport,
    ResolvedSource,
    SourceIndex,
    SourceResolver,
    asserted_causal,
    asserts_certainty,
    check_page,
    cjk_number,
    extract_tokens,
    normalize_text,
    page_sentence_hashes,
    sentence_hash,
)
from memfw_wiki_gate.normalize import mixed_script_word  # noqa: E402

from memfw.ports.testing import check_wiki_page  # noqa: E402
from memfw.ports.wiki import (  # noqa: E402
    WikiConcept,
    WikiJob,
    WikiJobSource,
    WikiOpenQuestion,
    WikiPage,
    WikiSource,
    wiki_job_digest,
)

FIXTURES = ROOT / "tests" / "fixtures" / "wiki_gate"
CORPUS: dict[str, Any] = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))
ATTACKS: dict[str, Any] = json.loads((FIXTURES / "attacks.json").read_text(encoding="utf-8"))
REF = {source["key"]: source["ref"] for source in CORPUS["sources"]}
GENERATOR = {
    "generator": "wikigen",
    "generator_version": "0.1.0",
    "model": "example-model",
    "model_digest": "sha256:" + "a" * 64,
    "prompt_version": "wiki-v1",
}
NLI_MODEL = "example-nli@sha256-" + "b" * 16
SECTIONS = ("summary", "facts", "timeline", "relations", "open")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _job_source(raw: Mapping[str, Any], **override: Any) -> WikiJobSource:
    fields = {k: raw[k] for k in ("ref", "kind", "privacy", "causal", "status")}
    fields["superseded_by"] = raw.get("superseded_by")
    fields["sha256"] = _sha(raw["text"])
    fields.update(override)
    return WikiJobSource(WikiSource(**fields), raw["text"])


def _job(
    *,
    order: list[int] | None = None,
    tenant: str = "",
    previous_page: WikiPage | None = None,
    **overrides: dict[str, Any],
) -> WikiJob:
    raws = CORPUS["sources"]
    indices = order if order is not None else list(range(len(raws)))
    sources = tuple(_job_source(raws[i], **overrides.get(raws[i]["key"], {})) for i in indices)
    concept = WikiConcept(
        CORPUS["concept"]["oid"],
        CORPUS["concept"]["pref_label"],
        tuple(CORPUS["concept"]["alt_labels"]),
        CORPUS["concept"]["definition"],
    )
    questions = tuple(
        WikiOpenQuestion(q["id"], q["question"], q["missing"], tuple(q["conflicts"]))
        for q in CORPUS["open_questions"]
    )
    digest = wiki_job_digest(
        concept=concept,
        tenant=tenant,
        sources=sources,
        open_questions=questions,
        previous_page=previous_page,
        generator=GENERATOR,
        checker_version=CHECKER_VERSION,
    )
    return WikiJob(
        job_id="job-0001",
        job_digest=digest,
        tenant=tenant,
        concept=concept,
        sources=sources,
        generator=GENERATOR,
        checker_version=CHECKER_VERSION,
        view_version="view-20261119",
        open_questions=questions,
        previous_page=previous_page,
    )


def _claim(spec: Mapping[str, Any], *, nli: float | None = None) -> dict[str, Any]:
    checks: dict[str, Any] = {"tokens": "not_run"}
    score = nli  # what the generator reports; the gate never reads it
    if score is not None:
        checks["nli"] = {"min": score}
    return {
        "cid": spec.get("cid", "atk"),
        "text": spec["text"],
        "cites": [REF.get(cite, cite) for cite in spec["cites"]],
        "causal": spec.get("causal", "none"),
        "kind": spec.get("kind", "statement"),
        "epistemic": spec.get("epistemic", "inference"),
        "checks": checks,
    }


def _page(
    job: WikiJob,
    claims: list[Mapping[str, Any]],
    *,
    nli: float | None = None,
    privacy: str | None = None,
) -> dict[str, Any]:
    """A page document answering ``job`` with ``claims`` (specs with a ``section``)."""

    offered = {item.source.ref: item.source for item in job.sources}
    docs = [(spec["section"], _claim(spec, nli=nli)) for spec in claims]
    cited = sorted({ref for _, doc in docs for ref in doc["cites"] if ref in offered})
    levels = [offered[ref].privacy for ref in cited]
    level = "shared" if "shared" in levels else "public"
    return {
        "format": "memfw-wiki-page/1",
        "page_key": job.page_key,
        "concept": job.concept.oid,
        "tenant": job.tenant,
        "privacy": privacy or level,
        "build": {
            **GENERATOR,
            "checker_version": job.checker_version,
            "nli_model": NLI_MODEL,
            "view_version": job.view_version,
            "job_digest": job.job_digest,
        },
        "sources": [offered[ref].to_json() for ref in cited],
        "sections": [
            {"id": section, "claims": [doc for sid, doc in docs if sid == section]}
            for section in SECTIONS
            if any(sid == section for sid, _ in docs)
        ],
        "open_questions": [q["id"] for q in CORPUS["open_questions"]],
        "built_from_digest": "sha256:" + "0" * 64,
    }


def _with(claim: Mapping[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """The base claims plus ``claim`` last in its section, and its page-order index."""

    claims = [dict(c) for c in CORPUS["base_claims"]]
    section = claim["section"]
    position = max(
        (
            i
            for i, c in enumerate(claims)
            if SECTIONS.index(c["section"]) <= SECTIONS.index(section)
        ),
        default=-1,
    )
    claims.insert(position + 1, dict(claim))
    return claims, position + 1


def _options(spec: Mapping[str, Any]) -> tuple[GateOptions, dict[str, float] | None]:
    raw = spec.get("options", {})
    approved = frozenset()
    if raw.get("approved"):
        approved = frozenset(sentence_hash(text) for text in CORPUS["approved_sentences"])
    options = GateOptions(
        approved_sentence_hashes=approved,
        generated_refs=frozenset(REF[key] for key in raw.get("generated", ())),
        require_nli=bool(raw.get("require_nli")),
        nli_allowlist=frozenset({NLI_MODEL}),
        nli_threshold=raw.get("nli_threshold"),
    )
    scores = None
    if raw.get("require_nli"):  # the caller's own checker scored the claims
        scores = {claim["cid"]: 0.95 for claim in CORPUS["base_claims"]}
        own = spec["claim"].get("nli")
        if own is not None:
            scores[spec["claim"].get("cid", "atk")] = own
    return options, scores


def _run_case(spec: Mapping[str, Any]) -> tuple[GateReport, int]:
    job = _job()
    claims, index = _with(spec["claim"])
    options, scores = _options(spec)
    # The generator reports 0.99 everywhere; only the caller's scores count.
    page = _page(job, claims, nli=None if scores is None else 0.99)
    report = check_page(
        page,
        job,
        options=options,
        nli_scores=scores,
        nli_model=None if scores is None else NLI_MODEL,
    )
    return report, index


# --- the base page ---------------------------------------------------------------------


def test_base_page_publishes_as_a_valid_page_for_its_job() -> None:
    job = _job()
    report = check_page(_page(job, CORPUS["base_claims"]), job)
    assert report.verdict == "publish", report.to_json()
    assert report.page_reasons == ()
    assert all(claim.accepted for claim in report.claims)
    assert report.counts["claims_total"] == report.counts["claims_accepted"] == 9
    assert isinstance(report.page, WikiPage)
    assert report.privacy == report.page.privacy == "shared"
    assert check_wiki_page(report.page, job=job) == report.page  # the ports contract
    assert all(claim.tokens == "pass" for claim in report.page.claims)
    assert [s.ref for s in report.page.sources] == sorted(s.ref for s in report.page.sources)
    # The worker's JSON text and the WikiPage object give the same answer.
    again = check_page(json.dumps(_page(job, CORPUS["base_claims"])), job.to_json())
    assert again.to_bytes() == report.to_bytes()
    assert check_page(report.page, job).to_bytes() == report.to_bytes()


# --- attacks: false accepts must be 0 ------------------------------------------------------


@pytest.mark.parametrize("spec", ATTACKS["claim_attacks"], ids=lambda s: s["id"])
def test_attack_claim_is_dropped_with_its_reason(spec: dict[str, Any]) -> None:
    report, index = _run_case(spec)
    verdict = report.claims[index]
    assert not verdict.accepted, spec["id"]
    missing = set(spec["expect"]) - set(verdict.reasons)
    assert not missing, (spec["id"], verdict.reasons)
    # One bad sentence never sinks the page: the base claims still publish.
    assert report.verdict == "publish", (spec["id"], report.page_reasons)
    assert all(c.accepted for i, c in enumerate(report.claims) if i != index), spec["id"]
    assert report.page is not None and spec["claim"]["text"] not in {
        c.text for c in report.page.claims if c.cid == spec["claim"].get("cid", "atk")
    }


def test_false_accept_count_over_all_attacks_is_zero() -> None:
    accepted = []
    rules: set[str] = set()
    for spec in ATTACKS["claim_attacks"]:
        report, index = _run_case(spec)
        rules.add(spec["rule"])
        if report.claims[index].accepted:
            accepted.append(spec["id"])
    assert accepted == [], accepted
    assert {"R1", "R2", "R3", "R4", "R5", "G6", "2.3", "check1", "nli", "format"} <= rules
    assert len(ATTACKS["claim_attacks"]) >= 70


@pytest.mark.parametrize("spec", ATTACKS["claim_controls"], ids=lambda s: s["id"])
def test_control_claim_is_kept(spec: dict[str, Any]) -> None:
    report, index = _run_case(spec)
    assert report.claims[index].accepted, (spec["id"], report.claims[index].reasons)
    assert report.verdict == "publish", report.page_reasons
    assert report.page is not None
    check_wiki_page(report.page, job=_job())


# --- page-level outcomes ------------------------------------------------------------------


class FakeView:
    """An admitted-view reader over the job, with per-ref changes."""

    def __init__(self, job: WikiJob, changes: Mapping[str, Any] | None = None) -> None:
        self._items = {item.source.ref: item for item in job.sources}
        self._tenant = job.tenant
        self._changes = dict(changes or {})

    def resolve(self, ref: str) -> ResolvedSource | None:
        if ref in self._changes:
            change = self._changes[ref]
            if isinstance(change, Exception):
                raise change
            return change  # type: ignore[no-any-return]
        item = self._items.get(ref)
        if item is None:
            return None
        return ResolvedSource(item.source, item.text, self._tenant)


def _view_change(**fields: Any) -> ResolvedSource:
    raw = next(s for s in CORPUS["sources"] if s["key"] == "nightly")
    item = _job_source(raw)
    source = replace(item.source, **{k: v for k, v in fields.items() if k != "tenant"})
    return ResolvedSource(source, raw["text"], fields.get("tenant", ""))


def test_fake_view_is_a_source_resolver() -> None:
    assert isinstance(FakeView(_job()), SourceResolver)


@pytest.mark.parametrize(
    ("change", "verdict", "reason"),
    [
        (None, "retry", "source_missing"),
        (OSError("view down"), "retry", "view_unavailable"),
        ("not a record", "retry", "view_unavailable"),
        (_view_change(sha256="c" * 64), "retry", "source_changed"),
        (_view_change(causal="association"), "retry", "source_changed"),
        (_view_change(status="retired"), "retry", "source_changed"),
        (_view_change(privacy="local-private"), "reject", "privacy_not_releasable"),
        (_view_change(tenant="team-b"), "reject", "tenant_mismatch"),
    ],
    ids=["missing", "raises", "garbage", "hash", "causal", "status", "private", "tenant"],
)
def test_view_changes_publish_nothing(change: Any, verdict: str, reason: str) -> None:
    job = _job()
    view = FakeView(job, {REF["nightly"]: change})
    report = check_page(_page(job, CORPUS["base_claims"]), job, view=view)
    assert report.verdict == verdict and reason in report.page_reasons, report.page_reasons
    assert report.page is None and not report.publishable
    assert not any(claim.accepted for claim in report.claims)


def test_unchanged_view_publishes_like_the_job() -> None:
    job = _job()
    page = _page(job, CORPUS["base_claims"])
    assert check_page(page, job, view=FakeView(job)).to_bytes() == check_page(page, job).to_bytes()


def test_view_marks_a_record_generated() -> None:
    job = _job()
    item = next(i for i in job.sources if i.source.ref == REF["retention"])
    view = FakeView(job, {item.source.ref: ResolvedSource(item.source, item.text, "", True)})
    report = check_page(_page(job, CORPUS["base_claims"]), job, view=view)
    dropped = [c for c in report.claims if not c.accepted]
    assert {c.cid for c in dropped} == {"c3", "c6"}
    assert all("generated_source" in c.reasons for c in dropped)


def _mutated(mutate: Any) -> GateReport:
    job = _job()
    doc = _page(job, CORPUS["base_claims"])
    mutate(doc)
    return check_page(doc, job)


@pytest.mark.parametrize(
    ("name", "mutate", "reason"),
    [
        ("format", lambda d: d.update(format="memfw-wiki-page/2"), "page_format"),
        ("extra key", lambda d: d.update(vault_path="x"), "page_format"),
        ("concept", lambda d: d.update(concept="concept:example:other"), "job_mismatch"),
        ("tenant", lambda d: d.update(tenant="team-b"), "job_mismatch"),
        (
            "job digest",
            lambda d: d["build"].update(job_digest="sha256:" + "d" * 64),
            "job_mismatch",
        ),
        ("model", lambda d: d["build"].update(model="other-model"), "job_mismatch"),
        ("view version", lambda d: d["build"].update(view_version="view-x"), "job_mismatch"),
        (
            "source privacy",
            lambda d: d["sources"][0].update(privacy="local-private"),
            "source_not_in_job",
        ),
        ("source hash", lambda d: d["sources"][0].update(sha256="e" * 64), "source_not_in_job"),
        ("source extra", lambda d: d["sources"][0].update(text="x"), "source_not_in_job"),
        ("section twice", lambda d: d["sections"].append(d["sections"][0]), "page_format"),
        ("section order", lambda d: d["sections"].reverse(), "page_format"),
        ("unknown section", lambda d: d["sections"][0].update(id="intro"), "page_format"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_envelope_refusals(name: str, mutate: Any, reason: str) -> None:
    report = _mutated(mutate)
    assert report.verdict == "reject" and report.page_reasons == (reason,), (name, report)
    assert report.page is None and report.claims == ()


@pytest.mark.parametrize(
    "raw",
    [b"\xff", "not json", "[]", '{"format": "memfw-wiki-page/1"}', "{" * 5000, '{"a": NaN}', 7],
)
def test_garbage_page_is_a_reject_not_an_exception(raw: Any) -> None:
    report = check_page(raw, _job())
    assert report.verdict == "reject" and report.page_reasons == ("page_format",)


def test_duplicate_json_keys_are_refused() -> None:
    job = _job()
    text = json.dumps(_page(job, CORPUS["base_claims"]))
    text = text.replace('"tenant": ""', '"tenant": "", "tenant": ""', 1)
    assert check_page(text, job).page_reasons == ("page_format",)


def test_invalid_job_is_a_reject() -> None:
    job = _job()
    doc = job.to_json()
    doc["sources"][0]["privacy"] = "local-private"  # a job never carries a private source
    report = check_page(_page(job, CORPUS["base_claims"]), doc)
    assert report.verdict == "reject" and report.page_reasons == ("job_invalid",)
    assert check_page(_page(job, CORPUS["base_claims"]), "{}").page_reasons == ("job_invalid",)


def test_generator_privacy_is_ignored_and_recomputed() -> None:
    job = _job()
    report = check_page(_page(job, CORPUS["base_claims"], privacy="public"), job)
    assert report.verdict == "publish"
    assert report.page_reasons == ("privacy_recomputed",)
    assert report.privacy == report.page.privacy == "shared"  # type: ignore[union-attr]


def test_summary_without_survivors_is_no_page() -> None:
    claims = [dict(c) for c in CORPUS["base_claims"]]
    claims[0]["text"] = "Backups run nightly at 05:00."  # unsupported number
    job = _job()
    report = check_page(_page(job, claims), job)
    assert report.verdict == "reject" and "summary_empty" in report.page_reasons
    assert report.page is None


def test_section_losing_more_than_half_is_withheld() -> None:
    claims = [dict(c) for c in CORPUS["base_claims"]]
    claims.insert(
        3, {"cid": "x1", "section": "facts", "text": "Backups keep 90 days.", "cites": ["nightly"]}
    )
    claims.insert(
        4, {"cid": "x2", "section": "facts", "text": "Backups keep 91 days.", "cites": ["nightly"]}
    )
    claims.insert(
        5, {"cid": "x3", "section": "facts", "text": "Backups keep 92 days.", "cites": ["nightly"]}
    )
    job = _job()
    report = check_page(_page(job, claims), job)
    assert report.verdict == "needs_attention" and report.page_reasons == ("section_withheld",)
    by_cid = {c.cid: c for c in report.claims}
    assert by_cid["c2"].reasons == ("section_withheld",) and not by_cid["c2"].accepted
    assert by_cid["c3"].reasons == ("section_withheld",)
    assert report.counts["claims_withheld"] == 2 and report.counts["claims_dropped"] == 3
    assert report.page is not None and "facts" not in report.page.section_ids()
    check_wiki_page(report.page, job=job)


def test_generator_open_questions_are_ignored_and_regenerated() -> None:
    job = _job()
    doc = _page(job, CORPUS["base_claims"])
    expected = tuple(sorted(q["id"] for q in CORPUS["open_questions"]))
    for value in (["oq-20261199-x"], [], "garbage", None):
        doc["open_questions"] = value
        report = check_page(doc, job)
        assert report.verdict == "publish", (value, report.page_reasons)
        assert report.page is not None and report.page.open_questions == expected
        check_wiki_page(report.page, job=job)


def test_nli_scores_come_from_the_caller_only() -> None:
    job = _job()
    page = _page(job, CORPUS["base_claims"], nli=0.99)  # what the generator claims
    scores = {claim["cid"]: 0.9 for claim in CORPUS["base_claims"]}
    options = GateOptions(require_nli=True, nli_allowlist={NLI_MODEL})
    ok = check_page(page, job, options=options, nli_scores=scores, nli_model=NLI_MODEL)
    assert ok.verdict == "publish" and ok.page is not None
    assert all(c.nli_min == 0.9 for c in ok.page.claims)
    assert ok.page.build.nli_model == NLI_MODEL
    # The generator's 0.99 is not a score: without the caller's, every claim misses one.
    missing = check_page(page, job, options=options, nli_model=NLI_MODEL)
    assert all("nli_missing" in c.reasons for c in missing.claims)
    assert missing.verdict == "reject" and missing.page is None
    # The threshold applies to the caller's score, not the generator's.
    strict = GateOptions(require_nli=True, nli_allowlist={NLI_MODEL}, nli_threshold=0.95)
    low = check_page(page, job, options=strict, nli_scores=scores, nli_model=NLI_MODEL)
    assert all("nli_below_threshold" in c.reasons for c in low.claims)
    # The model is the caller's checker, not build.nli_model (which is allowlisted here).
    for model in ("other@x", None):
        bad = check_page(page, job, options=options, nli_scores=scores, nli_model=model)
        assert bad.verdict == "reject" and "nli_model_not_allowlisted" in bad.page_reasons
    # Without caller scores the published page carries no NLI score at all.
    plain = check_page(page, job)
    assert plain.verdict == "publish" and plain.page is not None
    assert all(c.nli_min is None for c in plain.page.claims)
    for bad_scores in ({"c1": 1.5}, {"c1": float("nan")}, {"c1": "high"}, [("c1", 0.9)]):
        with pytest.raises(ValueError):
            check_page(page, job, nli_scores=bad_scores)  # type: ignore[arg-type]


def test_options_are_validated() -> None:
    for bad in (
        {"nli_threshold": 1.5},
        {"nli_threshold": float("nan")},
        {"loop_min_chars": 0},
        {"report_hash_key": b"short"},
        {"report_hash_key": "a string key, not bytes"},
    ):
        with pytest.raises(ValueError):
            GateOptions(**bad)  # type: ignore[arg-type]


# --- privacy matrix ----------------------------------------------------------------------


def _levels(default: str = "public", **levels: str) -> dict[str, dict[str, str]]:
    """Privacy overrides for every corpus source: ``default``, except ``levels``."""

    return {s["key"]: {"privacy": levels.get(s["key"], default)} for s in CORPUS["sources"]}


@pytest.mark.parametrize(
    ("levels", "expected"),
    [
        (("public",), "public"),
        (("shared",), "shared"),
        (("public", "public"), "public"),
        (("public", "shared"), "shared"),
        (("shared", "public"), "shared"),
        (("shared", "shared"), "shared"),
    ],
)
def test_page_privacy_is_the_strictest_job_source(levels: tuple[str, ...], expected: str) -> None:
    job = _job(**_levels(nightly=levels[0], time_b=levels[-1]))
    claims = [
        CORPUS["base_claims"][0],  # summary, cites nightly
        {
            "cid": "c2",
            "section": "open",
            "kind": "missing_evidence",
            "text": "A run log is missing.",
            "cites": ["time_b"],
        },
    ]
    report = check_page(_page(job, claims), job)
    assert report.verdict == "publish", report.to_json()
    assert report.privacy == report.page.privacy == expected  # type: ignore[union-attr]


def test_privacy_counts_every_job_source_not_only_surviving_claims() -> None:
    job = _job(**_levels(nightly="shared"))
    claims = [
        {"cid": "c1", "section": "summary", "text": "Backups keep 99 days.", "cites": ["nightly"]},
        {"cid": "c2", "section": "summary", "text": "备份保留 30 天。", "cites": ["retention"]},
    ]
    report = check_page(_page(job, claims, privacy="public"), job)
    assert [c.accepted for c in report.claims] == [False, True]
    assert report.verdict == "publish"
    # The dropped claim's shared source still counts (rule shared with the
    # generator, W1-4): the page lists every job source and is shared.
    assert report.privacy == "shared" and report.page_reasons == ("privacy_recomputed",)
    assert report.page is not None
    assert [s.ref for s in report.page.sources] == sorted(i.source.ref for i in job.sources)
    assert report.counts["sources_cited"] == 1
    check_wiki_page(report.page, job=job)


def test_a_shared_uncited_source_makes_the_page_shared() -> None:
    job = _job(**_levels(screenshot="shared"))
    claims = [
        {"cid": "c1", "section": "summary", "text": "备份保留 30 天。", "cites": ["retention"]}
    ]
    report = check_page(_page(job, claims, privacy="public"), job)
    assert report.verdict == "publish", report.page_reasons
    assert report.privacy == report.page.privacy == "shared"  # type: ignore[union-attr]
    assert "privacy_recomputed" in report.page_reasons
    assert REF["screenshot"] in {s.ref for s in report.page.sources}  # type: ignore[union-attr]


def test_a_previous_page_stricter_than_the_job_is_a_reject() -> None:
    shared = check_page(_page(_job(), CORPUS["base_claims"]), _job())
    assert shared.page is not None and shared.page.privacy == "shared"
    job = _job(previous_page=shared.page, **_levels())  # every source public now
    report = check_page(_page(job, CORPUS["base_claims"]), job)
    assert report.verdict == "reject" and "previous_page_stricter" in report.page_reasons
    assert report.page is None
    # A previous page as strict as the job's sources (or looser) is fine.
    public = check_page(_page(_job(**_levels()), CORPUS["base_claims"]), _job(**_levels()))
    assert public.page is not None and public.page.privacy == "public"
    for previous, levels in ((public.page, _levels()), (shared.page, {}), (public.page, {})):
        again = _job(previous_page=previous, **levels)
        assert check_page(_page(again, CORPUS["base_claims"]), again).verdict == "publish"


def test_privacy_matrix_through_the_view() -> None:
    job = _job()
    page = _page(job, CORPUS["base_claims"])
    outcomes = {}
    for level in ("public", "shared", "local-private"):
        view = FakeView(job, {REF["retention"]: _retention(privacy=level)})
        outcomes[level] = check_page(page, job, view=view)
    assert outcomes["public"].verdict == "publish"
    assert outcomes["shared"].verdict == "retry"  # the view's record differs from the job's
    assert outcomes["local-private"].verdict == "reject"
    assert "privacy_not_releasable" in outcomes["local-private"].page_reasons
    for report in outcomes.values():
        assert report.privacy in (None, "public", "shared")


def _retention(**fields: Any) -> ResolvedSource:
    raw = next(s for s in CORPUS["sources"] if s["key"] == "retention")
    item = _job_source(raw)
    return ResolvedSource(replace(item.source, **fields), raw["text"], "")


def test_a_job_with_an_unreleasable_source_cannot_be_built() -> None:
    raw = next(s for s in CORPUS["sources"] if s["key"] == "nightly")
    for level in ("local-private", "unknown"):
        with pytest.raises(ValueError):
            _job_source(raw, privacy=level)


# --- tokens: multilingual -----------------------------------------------------------------


def _kinds(text: str) -> list[tuple[str, str]]:
    return [(t.kind, t.value) for t in extract_tokens(text)]


def test_token_extraction_examples() -> None:
    assert _kinds("Upgraded to v0.4.0 on 2026-11-19; see /srv/x and docs/a.md.") == [
        ("date", "2026-11-19"),
        ("path", "/srv/x"),
        ("path", "docs/a.md"),
        ("version", "0.4.0"),
    ]
    assert _kinds("PR #42 merged 9c3e7d1 for W1-2 on H7.") == [
        ("id", "#42"),
        ("id", "9c3e7d1"),
        ("id", "w1-2"),
        ("id", "h7"),
        ("name", "pr"),
    ]
    assert _kinds("三台主机里有两台在线，共十二个任务。") == [
        ("number", "3"),
        ("number", "2"),
        ("number", "12"),
    ]
    assert _kinds("统一一致的方案。") == []  # bare 一 is not a quantity
    assert _kinds("备份保留 ３０ 天，比例 1.50，共 1,000 条。") == [
        ("number", "30"),
        ("number", "1.5"),
        ("number", "1000"),
    ]
    assert _kinds("Nov 19, 2026 and 19 November 2026") == [
        ("date", "2026-11-19"),
        ("date", "2026-11-19"),
    ]
    assert _kinds("11月19日 和 2026年11月") == [("date", "2026-11-?"), ("date", "?-11-19")]
    assert _kinds("Read-only mode stays on.") == []  # hyphenated words are not ids
    assert _kinds("The Qdrant index uses OpenAI embeddings on the NAS.") == [
        ("name", "qdrant"),
        ("name", "openai"),
        ("name", "nas"),
    ]


@pytest.mark.parametrize(
    ("run", "value"),
    [
        ("一", 1),
        ("两", 2),
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("三十一", 31),
        ("一百零五", 105),
        ("三百", 300),
        ("一千二百三十四", 1234),
        ("一万二千", 12000),
        ("二〇二六", 2026),
        ("叁", 3),
        ("壹佰零伍", 105),
        ("贰拾", 20),
        ("仟", 1000),
        ("万", None),
        ("二二十", None),
    ],
)
def test_cjk_numerals(run: str, value: int | None) -> None:
    assert cjk_number(run) == value


@pytest.mark.parametrize(
    ("claim", "source", "supported"),
    [
        ("3 台主机", "三台主机", True),
        ("三台主机", "3 台主机", True),
        ("十二个任务", "12 tasks", True),
        ("12 tasks", "共十二个任务", True),
        ("４台", "4 台", True),
        ("4 台", "四台", True),
        ("5 台", "四台", False),
        ("v0.4.0", "version 0.4.0", True),
        ("0.4.0", "v0.4.0", True),
        ("v3.13", "Python 3.13", True),
        ("0.4.1", "0.4.0", False),
        ("2026-11-19", "2026年11月19日", True),
        ("11月19日", "2026-11-19", True),
        ("2026-11-19", "11月19日", False),  # the claim names a year the source does not
        ("Nov 19", "2026/11/19", True),
        ("H7", "the H7 host", True),
        ("H7", "the H70 host", False),
        ("mem-1", "mem-12", False),
        ("uses Qdrant", "the qdrant index", True),
        ("uses Qdrant", "the qdrantx index", False),
        ("at 02:00", "runs at 2:00", True),
        ("at 04:00", "restic 0.16.4 at 02:00", False),
        ("4 snapshots", "restic 0.16.4", False),
        # A date's parts back only numbers with a date or time unit.
        ("11 snapshots", "on 2026-11-01", False),
        ("11 月", "on 2026-11-01", True),
        ("in 2026", "on 2026-11-01", False),
        ("the year 2026", "on 2026-11-01", True),
        ("2026 年", "on 2026-11-01", True),
        ("at 3 am", "starts at 03:00", True),
        # Every Unicode decimal digit reads as its ASCII digit (both sides).
        ("\u0664\u0660 days", "keep 40 days", True),
        ("\u096a\u0966 days", "keep 40 days", True),
        ("\u0664\u0660 days", "keep 30 days", False),
        ("40 days", "keep \u0664\u0660 days", True),
        # Units compare when both sides name one; a minus is part of a number.
        ("30 天", "30 hours", False),
        ("30 天", "keep 30 days", True),
        ("30 days", "保留 30 天", True),
        ("30 days", "30 snapshots", False),  # a unit is not backed by a unitless number
        ("30 snapshots", "30 days", True),  # a unitless number is backed by any unit
        ("95%", "95 percent", True),
        ("95%", "95 hosts and 5%", False),
        ("1e3 rows", "1000 rows", True),
        ("1e3 rows", "1 row and 3 more", False),
        ("2.5E-4", "0.00025", True),
        ("-5 °C", "5 °C", False),
        ("-5 °C", "低至 \u22125 °C", True),
        # CJK numerals: whitespace before the counter, financial forms, sentence end.
        ("三 台", "3 台", True),
        ("肆台", "4 台", True),
        ("4 台", "肆台", True),
        ("数量是四。", "共 4 台", True),
        ("数量是四。", "共 3 台", False),
        # Paths and quotes match at boundaries only.
        ("/srv/a", "files in /srv/a/b", True),
        ("/srv/backup", "writes to /srv/backup/restic.", True),
        ("/srv/backup/rest", "writes to /srv/backup/restic.", False),
        ("/srv/back", "writes to /srv/backup/restic.", False),
        ("docs/a.md", "see docs/a.mdx", False),
        ("docs/a.md", "see docs/a.md.", True),
        ("/srv/x", "in /opt/srv/x", False),
        ("srv/backup/restic", "writes to /srv/backup/restic", False),
        ("srv/backup/restic", "writes to srv/backup/restic now", True),
        ("3e1 days", "keep 30 days", True),
        ('"keep it"', "they said keep it", True),
        ('"keep it"', "they said keep items", False),
        ('"30 day"', "keep 30 days", False),
        # A quote never cuts a numeral, and its own numbers are checked.
        ("「十天」", "备份保留三十天。", False),
        ("「备份保留三」", "备份保留三十天。", False),
        ("「三十天」", "备份保留三十天。", True),
        ('"0 days"', "keep 30 days", False),
        ('"keep 30 days"', "they keep 30 days", True),
        ('"keep 3 days"', "they keep 30 days", False),
        ("「保留三十天」", "备份保留三十天。", True),
    ],
)
def test_token_support_across_scripts(claim: str, source: str, supported: bool) -> None:
    tokens = extract_tokens(claim)
    assert tokens, claim
    index = SourceIndex.build(source)
    assert all(index.supports(t) for t in tokens) is supported, (claim, source, tokens)


# --- causal lexicon -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "grade"),
    [
        ("备份每晚运行。", "none"),
        ("磁盘写满导致备份失败。", "verified"),
        ("磁盘写满可能导致备份失败。", "hypothesis"),
        ("由于磁盘满了，备份失败。", "verified"),
        ("因为磁盘满了，所以备份失败。", "verified"),
        ("备份失败或许是因为磁盘满了。", "hypothesis"),
        ("已证实磁盘写满导致备份失败。", "verified"),
        ("磁盘使用率与快照数量呈正相关。", "association"),
        ("与备份相关的配置放在这里。", "none"),
        ("这些关联不是因果证明。", "none"),
        # Known limit: one hedge covers the whole sentence (docs/wiki-gate.md section 4).
        ("因为磁盘可能写满，所以备份失败。", "hypothesis"),
        ("The disk filling up caused the failure.", "verified"),
        ("The disk filling up may have caused the failure.", "hypothesis"),
        ("The failure is probably due to a full disk.", "hypothesis"),
        ("The failure was definitely caused by a full disk, it may seem.", "verified"),
        ("A full disk leads to failures.", "verified"),
        ("Snapshot count is correlated with disk usage.", "association"),
        ("The account associated with the job.", "none"),
        ("Becausewe is not a word.", "none"),
        ("Therefore the job moved.", "verified"),
        ("THE DISK CAUSES FAILURES.", "verified"),
        # Invisible or combining characters and spaces never split a marker.
        ("磁盘写满导\u200d致备份失败。", "verified"),
        ("磁盘写满导\u200c致备份失败。", "verified"),
        ("磁盘写满导 致备份失败。", "verified"),
        ("磁盘写满导\u0301致备份失败。", "verified"),
        ("磁盘写满导\ufe0f致备份失败。", "verified"),
        ("The disk ca\u0301used the failure.", "verified"),
        # Traditional forms and the added markers.
        ("磁盘写满導致备份失败。", "verified"),
        ("磁盘写满引發备份失败。", "verified"),
        ("部署觸發了失败。", "verified"),
        ("部署触发了失败。", "verified"),
        ("因為磁盘满了，備份失败。", "verified"),
        ("磁盘写满可能導致备份失败。", "hypothesis"),
        ("The deploy triggered the failure.", "verified"),
        ("A deploy may trigger the failure.", "hypothesis"),
        ("The disk was full, so the job failed.", "verified"),
        ("The disk was full; thus the job failed.", "verified"),
        ("Hence the job moved.", "verified"),
        ("The job is so slow.", "none"),
        # Negated modals and "unlikely" are not hedges; "May" the month is not one.
        ("The disk could not have caused it.", "verified"),
        ("The disk may not cause it.", "verified"),
        ("The disk cannot cause it.", "verified"),
        ("The disk couldn't cause it.", "verified"),
        ("A full disk is unlikely to cause it.", "verified"),
        ("磁盘写满不可能导致备份失败。", "verified"),
        ("磁盘写满不太可能导致备份失败。", "verified"),
        ("In May the disk caused it.", "verified"),
        ("On May 3 the disk caused it.", "verified"),
        ("May 2026: the disk caused it.", "verified"),
        ("May have caused it, a full disk.", "hypothesis"),
        ("The disk could have caused it.", "hypothesis"),
        ("The failure is likely due to the disk.", "hypothesis"),
        ("The cmay field causes nothing new.", "verified"),
        ("The failure stems from a full disk.", "verified"),
        ("The disk was responsible for the failure.", "verified"),
        ("As a consequence the job failed.", "verified"),
        ("备份失败为磁盘写满所致。", "verified"),
        ("The failure may stem from a full disk.", "hypothesis"),
    ],
)
def test_asserted_causal(text: str, grade: str) -> None:
    assert asserted_causal(text) == grade


def test_certainty_markers() -> None:
    assert asserts_certainty("It is proven that backups run.")
    assert asserts_certainty("已证实备份每晚运行。")
    assert not asserts_certainty("The test verified the restore.")  # 验证/verified stay allowed
    assert not asserts_certainty("测试验证了恢复流程。")


def test_mixed_script_words_are_found() -> None:
    assert mixed_script_word("The disk c\u0430uses it.")  # Cyrillic a
    assert mixed_script_word("The disk \u03bfverflowed.")  # Greek omicron
    assert not mixed_script_word("The disk causes it.")
    assert not mixed_script_word("使用Qdrant索引，ΑΒΓ 与 АБВ 分开写。")


def test_lexicon_has_both_languages_for_every_group() -> None:
    for group, entries in LEXICON.items():
        assert entries["zh"] and entries["en"], group
        assert len(set(entries["zh"])) == len(entries["zh"]), group
        assert len(set(entries["en"])) == len(entries["en"]), group


# --- hashes, determinism, no echo ---------------------------------------------------------


def test_sentence_hash_normalises() -> None:
    base = sentence_hash("Backups run nightly.")
    assert base == sentence_hash("  backups   RUN nightly ")
    assert base == sentence_hash('"Backups run nightly!"')
    assert base == sentence_hash("Ｂａｃｋｕｐｓ run nightly。")
    assert base != sentence_hash("Backups run weekly.")
    assert sentence_hash("备份每晚运行。") == sentence_hash("备份每晚运行")
    assert sentence_hash("备份每晚运行。") != sentence_hash("备份每周运行。")
    assert base.startswith("sha256:") and len(base) == 71
    assert normalize_text(" A　b\n") == "a b"


def test_page_sentence_hashes_cover_whole_claims_and_their_sentences() -> None:
    job = _job()
    report = check_page(_page(job, CORPUS["base_claims"]), job)
    assert report.page is not None
    hashes = page_sentence_hashes([report.page])
    assert all(sentence_hash(c.text) in hashes for c in report.page.claims)
    # A source that repeats an approved sentence is a loop (G4).
    claims, index = _with({"section": "facts", "text": "Backups run nightly.", "cites": ["echo"]})
    loop = check_page(
        _page(job, claims),
        job,
        options=GateOptions(approved_sentence_hashes=page_sentence_hashes([_approved(job)])),
    )
    assert "loop_source" in loop.claims[index].reasons


def _approved(job: WikiJob) -> WikiPage:
    spec = {
        "cid": "a1",
        "section": "summary",
        "text": CORPUS["approved_sentences"][0],
        "cites": ["echo"],
    }
    report = check_page(_page(job, [spec]), job)
    assert report.page is not None
    return report.page


def test_previous_page_sentences_count_for_the_loop_check() -> None:
    # The previous page says, citing echo_zh, a sentence the echo source repeats.
    spec = {
        "cid": "a1",
        "section": "summary",
        "text": CORPUS["approved_sentences"][0],
        "cites": ["echo_zh"],
    }
    previous = check_page(_page(_job(), [spec]), _job()).page
    assert previous is not None
    job = _job(previous_page=previous)
    claims, index = _with({"section": "facts", "text": "Backups run nightly.", "cites": ["echo"]})
    report = check_page(_page(job, claims), job)  # no approved hashes passed
    assert "loop_source" in report.claims[index].reasons
    assert check_page(_page(_job(), claims), _job()).claims[index].accepted
    # The source a previous sentence was taken from (it cites echo) is not its echo,
    # and the base page, which copies its sources' sentences, regenerates.
    job = _job(previous_page=_approved(_job()))
    assert check_page(_page(job, claims), job).claims[index].accepted
    # Passing that same page in approved_sentence_hashes does not cancel the exemption.
    approved = GateOptions(approved_sentence_hashes=page_sentence_hashes([_approved(_job())]))
    assert check_page(_page(job, claims), job, options=approved).claims[index].accepted
    # An approved page that is not the previous one still counts in full.
    assert not check_page(_page(_job(), claims), _job(), options=approved).claims[index].accepted
    base = check_page(_page(_job(), CORPUS["base_claims"]), _job()).page
    again = _job(previous_page=base)
    assert check_page(_page(again, CORPUS["base_claims"]), again).verdict == "publish"


def test_report_sentence_hashes_are_keyed_or_absent() -> None:
    job = _job()
    page = _page(job, CORPUS["base_claims"])
    plain = check_page(page, job)
    assert all(c.sentence_hash is None for c in plain.claims)
    assert all(c.cid for c in plain.claims) and plain.counts["claims_total"] == 9
    key_a, key_b = b"a" * 32, b"b" * 32
    first = check_page(page, job, options=GateOptions(report_hash_key=key_a))
    again = check_page(page, job, options=GateOptions(report_hash_key=key_a))
    other = check_page(page, job, options=GateOptions(report_hash_key=key_b))
    hashes = [c.sentence_hash for c in first.claims]
    assert all(h is not None and h.startswith("hmac-sha256:") for h in hashes)
    assert hashes == [c.sentence_hash for c in again.claims]
    assert not set(hashes) & {c.sentence_hash for c in other.claims}
    unkeyed = {sentence_hash(c["text"]).removeprefix("sha256:") for c in CORPUS["base_claims"]}
    assert not {h.removeprefix("hmac-sha256:") for h in hashes if h} & unkeyed


def test_an_uncited_source_changed_in_the_view_publishes_nothing() -> None:
    job = _job()
    view = FakeView(job, {REF["screenshot"]: None})  # cited by no base claim
    report = check_page(_page(job, CORPUS["base_claims"]), job, view=view)
    assert report.verdict == "retry" and "source_missing" in report.page_reasons
    assert report.page is None


def test_report_is_deterministic_and_independent_of_job_order() -> None:
    first = check_page(_page(_job(), CORPUS["base_claims"]), _job())
    order = list(range(len(CORPUS["sources"])))
    random.Random(7).shuffle(order)
    shuffled_job = _job(order=order)
    assert shuffled_job.job_digest == _job().job_digest
    second = check_page(_page(shuffled_job, CORPUS["base_claims"]), shuffled_job)
    assert first.to_bytes() == second.to_bytes()
    assert first.page is not None and second.page is not None
    assert first.page.page_digest() == second.page.page_digest()


def test_report_bytes_do_not_depend_on_the_hash_seed(tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text(
        "import sys\n"
        f"sys.path[:0] = [{str(ROOT)!r}, {str(PKG_SRC)!r}]\n"
        "from tests.test_wiki_gate import ATTACKS, _run_case\n"
        "out = b''.join(_run_case(s)[0].to_bytes() for s in ATTACKS['claim_attacks'])\n"
        "import hashlib; print(hashlib.sha256(out).hexdigest())\n",
        encoding="utf-8",
    )
    digests = set()
    for seed in ("1", "2", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
        )
        assert done.returncode == 0, done.stderr
        digests.add(done.stdout.strip())
    assert len(digests) == 1


def _texts() -> list[str]:
    out = [s["text"] for s in CORPUS["sources"]] + [s["ref"] for s in CORPUS["sources"]]
    out += [c["text"] for c in CORPUS["base_claims"]]
    for group in ("claim_attacks", "claim_controls"):
        for spec in ATTACKS[group]:
            out.append(spec["claim"]["text"])
            out += [c for c in spec["claim"]["cites"] if c not in REF]
    return [t for t in out if len(t) >= 4]


def test_reports_never_echo_text_refs_or_tokens() -> None:
    needles = _texts()
    words = {w for t in needles for w in t.split() if len(w) >= 6 and not w.isdigit()}
    for group in ("claim_attacks", "claim_controls"):
        for spec in ATTACKS[group]:
            report, _ = _run_case(spec)
            data = json.dumps(report.to_json(), ensure_ascii=False)
            assert not [n for n in needles if n in data], spec["id"]
            assert not [w for w in words if w in data and w not in _ALLOWED_WORDS], spec["id"]


#: Words that appear in a report as reason codes or field names, not as page text.
_ALLOWED_WORDS = frozenset(
    {"accepted", "claims", "counts", "format", "privacy", "reasons", "section", "summary"}
)


def test_only_fixed_reason_codes_are_reported() -> None:
    seen: set[str] = set()
    for group in ("claim_attacks", "claim_controls"):
        for spec in ATTACKS[group]:
            report, _ = _run_case(spec)
            seen |= {code for claim in report.claims for code in claim.reasons}
            assert set(report.page_reasons) <= set(PAGE_REASONS)
            assert set(report.counts["dropped_by_reason"]) <= set(CLAIM_REASONS)
    assert seen <= set(CLAIM_REASONS)
    assert {f"token_unsupported_{k}" for k in TOKEN_KINDS} <= seen  # every token kind exercised


def test_random_mutations_never_raise() -> None:
    job = _job()
    base = _page(job, CORPUS["base_claims"])
    rng = random.Random(20261119)
    junk: list[Any] = [None, 1, -1, 1.5, "", "x", [], {}, True, ["mem:x"], {"a": 1}]
    for _ in range(400):
        doc = copy.deepcopy(base)
        target: Any = doc
        path = []
        while isinstance(target, (dict, list)) and target and rng.random() < 0.7:
            key = (
                rng.choice(list(target)) if isinstance(target, dict) else rng.randrange(len(target))
            )
            path.append(key)
            if not isinstance(target[key], (dict, list)):
                break
            target = target[key]
        if path:
            parent: Any = doc
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = rng.choice(junk)
        report = check_page(doc, job)
        assert report.verdict in ("publish", "needs_attention", "retry", "reject")
        if report.page is not None:
            check_wiki_page(report.page, job=job)


# --- package boundaries ------------------------------------------------------------------


def test_package_imports_only_stdlib_and_the_ports() -> None:
    allowed = set(sys.stdlib_module_names) | {"memfw_wiki_gate", "memfw_ports"}
    for path in sorted((PKG_SRC / "memfw_wiki_gate").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                ok = name.split(".")[0] in allowed or name.startswith("memfw.ports.")
                assert ok, (path.name, name)


def test_only_the_seam_names_the_ports_and_the_pin_matches() -> None:
    import tomllib

    for path in sorted((PKG_SRC / "memfw_wiki_gate").glob("*.py")):
        if path.name == "_ports.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert not node.module.startswith(("memfw.", "memfw_")), (path.name, node.module)
    package = ROOT / "packages"
    data = tomllib.loads((package / "memfw-wiki-gate" / "pyproject.toml").read_text("utf-8"))
    ports = tomllib.loads((package / "memfw-ports" / "pyproject.toml").read_text("utf-8"))
    assert data["project"]["dependencies"] == [f"memfw-ports=={ports['project']['version']}"]
    from memfw_wiki_gate import __version__

    assert data["project"]["version"] == __version__
    assert CHECKER_VERSION == f"wiki-gate-{__version__}"
