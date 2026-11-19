"""Model reply -> ``memfw-wiki-page/1`` page. The reply is data, never trusted.

Two levels of refusal:

* **The whole reply** (:class:`BadOutput`, retried once by the deriver): not one
  JSON object, a duplicate key, a key outside ``summary`` / ``facts`` /
  ``timeline`` / ``relations`` / ``missing_evidence`` (a model that writes
  ``open`` or ``sources`` is not following the page structure), or a section
  that is not a list.
* **One sentence** (dropped and counted by reason code, never repaired): a
  shape error, a ref that is not *exactly* one of the job's source refs, a
  causal grade above its cited sources' or outside ``relations``, a kind not
  allowed in its section, a non-current source outside ``timeline`` (or a
  ``timeline`` sentence citing one that is not kind ``superseded``), a side
  taken in one of the job's conflicts, a duplicate, or one past the
  per-section cap.

The program then writes what the model may not: the ``open`` section's
``conflict`` claims (one per side of each open question, citing that side's
refs, text = the question as the job states it), the page's ``sources``
(*every* job source, cited or not, field for field and in job order),
``open_questions`` (every question of the job), ``privacy`` (the strictest
level over all job sources: everything the model saw, design 3.2; the ports
require it to equal the strictest listed source, hence the full list) and
``built_from_digest`` (over all of them). A job with many sources can so
exceed the 16 KiB page limit: ``page_invalid:too_large``, never trimmed.
The assembled document goes through
``memfw.ports.wiki.parse_wiki_page`` and ``memfw.ports.testing.check_wiki_page``
(against the job); anything they refuse refuses the whole page.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ._ports import (
    CAUSAL_VALUES,
    MAX_CITES_PER_CLAIM,
    MAX_CLAIM_CHARS,
    WIKI_CAUSAL_CAP,
    WIKI_PAGE_FORMAT,
    Citation,
    ContractError,
    WikiClaim,
    WikiFormatError,
    WikiJob,
    WikiPage,
    check_wiki_page,
    parse_wiki_page,
    strictest,
    wiki_sources_digest,
)
from .prompt import MISSING_KEY, MODEL_SECTIONS, REPLY_KEYS, conflict_refs

_RANK: dict[str, int] = {value: rank for rank, value in enumerate(CAUSAL_VALUES)}
_CID_PREFIX = {"summary": "s", "facts": "f", "timeline": "t", "relations": "r", "open": "o"}
_CLAIM_KEYS = frozenset({"text", "cites", "causal", "kind"})
_MISSING_KEYS = frozenset({"question", "text", "cites"})
#: Longest reply read (a page is at most 16 KiB; this leaves room for JSON noise).
MAX_REPLY_CHARS = 256 * 1024
MAX_REPLY_DEPTH = 8

#: Sentence-level drop reasons.
DROP_REASONS: tuple[str, ...] = (
    "shape",
    "text",
    "uncited",
    "too_many_cites",
    "unknown_citation",
    "duplicate_cite",
    "causal_value",
    "causal_cap",
    "causal_outside_relations",
    "causal_above_source",
    "kind_section",
    "inactive_source_section",
    "inactive_source_kind",
    "conflict_side",
    "conflict_both",
    "unknown_question",
    "duplicate",
    "too_many_claims",
)


class BadOutput(ValueError):
    """The whole reply is unusable. ``code`` is short and never holds reply text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PageRefused(ValueError):
    """No page can be built from a usable reply (``summary_empty``, ``page_invalid:<code>``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Report:
    """Counts and reason codes of one assembly; never text."""

    offered: dict[str, int] = field(default_factory=dict)
    kept: dict[str, int] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)
    #: Sections where more than half of what the model offered was dropped (design 1.2).
    needs_attention: list[str] = field(default_factory=list)

    def drop(self, reason: str) -> None:
        assert reason in DROP_REASONS, reason
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def to_json(self) -> dict[str, Any]:
        return {
            "offered": dict(sorted(self.offered.items())),
            "kept": dict(sorted(self.kept.items())),
            "dropped": dict(sorted(self.dropped.items())),
            "needs_attention": list(self.needs_attention),
        }


def _claim_ok(claim: _Claim) -> bool:
    """Whether the ports accept the sentence on its own (text characters, cites, kind)."""

    try:
        WikiClaim(
            cid="c1",
            section=claim.section,  # type: ignore[arg-type]
            text=claim.text,
            citations=tuple(Citation.from_ref(ref) for ref in claim.refs),
            causal=claim.causal,
            kind=claim.kind,  # type: ignore[arg-type]
        )
    except (WikiFormatError, ValueError):
        return False
    return True


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise BadOutput("duplicate_key")
        out[key] = value
    return out


def _no_constant(_: str) -> Any:
    raise BadOutput("not_json")


def _depth(value: Any, level: int = 0) -> int:
    if level > MAX_REPLY_DEPTH:
        return level
    if isinstance(value, Mapping):
        return max((_depth(item, level + 1) for item in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(item, level + 1) for item in value), default=level + 1)
    return level


def _unfence(text: str) -> str:
    """The reply without one surrounding ```json fence (no other cleanup)."""

    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```") and stripped.count("```") == 2:
        inner = stripped[3:-3]
        first, _, rest = inner.partition("\n")
        if first.strip().lower() in ("", "json"):
            return rest
    return stripped


def load_reply(text: str) -> dict[str, Any]:
    """The reply as a JSON object with only the allowed keys. Raises :class:`BadOutput`."""

    if not isinstance(text, str) or not text.strip():
        raise BadOutput("empty_reply")
    if len(text) > MAX_REPLY_CHARS:
        raise BadOutput("reply_too_large")
    try:
        value = json.loads(
            _unfence(text), object_pairs_hook=_no_duplicates, parse_constant=_no_constant
        )
    except BadOutput:
        raise
    except (ValueError, RecursionError):
        raise BadOutput("not_json") from None
    if not isinstance(value, dict):
        raise BadOutput("not_object")
    if set(value) - REPLY_KEYS:
        raise BadOutput("unknown_key")
    if _depth(value) > MAX_REPLY_DEPTH:
        raise BadOutput("too_deep")
    for key in REPLY_KEYS:
        if key in value and not isinstance(value[key], list):
            raise BadOutput("section_not_list")
    return value


@dataclass(frozen=True)
class _Claim:
    section: str
    text: str
    refs: tuple[str, ...]
    causal: str
    kind: str


class _Assembler:
    def __init__(self, job: WikiJob, max_claims: int) -> None:
        self.job = job
        self.max_claims = max_claims
        self.sources = {item.source.ref: item.source for item in job.sources}
        self.conflicts = conflict_refs(job)
        self.report = Report()
        self.seen: set[tuple[str, str]] = set()

    # -- shared sentence checks ----------------------------------------------------

    def _text(self, raw: Any) -> str | None:
        if not isinstance(raw, str):
            self.report.drop("shape")
            return None
        text = raw.strip()
        if not text or len(text) > MAX_CLAIM_CHARS:
            self.report.drop("text")
            return None
        return text

    def _refs(self, raw: Any) -> tuple[str, ...] | None:
        if not isinstance(raw, list) or not all(isinstance(ref, str) for ref in raw):
            self.report.drop("shape")
            return None
        if not raw:
            self.report.drop("uncited")
            return None
        if len(raw) > MAX_CITES_PER_CLAIM:
            self.report.drop("too_many_cites")
            return None
        # Exact match only: a ref the job does not list is never "corrected".
        if any(ref not in self.sources for ref in raw):
            self.report.drop("unknown_citation")
            return None
        if len(set(raw)) != len(raw):
            self.report.drop("duplicate_cite")
            return None
        return tuple(raw)

    def _conflict_ok(self, section: str, refs: Sequence[str]) -> bool:
        for sides in self.conflicts.values():
            cited = {Citation.from_ref(ref).memory_id for ref in refs if ref in sides}
            if not cited:
                continue
            if section not in ("open", "timeline"):
                self.report.drop("conflict_side")
                return False
            if len(cited) > 1:
                self.report.drop("conflict_both")
                return False
        return True

    def _lifecycle_ok(self, section: str, kind: str, refs: Sequence[str]) -> bool:
        for ref in refs:
            status = self.sources[ref].status
            if status == "current":
                continue
            if not (section == "timeline" or (status == "disputed" and section == "open")):
                self.report.drop("inactive_source_section")
                return False
            if section == "timeline" and kind != "superseded":
                self.report.drop("inactive_source_kind")
                return False
        return True

    def _unique(self, section: str, text: str) -> bool:
        key = (section, " ".join(text.split()).casefold())
        if key in self.seen:
            self.report.drop("duplicate")
            return False
        self.seen.add(key)
        return True

    # -- model sections ------------------------------------------------------------

    def model_claim(self, section: str, raw: Any) -> _Claim | None:
        if not isinstance(raw, Mapping) or set(raw) - _CLAIM_KEYS:
            self.report.drop("shape")
            return None
        text = self._text(raw.get("text"))
        if text is None:
            return None
        refs = self._refs(raw.get("cites"))
        if refs is None:
            return None
        causal = raw.get("causal", "none")
        kind = raw.get("kind", "statement")
        if causal not in CAUSAL_VALUES:
            self.report.drop("causal_value")
            return None
        if _RANK[causal] > _RANK[WIKI_CAUSAL_CAP]:
            self.report.drop("causal_cap")
            return None
        if causal != "none" and section != "relations":
            self.report.drop("causal_outside_relations")
            return None
        if _RANK[causal] > max(_RANK[self.sources[ref].causal] for ref in refs):
            self.report.drop("causal_above_source")
            return None
        allowed = ("statement", "superseded") if section == "timeline" else ("statement",)
        if kind not in allowed:
            self.report.drop("kind_section")
            return None
        if not self._lifecycle_ok(section, kind, refs):
            return None
        if not self._conflict_ok(section, refs):
            return None
        claim = _Claim(section, text, refs, causal, kind)
        if not _claim_ok(claim):
            self.report.drop("text")
            return None
        if not self._unique(section, text):
            return None
        return claim

    def section(self, section: str, items: Sequence[Any]) -> list[_Claim]:
        self.report.offered[section] = len(items)
        kept: list[_Claim] = []
        for raw in items:
            claim = self.model_claim(section, raw)
            if claim is None:
                continue
            if len(kept) >= self.max_claims:
                self.report.drop("too_many_claims")
                continue
            kept.append(claim)
        self.report.kept[section] = len(kept)
        if items and len(items) - len(kept) > len(items) / 2:
            self.report.needs_attention.append(section)
        return kept

    # -- the open section: conflicts by the program, missing evidence by the model ---

    def missing(self, items: Sequence[Any]) -> dict[str, _Claim]:
        self.report.offered["missing_evidence"] = len(items)
        asked = {question.id for question in self.job.open_questions}
        out: dict[str, _Claim] = {}
        for raw in items:
            if not isinstance(raw, Mapping) or set(raw) != _MISSING_KEYS:
                self.report.drop("shape")
                continue
            question = raw.get("question")
            if not isinstance(question, str) or question not in asked:
                self.report.drop("unknown_question")
                continue
            if question in out:
                self.report.drop("duplicate")
                continue
            text = self._text(raw.get("text"))
            if text is None:
                continue
            refs = self._refs(raw.get("cites"))
            if refs is None:
                continue
            if not self._lifecycle_ok("open", "missing_evidence", refs):
                continue
            if not self._conflict_ok("open", refs):
                continue
            claim = _Claim("open", text, refs, "none", "missing_evidence")
            if not _claim_ok(claim):
                self.report.drop("text")
                continue
            if not self._unique("open", text):
                continue
            out[question] = claim
        self.report.kept["missing_evidence"] = len(out)
        if items and len(items) - len(out) > len(items) / 2:
            self.report.needs_attention.append("missing_evidence")
        return out

    def open_section(self, missing: Mapping[str, _Claim]) -> list[_Claim]:
        claims: list[_Claim] = []
        for question in self.job.open_questions:
            sides: dict[str, list[str]] = {}
            for ref in self.conflicts[question.id]:
                if self.sources[ref].status in ("current", "disputed"):
                    sides.setdefault(Citation.from_ref(ref).memory_id, []).append(ref)
            text = " ".join(question.question.split())
            if len(text) > MAX_CLAIM_CHARS:
                text = text[: MAX_CLAIM_CHARS - 1].rstrip() + "…"
            if len(sides) >= 2:  # a conflict has two sides; one alone is not shown as one
                for memory_id in question.conflicts:
                    refs = sides.pop(memory_id, None)
                    if not refs:
                        continue
                    claim = _Claim("open", text, tuple(refs[:MAX_CITES_PER_CLAIM]), "none",
                                   "conflict")  # fmt: skip
                    if _claim_ok(claim):  # a question text the ports refuse is not shown
                        claims.append(claim)
            if question.id in missing:
                claims.append(missing[question.id])
        return claims


def assemble(
    reply: str,
    job: WikiJob,
    *,
    build: Mapping[str, str],
    max_claims: int,
) -> tuple[WikiPage, Report]:
    """The page for ``job`` from ``reply``. See the module docstring for every rule.

    Raises :class:`BadOutput` (the reply as a whole) or :class:`PageRefused`.
    """

    value = load_reply(reply)
    assembler = _Assembler(job, max_claims)
    claims: list[_Claim] = []
    for section in MODEL_SECTIONS:
        claims.extend(assembler.section(section, value.get(section, [])))
    claims.extend(assembler.open_section(assembler.missing(value.get(MISSING_KEY, []))))
    report = assembler.report
    if not any(claim.section == "summary" for claim in claims):
        raise PageRefused("summary_empty")
    # All job sources, not only the cited ones: the privacy level must cover
    # everything the model saw (an uncited shared text still shaped the reply).
    sources = [item.source for item in job.sources]
    counters: dict[str, int] = {}
    sections: dict[str, list[dict[str, Any]]] = {}
    for claim in claims:
        counters[claim.section] = counters.get(claim.section, 0) + 1
        sections.setdefault(claim.section, []).append(
            {
                "cid": f"{_CID_PREFIX[claim.section]}{counters[claim.section]}",
                "text": claim.text,
                "cites": list(claim.refs),
                "causal": claim.causal,
                "kind": claim.kind,
                "epistemic": "inference",
                "checks": {"tokens": "not_run"},
            }
        )
    document = {
        "format": WIKI_PAGE_FORMAT,
        "page_key": job.page_key,
        "concept": job.concept.oid,
        "tenant": job.tenant,
        "privacy": strictest(source.privacy for source in sources),
        "build": dict(build),
        "sources": [source.to_json() for source in sources],
        "sections": [{"id": name, "claims": items} for name, items in sections.items()],
        "open_questions": [question.id for question in job.open_questions],
        "built_from_digest": wiki_sources_digest(sources),
    }
    return validate(document, job), report


def validate(page: Mapping[str, Any] | WikiPage, job: WikiJob) -> WikiPage:
    """The ports parser and the page-against-job contract; any refusal refuses the page."""

    try:
        parsed = page if isinstance(page, WikiPage) else parse_wiki_page(page)
        return check_wiki_page(parsed, job=job)
    except WikiFormatError as exc:
        raise PageRefused(f"page_invalid:{exc.code}") from None
    except ContractError:
        raise PageRefused("page_invalid:job_contract") from None


__all__ = [
    "DROP_REASONS",
    "MAX_REPLY_CHARS",
    "BadOutput",
    "PageRefused",
    "Report",
    "assemble",
    "load_reply",
    "validate",
]
