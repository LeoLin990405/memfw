"""The gate: check a generated page claim by claim, against its job (and view).

:func:`check_page` never raises for bad input and never runs a model. It reads
the page envelope (format, concept, tenant, build fields and listed sources
must be the job's; the generator's open questions are ignored), then judges
every claim on its own:

1. **format** — no ``Cf`` character (ZWJ and ZWNJ included) and no word
   mixing Latin with Cyrillic or Greek letters; the claim must build as a
   ports ``WikiClaim`` (cited,
   canonical refs, no ``wiki:`` / ``mem:wiki-`` / ``file:`` / URL citation,
   versioned memories, epistemic ``inference``, causal label at most
   ``hypothesis`` and only in ``relations``, kind allowed in its section);
2. **resolution** — every citation is a job source and, with ``view=``,
   still the same record in the admitted view; not generated text (G1); not a
   source that repeats a sentence of an approved page or of the job's
   previous page (G4 loop; a previous-page sentence does not count against
   a source that sentence cites);
3. **lifecycle** (G6) — a non-current source only in ``timeline`` (a
   ``disputed`` one also in ``open``), there as kind ``superseded``, and a
   ``superseded`` claim about a replaced source also cites the replacement;
4. **conflict** (design 2.3) — no claim outside ``open`` / ``timeline``
   cites a side of a job open question's conflict; no claim cites two sides;
5. **causal** (G5) — the label is at most the strongest cited source's grade
   (capped at ``hypothesis``); the wording asserts no more than the label
   (:mod:`memfw_wiki_gate.causal`); no certainty wording (G7);
6. **tokens** (check ①) — every number, version, date, id, path, quote and
   name appears in a cited source text (:mod:`memfw_wiki_gate.tokens`);
7. **NLI** — scores come only from the caller's own checker (``nli_scores``
   keyed by cid, ``nli_model``); the generator's ``checks.nli`` and
   ``build.nli_model`` are ignored. With ``require_nli`` every claim needs a
   caller score and the caller's model must be allowlisted; with
   ``nli_threshold`` a caller score below it drops the claim. Scoring and
   calibration are W1-5 / W1-6's job.

A claim that fails any check is dropped; the report lists every reason it
failed, as fixed codes. A section that loses more than half its claims is
withheld whole (``needs_attention``); no surviving ``summary`` claim means no
page. The surviving claims are rebuilt into a ``WikiPage`` that lists every
job source (sorted by ref), whatever claims survive; its privacy is the
strictest of them (the rule the W1-4 generator shares, so the ports invariant
``privacy == strictest(page.sources)`` holds; the generator's value is
ignored). A job whose ``previous_page`` is stricter than its sources is
refused (``previous_page_stricter``): the generator never offers one. The
page lists every open question of the job, and its claims carry ``tokens:
pass`` and the caller's NLI score, if any.

Reports hold codes, counts and claim ids only (and, with a caller key, keyed
HMAC sentence hashes): never page text, source text, refs or token values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from . import _ports as ports
from .causal import asserted_causal, asserts_certainty, causal_rank
from .normalize import (
    LOOP_MIN_CHARS,
    has_format_character,
    mixed_script_word,
    normalize_sentence,
    sentence_hash,
    sentence_hashes,
)
from .resolve import JobSources, ResolvedSource, SourceResolver
from .tokens import TOKEN_KINDS, SourceIndex, extract_tokens, unsupported_kinds

#: Written into a page's ``build.checker_version`` by jobs that use this gate.
CHECKER_VERSION: Final = "wiki-gate-0.1.0"
REPORT_FORMAT: Final = "memfw-wiki-gate-report/1"

Verdict = Literal["publish", "needs_attention", "retry", "reject"]
VERDICTS: Final[tuple[str, ...]] = ("publish", "needs_attention", "retry", "reject")

#: Why a claim was dropped.
CLAIM_REASONS: Final[tuple[str, ...]] = (
    # format
    "claim_format",
    "format_character",
    "mixed_script_word",
    "duplicate_cid",
    "uncited",
    "citation_malformed",
    "citation_not_canonical",
    "wiki_citation",
    "unversioned_memory",
    "duplicate_citation",
    "epistemic_above_inference",
    "causal_label_above_cap",
    "causal_outside_relations",
    "kind_section",
    # resolution
    "citation_unresolved",
    "source_changed",
    "source_not_releasable",
    "tenant_mismatch",
    "generated_source",
    "loop_source",
    # lifecycle (G6) and conflicts (2.3)
    "inactive_source_section",
    "inactive_source_kind",
    "superseded_without_replacement",
    "conflict_side",
    "conflict_both_sides",
    # causal (G5, G7)
    "causal_above_source",
    "causal_wording_above_label",
    "causal_wording_unhedged",
    "certainty_wording",
    # tokens (check 1)
    *(f"token_unsupported_{kind}" for kind in TOKEN_KINDS),
    # NLI presence
    "nli_missing",
    "nli_below_threshold",
    # page-level withholding
    "section_withheld",
)

#: Page-level reasons and the verdict each forces (worst wins).
PAGE_REASONS: Final[dict[str, str]] = {
    "page_format": "reject",
    "job_invalid": "reject",
    "job_mismatch": "reject",
    "source_not_in_job": "reject",
    "previous_page_stricter": "reject",
    "privacy_not_releasable": "reject",
    "tenant_mismatch": "reject",
    "nli_model_not_allowlisted": "reject",
    "summary_empty": "reject",
    "internal_inconsistent": "reject",
    "source_changed": "retry",
    "source_missing": "retry",
    "view_unavailable": "retry",
    "section_withheld": "needs_attention",
    "privacy_recomputed": "publish",
}

_VERDICT_RANK: Final[dict[str, int]] = {name: rank for rank, name in enumerate(VERDICTS)}
_MAX_PAGE_INPUT: Final = 4 * 16_384
_PAGE_KEYS: Final = frozenset(
    {
        "format",
        "page_key",
        "concept",
        "tenant",
        "privacy",
        "build",
        "sources",
        "sections",
        "open_questions",
        "built_from_digest",
    }
)
_CLAIM_KEYS: Final = frozenset({"cid", "text", "cites", "causal", "kind", "epistemic", "checks"})
_BUILD_KEYS: Final = (
    "generator",
    "generator_version",
    "model",
    "model_digest",
    "prompt_version",
    "checker_version",
    "nli_model",
    "view_version",
    "job_digest",
)
_FORMAT_CODES: Final[dict[str, str]] = {
    "uncited_claim": "uncited",
    "wiki_citation": "wiki_citation",
    "wiki_as_memory": "wiki_citation",
    "unversioned_memory": "unversioned_memory",
    "duplicate": "duplicate_citation",
    "epistemic_cap": "epistemic_above_inference",
    "causal_cap": "causal_label_above_cap",
    "causal_outside_relations": "causal_outside_relations",
    "claim_kind_section": "kind_section",
    "not_canonical": "citation_not_canonical",
    "unknown_citation": "citation_malformed",
}
_CAUSAL_CAP_RANK: Final = causal_rank(ports.WIKI_CAUSAL_CAP)
_CID_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True, slots=True)
class GateOptions:
    """What the caller asks of the gate beyond the fixed rules.

    ``approved_sentence_hashes`` (from :func:`page_sentence_hashes` over the
    approved pages) turns on the G4 loop check: a source with a sentence of at
    least ``loop_min_chars`` normalised code points whose hash is in the set
    cannot be cited. Rewording defeats it; G1-G3 in the kernel stay the main
    control. ``generated_refs`` names job sources the caller knows to be
    generated text (G1; a view reader says so per record instead).
    The previous approved page of the job (``job.previous_page``) always takes
    part, except against a source its own sentence cites; its hashes are
    removed from ``approved_sentence_hashes`` first, so passing that page
    there too does not cancel the exemption. Turn ``require_nli`` on before
    publishing to production.

    NLI: the gate never trusts the generator's ``checks.nli`` or
    ``build.nli_model``. Scores come from the caller's own checker, passed to
    :func:`check_page` as ``nli_scores`` (keyed by claim ``cid``) with the
    checker's ``nli_model``. ``require_nli`` asks for a caller score on every
    claim and an ``nli_model`` in ``nli_allowlist``; ``nli_threshold`` drops
    claims whose caller score is below it (calibration is W1-5's job; leave it
    ``None`` until then). ``section_drop_ratio``: a section losing more than
    this share of its claims is withheld.

    ``report_hash_key`` (at least 16 bytes, one per run, held by the caller)
    adds an HMAC-SHA256 of each claim's normalised sentence to the report
    (``hmac-sha256:<hex>``) so the caller can match claims across reports of
    the same run; without it the report has counts and cids only, no
    sentence hash (an unkeyed hash of a short sentence can be guessed).
    """

    approved_sentence_hashes: frozenset[str] = frozenset()
    generated_refs: frozenset[str] = frozenset()
    loop_min_chars: int = LOOP_MIN_CHARS
    require_nli: bool = False
    nli_allowlist: frozenset[str] = frozenset()
    nli_threshold: float | None = None
    section_drop_ratio: float = 0.5
    report_hash_key: bytes | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "approved_sentence_hashes", frozenset(self.approved_sentence_hashes)
        )
        object.__setattr__(self, "nli_allowlist", frozenset(self.nli_allowlist))
        object.__setattr__(self, "generated_refs", frozenset(self.generated_refs))
        if self.nli_threshold is not None and not (
            isinstance(self.nli_threshold, (int, float))
            and math.isfinite(self.nli_threshold)
            and 0.0 <= self.nli_threshold <= 1.0
        ):
            raise ValueError("nli_threshold must be a number in [0, 1]")
        if not 0.0 <= self.section_drop_ratio <= 1.0:
            raise ValueError("section_drop_ratio must be in [0, 1]")
        if type(self.loop_min_chars) is not int or self.loop_min_chars < 1:
            raise ValueError("loop_min_chars must be a positive int")
        key = self.report_hash_key
        if key is not None and not (isinstance(key, (bytes, bytearray)) and len(key) >= 16):
            raise ValueError("report_hash_key must be at least 16 bytes")
        if key is not None:
            object.__setattr__(self, "report_hash_key", bytes(key))


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """One claim's outcome. ``index`` is its position in page order (0-based)."""

    index: int
    section: str | None
    cid: str | None
    sentence_hash: str | None
    accepted: bool
    reasons: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "index": self.index,
            "section": self.section,
            "cid": self.cid,
            "sentence_hash": self.sentence_hash,
            "accepted": self.accepted,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class GateReport:
    """The outcome for one page: verdict, reasons, per-claim verdicts, counts.

    ``page`` is the publishable page (surviving claims only) for verdicts
    ``publish`` and ``needs_attention``, else ``None``.
    """

    verdict: Verdict
    page_reasons: tuple[str, ...]
    claims: tuple[ClaimVerdict, ...]
    counts: Mapping[str, Any]
    privacy: str | None = None
    page: Any = None
    checker_version: str = CHECKER_VERSION

    @property
    def publishable(self) -> bool:
        return self.verdict in ("publish", "needs_attention") and self.page is not None

    def to_json(self) -> dict[str, object]:
        """Codes, counts, ids and hashes only (no text, refs or token values)."""

        return {
            "format": REPORT_FORMAT,
            "checker_version": self.checker_version,
            "verdict": self.verdict,
            "page_reasons": list(self.page_reasons),
            "privacy": self.privacy,
            "page_digest": None if self.page is None else self.page.page_digest(),
            "counts": json.loads(json.dumps(self.counts, sort_keys=True)),
            "claims": [claim.to_json() for claim in self.claims],
        }

    def to_bytes(self) -> bytes:
        return ports.canonical_json(self.to_json())


class _PageError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class _Claim:
    index: int
    section: str | None
    raw: Mapping[str, Any] | None
    cid: str | None = None
    claim: Any = None
    reasons: list[str] = field(default_factory=list)


def page_sentence_hashes(pages: Iterable[Any]) -> frozenset[str]:
    """The G4 hash set of approved pages: each claim whole and each of its sentences."""

    out: set[str] = set()
    for page in pages:
        for claim in page.claims:
            out.add(sentence_hash(claim.text))
            out |= sentence_hashes(claim.text, min_chars=1)
    return frozenset(out)


def _previous_hashes(page: Any) -> dict[str, frozenset[str]]:
    """G4 over the job's previous page: each sentence hash and the refs citing it."""

    out: dict[str, set[str]] = {}
    for claim in () if page is None else page.claims:
        hashes = {sentence_hash(claim.text)} | sentence_hashes(claim.text, min_chars=1)
        for digest in hashes:
            out.setdefault(digest, set()).update(claim.refs())
    return {digest: frozenset(refs) for digest, refs in out.items()}


# --- loading -----------------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _PageError("page_format")
        result[key] = value
    return result


def _no_constant(name: str) -> object:
    raise _PageError("page_format")


def _load_page(page: object) -> Mapping[str, Any]:
    if type(page) is ports.WikiPage:
        return page.to_json()
    data: object = page
    if isinstance(data, (bytes, bytearray)):
        if len(data) > _MAX_PAGE_INPUT:
            raise _PageError("page_format")
        try:
            data = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _PageError("page_format") from exc
    if isinstance(data, str):
        if len(data.encode("utf-8")) > _MAX_PAGE_INPUT:
            raise _PageError("page_format")
        try:
            data = json.loads(data, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
        except (ValueError, RecursionError) as exc:
            raise _PageError("page_format") from exc
    if not isinstance(data, Mapping) or set(data) != _PAGE_KEYS:
        raise _PageError("page_format")
    return data


def _load_job(job: object) -> Any:
    if type(job) is ports.WikiJob:
        return job
    try:
        return ports.parse_wiki_job(job)  # type: ignore[arg-type]
    except ports.WikiFormatError as exc:
        raise _PageError("job_invalid") from exc
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise _PageError("job_invalid") from exc


def _envelope(doc: Mapping[str, Any], job: Any) -> tuple[Any, list[_Claim]]:
    """The build and the claims (unparsed); raises ``_PageError``."""

    if doc["format"] != "memfw-wiki-page/1":
        raise _PageError("page_format")
    if (
        doc["concept"] != job.concept.oid
        or doc["tenant"] != job.tenant
        or doc["page_key"] != job.page_key
    ):
        raise _PageError("job_mismatch")
    raw_build = doc["build"]
    if not isinstance(raw_build, Mapping) or set(raw_build) != set(_BUILD_KEYS):
        raise _PageError("page_format")
    try:
        build = ports.WikiBuild(**{name: raw_build[name] for name in _BUILD_KEYS})
    except (ValueError, TypeError) as exc:
        raise _PageError("page_format") from exc
    if (
        build.job_digest != job.job_digest
        or build.view_version != job.view_version
        or build.checker_version != job.checker_version
        or any(getattr(build, name) != value for name, value in job.generator.items())
    ):
        raise _PageError("job_mismatch")
    offered = {item.source.ref: item.source for item in job.sources}
    raw_sources = doc["sources"]
    if not isinstance(raw_sources, list):
        raise _PageError("page_format")
    for raw in raw_sources:
        if not isinstance(raw, Mapping):
            raise _PageError("page_format")
        try:
            source = ports.WikiSource(**dict(raw))
        except (ValueError, TypeError) as exc:
            raise _PageError("source_not_in_job") from exc
        if offered.get(source.ref) != source:
            raise _PageError("source_not_in_job")
    # ``open_questions``: the generator's value is ignored; the published page
    # lists every open question of the job (see check_page).
    sections = doc["sections"]
    if not isinstance(sections, list):
        raise _PageError("page_format")
    claims: list[_Claim] = []
    order: list[int] = []
    for raw_section in sections:
        if not isinstance(raw_section, Mapping) or set(raw_section) != {"id", "claims"}:
            raise _PageError("page_format")
        sid = raw_section["id"]
        if sid not in ports.WIKI_SECTIONS or ports.WIKI_SECTIONS.index(sid) in order:
            raise _PageError("page_format")
        order.append(ports.WIKI_SECTIONS.index(sid))
        items = raw_section["claims"]
        if not isinstance(items, list):
            raise _PageError("page_format")
        for item in items:
            claims.append(_Claim(len(claims), sid, item if isinstance(item, Mapping) else None))
    if order != sorted(order):
        raise _PageError("page_format")
    return build, claims


# --- one claim -----------------------------------------------------------------------


def _parse_claim(entry: _Claim) -> None:
    raw = entry.raw
    if raw is None or set(raw) != _CLAIM_KEYS:
        entry.reasons.append("claim_format")
        return
    cid = raw["cid"]
    if type(cid) is str and _CID_RE.fullmatch(cid):
        entry.cid = cid
    text = raw["text"]
    if type(text) is str and has_format_character(text):
        # Any Cf (ZWJ and ZWNJ too, which the format allows): it can hide a
        # marker or a token from the checks below.
        entry.reasons.append("format_character")
        return
    if type(text) is str and mixed_script_word(text):
        entry.reasons.append("mixed_script_word")
        return
    checks = raw["checks"]
    if not isinstance(checks, Mapping) or not {"tokens"} <= set(checks) <= {"tokens", "nli"}:
        entry.reasons.append("claim_format")
        return
    if "nli" in checks:
        nli_raw = checks["nli"]
        if not isinstance(nli_raw, Mapping) or set(nli_raw) != {"min"}:
            entry.reasons.append("claim_format")
            return
        if type(nli_raw["min"]) not in (int, float):
            entry.reasons.append("claim_format")
            return
        # The generator's score is never trusted (only the caller's, in _judge).
    cites = raw["cites"]
    if not isinstance(cites, list):
        entry.reasons.append("claim_format")
        return
    citations = []
    for ref in cites:
        try:
            citations.append(ports.Citation.from_ref(ref))
        except ValueError as exc:
            code = getattr(exc, "code", "malformed")
            if code == "wiki_as_memory" or (isinstance(ref, str) and ref.startswith("wiki:")):
                entry.reasons.append("wiki_citation")
            elif code == "not_canonical":
                entry.reasons.append("citation_not_canonical")
            else:
                entry.reasons.append("citation_malformed")
        except TypeError:
            entry.reasons.append("citation_malformed")
    if entry.reasons:
        entry.reasons = sorted(set(entry.reasons))
        return
    try:
        entry.claim = ports.WikiClaim(
            cid=cid,
            section=entry.section,  # type: ignore[arg-type]
            text=raw["text"],
            citations=tuple(citations),
            causal=raw["causal"],
            kind=raw["kind"],
            epistemic=raw["epistemic"],
            tokens=checks["tokens"],
        )
    except ports.WikiFormatError as exc:
        entry.reasons.append(_FORMAT_CODES.get(exc.code, "claim_format"))
    except (ValueError, TypeError):
        entry.reasons.append("claim_format")


@dataclass(slots=True)
class _Context:
    job: Any
    view: SourceResolver | None
    options: GateOptions
    job_sources: JobSources
    #: Previous approved page: sentence hash -> refs its claims with that
    #: sentence cite (the source a sentence was taken from is not its echo).
    previous: Mapping[str, frozenset[str]] = field(default_factory=dict)
    nli_scores: Mapping[str, float] | None = None
    page_reasons: set[str] = field(default_factory=set)
    resolved: dict[str, ResolvedSource | None] = field(default_factory=dict)
    indexes: dict[str, SourceIndex] = field(default_factory=dict)
    loops: dict[str, bool] = field(default_factory=dict)
    misses: dict[str, str] = field(default_factory=dict)

    def lookup(self, ref: str) -> tuple[ResolvedSource | None, str | None]:
        """The source a citation resolves to now, or ``(None, claim reason)``."""

        if ref in self.resolved:
            found = self.resolved[ref]
            return found, None if found is not None else self.misses.get(ref)
        offered = self.job_sources.resolve(ref)
        result: ResolvedSource | None = offered
        reason: str | None = None
        if offered is None:
            reason = "citation_unresolved"
        elif self.view is not None:
            try:
                current = self.view.resolve(ref)
            except Exception:  # noqa: BLE001 - any reader failure publishes nothing
                self.page_reasons.add("view_unavailable")
                current = None
                reason = "source_changed"
            else:
                if current is None:
                    self.page_reasons.add("source_missing")
                    reason = "source_changed"
                elif (
                    type(current) is not ResolvedSource
                    or type(current.source) is not ports.WikiSource
                ):
                    self.page_reasons.add("view_unavailable")
                    reason = "source_changed"
                    current = None
                elif current.source.privacy not in ports.RELEASABLE_PRIVACY:
                    self.page_reasons.add("privacy_not_releasable")
                    reason = "source_not_releasable"
                    current = None
                elif current.tenant != self.job.tenant:
                    self.page_reasons.add("tenant_mismatch")
                    reason = "tenant_mismatch"
                    current = None
                elif current.source != offered.source:
                    self.page_reasons.add("source_changed")
                    reason = "source_changed"
                    current = None
                elif current.text != offered.text:
                    # Same hash, other released text: the reader is inconsistent.
                    self.page_reasons.add("source_changed")
                    reason = "source_changed"
                    current = None
                else:
                    current = ResolvedSource(
                        source=offered.source,
                        text=offered.text,
                        tenant=offered.tenant,
                        generated=offered.generated or current.generated,
                    )
            result = current
        self.resolved[ref] = result
        if reason is not None:
            self.misses[ref] = reason
        return result, reason

    def index(self, found: ResolvedSource) -> SourceIndex:
        ref = found.source.ref
        if ref not in self.indexes:
            self.indexes[ref] = SourceIndex.build(found.text)
        return self.indexes[ref]

    def loop(self, found: ResolvedSource) -> bool:
        # The previous page's sentences are judged only through ``previous``
        # (with its own-source exemption), even when the caller also put that
        # page into ``approved_sentence_hashes``.
        hashes = self.options.approved_sentence_hashes - frozenset(self.previous)
        if not hashes and not self.previous:
            return False
        ref = found.source.ref
        if ref not in self.loops:
            mine = sentence_hashes(found.text, min_chars=self.options.loop_min_chars)
            self.loops[ref] = bool(mine & hashes) or any(
                ref not in self.previous[h] for h in mine if h in self.previous
            )
        return self.loops[ref]


def _judge(entry: _Claim, ctx: _Context) -> None:
    claim = entry.claim
    reasons = entry.reasons
    cited: list[ResolvedSource] = []
    for ref in claim.refs():
        found, why = ctx.lookup(ref)
        if found is None:
            reasons.append(why or "citation_unresolved")
            continue
        cited.append(found)
        if found.generated:
            reasons.append("generated_source")
        if ctx.loop(found):
            reasons.append("loop_source")
    refs = set(claim.refs())
    # G6: lifecycle.
    for found in cited:
        source = found.source
        if source.status == "current":
            continue
        allowed = claim.section == "timeline" or (
            source.status == "disputed" and claim.section == "open"
        )
        if not allowed:
            reasons.append("inactive_source_section")
        elif claim.section == "timeline" and claim.kind != "superseded":
            reasons.append("inactive_source_kind")
        if source.superseded_by is not None and source.superseded_by not in refs:
            reasons.append("superseded_without_replacement")
    # 2.3: conflicts the job names.
    memories = {c.memory_id for c in claim.citations if c.ref_kind == "memory"}
    for question in ctx.job.open_questions:
        sides = memories & set(question.conflicts)
        if sides and claim.section not in ("open", "timeline"):
            reasons.append("conflict_side")
        if len(sides) >= 2:
            reasons.append("conflict_both_sides")
    # G5 / G7: causal label and wording.
    if cited:
        ceiling = min(max(causal_rank(f.source.causal) for f in cited), _CAUSAL_CAP_RANK)
        if causal_rank(claim.causal) > ceiling:
            reasons.append("causal_above_source")
    asserted = asserted_causal(claim.text)
    if asserted == "verified":
        reasons.append("causal_wording_unhedged")
    elif causal_rank(asserted) > causal_rank(claim.causal):
        reasons.append("causal_wording_above_label")
    if asserts_certainty(claim.text):
        reasons.append("certainty_wording")
    # Check 1: tokens.
    if cited:
        tokens = extract_tokens(claim.text)
        for kind in unsupported_kinds(tokens, (ctx.index(found) for found in cited)):
            reasons.append(f"token_unsupported_{kind}")
    # NLI: the caller's scores only.
    score = None if ctx.nli_scores is None else ctx.nli_scores.get(claim.cid)
    if score is None:
        if ctx.options.require_nli:
            reasons.append("nli_missing")
    elif ctx.options.nli_threshold is not None and score < ctx.options.nli_threshold:
        reasons.append("nli_below_threshold")


def _order(reasons: Iterable[str]) -> tuple[str, ...]:
    present = set(reasons)
    assert present <= set(CLAIM_REASONS), present - set(CLAIM_REASONS)
    return tuple(code for code in CLAIM_REASONS if code in present)


def _counts(entries: Sequence[_Claim], kept: Sequence[_Claim], cited: int) -> dict[str, Any]:
    dropped: Counter[str] = Counter()
    by_section: dict[str, dict[str, int]] = {}
    for entry in entries:
        section = entry.section or "unknown"
        bucket = by_section.setdefault(section, {"total": 0, "kept": 0})
        bucket["total"] += 1
        if not entry.reasons:
            bucket["kept"] += 1
        for code in set(entry.reasons):
            dropped[code] += 1
    withheld = sum(1 for e in entries if e.reasons == ["section_withheld"])
    return {
        "claims_total": len(entries),
        "claims_accepted": len(kept),
        "claims_dropped": len(entries) - len(kept) - withheld,
        "claims_withheld": withheld,
        "sources_cited": cited,
        "dropped_by_reason": dict(sorted(dropped.items())),
        "by_section": dict(sorted(by_section.items())),
    }


def _verdict(reasons: Iterable[str]) -> Verdict:
    worst = "publish"
    for code in reasons:
        level = PAGE_REASONS[code]
        if _VERDICT_RANK[level] > _VERDICT_RANK[worst]:
            worst = level
    return worst  # type: ignore[return-value]


def _keyed_hash(key: bytes | None, claim: Any) -> str | None:
    if key is None or claim is None:
        return None
    digest = hmac.new(key, normalize_sentence(claim.text).encode("utf-8"), hashlib.sha256)
    return "hmac-sha256:" + digest.hexdigest()


def _report(
    reasons: Iterable[str],
    entries: Sequence[_Claim] = (),
    kept: Sequence[_Claim] = (),
    *,
    cited: int = 0,
    privacy: str | None = None,
    page: Any = None,
    hash_key: bytes | None = None,
) -> GateReport:
    page_reasons = tuple(code for code in PAGE_REASONS if code in set(reasons))
    verdict = _verdict(page_reasons)
    if verdict in ("retry", "reject"):
        page = None
    claims = tuple(
        ClaimVerdict(
            index=entry.index,
            section=entry.section,
            cid=entry.cid,
            sentence_hash=_keyed_hash(hash_key, entry.claim),
            accepted=not entry.reasons and page is not None,
            reasons=_order(entry.reasons),
        )
        for entry in entries
    )
    counts = _counts(entries, kept if page is not None else (), cited)
    return GateReport(
        verdict=verdict,
        page_reasons=page_reasons,
        claims=claims,
        counts=counts,
        privacy=privacy,
        page=page,
    )


def _caller_scores(nli_scores: Mapping[str, float] | None) -> dict[str, float] | None:
    if nli_scores is None:
        return None
    if not isinstance(nli_scores, Mapping):
        raise ValueError("nli_scores must map claim cids to scores")
    out: dict[str, float] = {}
    for cid, score in nli_scores.items():
        if not (
            type(cid) is str
            and type(score) in (int, float)
            and math.isfinite(score)
            and 0.0 <= score <= 1.0
        ):
            raise ValueError("nli_scores must map claim cids to numbers in [0, 1]")
        out[cid] = float(score)
    return out


def check_page(
    page: object,
    job: object,
    *,
    view: SourceResolver | None = None,
    options: GateOptions | None = None,
    nli_scores: Mapping[str, float] | None = None,
    nli_model: str | None = None,
) -> GateReport:
    """Check ``page`` (a ``WikiPage``, its document or JSON text) against ``job``.

    ``job`` is a ``WikiJob``, its document or JSON text. With ``view`` (the
    intake's admitted-view reader) every job source must also still resolve
    to the same record there. ``nli_scores`` (claim ``cid`` -> score in
    ``[0, 1]``) and ``nli_model`` come from the caller's own entailment
    checker; the generator's ``checks.nli`` and ``build.nli_model`` are never
    used. Never raises for bad page or job input; raises ``ValueError`` for a
    malformed ``nli_scores`` or ``nli_model``.
    """

    options = options or GateOptions()
    scores = _caller_scores(nli_scores)
    if nli_model is not None and type(nli_model) is not str:
        raise ValueError("nli_model must be a string")
    try:
        work = _load_job(job)
        doc = _load_page(page)
        build, entries = _envelope(doc, work)
    except _PageError as exc:
        return _report([exc.code], hash_key=options.report_hash_key)
    ctx = _Context(
        job=work,
        view=view,
        options=options,
        job_sources=JobSources(work, generated=options.generated_refs),
        previous=_previous_hashes(work.previous_page),
        nli_scores=scores,
    )
    # Privacy (rule shared with the generator, W1-4): the page lists every job
    # source and its level is the strictest of them, whichever claims survive.
    job_level = ports.strictest(item.source.privacy for item in work.sources)
    previous = work.previous_page
    if previous is not None and ports.strictest((previous.privacy, job_level)) != job_level:
        # The generator never offers a previous page stricter than the job's
        # sources; seeing one means the job (or its builder) is wrong.
        ctx.page_reasons.add("previous_page_stricter")
    if options.require_nli and nli_model not in options.nli_allowlist:
        ctx.page_reasons.add("nli_model_not_allowlisted")
    seen: set[str] = set()
    for entry in entries:
        _parse_claim(entry)
        if entry.claim is not None:
            if entry.claim.cid in seen:
                entry.reasons.append("duplicate_cid")
            seen.add(entry.claim.cid)
        if not entry.reasons:
            _judge(entry, ctx)
    if view is not None:
        # Every listed source, cited or not, must still be the job's record
        # (a changed level would change the page's privacy).
        for item in sorted(work.sources, key=lambda item: item.source.ref):
            ctx.lookup(item.source.ref)
    # Withhold sections that lost too much (design 1.2). Skipped when the page
    # is already void (a changed source, a bad view or build): those claims
    # were dropped for that reason, and "summary empty" would hide it.
    void = any(PAGE_REASONS[code] in ("retry", "reject") for code in ctx.page_reasons)
    for section in () if void else ports.WIKI_SECTIONS:
        members = [e for e in entries if e.section == section]
        if not members:
            continue
        dropped = sum(1 for e in members if e.reasons)
        if dropped / len(members) > options.section_drop_ratio:
            for entry in members:
                if not entry.reasons:
                    entry.reasons.append("section_withheld")
            ctx.page_reasons.add("section_withheld")
    kept = [e for e in entries if not e.reasons]
    if not void and not any(e.section == "summary" for e in kept):
        ctx.page_reasons.add("summary_empty")
    used = sorted({ref for entry in kept for ref in entry.claim.refs()})
    # By ref: the page does not depend on the job's source order.
    sources = sorted((item.source for item in work.sources), key=lambda source: source.ref)
    privacy = job_level
    if privacy not in ports.RELEASABLE_PRIVACY:
        ctx.page_reasons.add("privacy_not_releasable")
    if doc["privacy"] != privacy:
        ctx.page_reasons.add("privacy_recomputed")
    questions = tuple(sorted(question.id for question in work.open_questions))
    built = None
    if _verdict(ctx.page_reasons) in ("publish", "needs_attention"):
        try:
            published_build = build if nli_model is None else replace(build, nli_model=nli_model)
            built = ports.WikiPage(
                page_key=work.page_key,
                concept=work.concept.oid,
                tenant=work.tenant,
                privacy=privacy,
                build=published_build,
                sources=tuple(sources),
                claims=tuple(_passed(entry.claim, scores) for entry in kept),
                open_questions=questions,
                built_from_digest=ports.wiki_sources_digest(sources),
            )
        except (ValueError, TypeError):
            ctx.page_reasons.add("internal_inconsistent")
    return _report(
        ctx.page_reasons,
        entries,
        kept,
        cited=len(used),
        privacy=privacy,
        page=built,
        hash_key=options.report_hash_key,
    )


def _passed(claim: Any, scores: Mapping[str, float] | None) -> Any:
    """The claim as the gate publishes it: token check passed, the caller's NLI score."""

    return ports.WikiClaim(
        cid=claim.cid,
        section=claim.section,
        text=claim.text,
        citations=claim.citations,
        causal=claim.causal,
        kind=claim.kind,
        epistemic=claim.epistemic,
        tokens="pass",
        nli_min=None if scores is None else scores.get(claim.cid),
    )


__all__ = [
    "CHECKER_VERSION",
    "CLAIM_REASONS",
    "PAGE_REASONS",
    "REPORT_FORMAT",
    "VERDICTS",
    "ClaimVerdict",
    "GateOptions",
    "GateReport",
    "Verdict",
    "check_page",
    "page_sentence_hashes",
]
