"""Wiki page and wiki job formats (ports 0.4.0, W1-1). Standard library only.

A wiki page is a set of cited sentences about one ontology concept, in fixed
sections. It is generated text, never evidence: every sentence (a
:class:`WikiClaim`) cites at least one source, claims no stronger epistemic
type than ``inference`` and no stronger causal grade than ``hypothesis`` or
than the strongest of its own sources, and the page's privacy level is the
strictest of its sources'. This module holds the data contract only. The
kernel stores approved pages; generating, checking and indexing them happen in
adapters and pipeline stages.

Two formats:

* ``memfw-wiki-page/1`` (:class:`WikiPage`, :func:`parse_wiki_page`) is what a
  generator produces and what an approved page record keeps next to its
  rendered Markdown.
* ``memfw-wiki-job/1`` (:class:`WikiJob`, :func:`parse_wiki_job`) is the work
  package a generator reads: the concept, the released source texts with
  their hashes, the open questions and the previously approved page. It holds
  ids and released text only, never a vault path.

``WIKI_PAGE_SCHEMA`` and ``WIKI_JOB_SCHEMA`` describe both as JSON Schema
(draft 2020-12) for other languages. JSON Schema cannot express the
cross-field rules (citations resolve to listed sources, privacy is the
strictest source, causal grade at most the cited sources', digests), so the
parsers here are the reference: a document is valid only when the parser
accepts it. Every refusal is a :class:`WikiFormatError` with a fixed
``code``; the message never contains page text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from .privacy import PRIVACY_LEVELS, strictest
from .refs import (
    WIKI_PAGE_KEY_RE,
    WIKI_SECTIONS,
    CitationRefError,
    citation_ref_kind,
    parse_memory_ref,
)
from .types import CAUSAL_VALUES, MEMORY_ID_RE, Citation, check_name

WIKI_PAGE_FORMAT: Final = "memfw-wiki-page/1"
WIKI_JOB_FORMAT: Final = "memfw-wiki-job/1"

WikiSection = Literal["summary", "facts", "timeline", "relations", "open"]

#: What a claim is. ``superseded`` (an old conclusion and what replaced it)
#: belongs in ``timeline``; ``conflict`` and ``missing_evidence`` belong in
#: ``open`` and are the only kinds allowed there.
ClaimKind = Literal["statement", "superseded", "conflict", "missing_evidence"]
CLAIM_KINDS: Final[tuple[str, ...]] = get_args(ClaimKind)
_KIND_SECTIONS: Final[dict[str, frozenset[str]]] = {
    "statement": frozenset({"summary", "facts", "timeline", "relations"}),
    "superseded": frozenset({"timeline"}),
    "conflict": frozenset({"open"}),
    "missing_evidence": frozenset({"open"}),
}

#: The only epistemic type a wiki claim may carry (design G7): a human
#: approving a page does not turn its sentences into facts.
WIKI_EPISTEMIC: Final = "inference"
#: The strongest causal grade a wiki claim may carry (design G5).
WIKI_CAUSAL_CAP: Final = "hypothesis"
WIKI_CAUSAL_VALUES: Final[tuple[str, ...]] = ("none", "association", "hypothesis")
_CAUSAL_RANK: Final[dict[str, int]] = {value: rank for rank, value in enumerate(CAUSAL_VALUES)}

#: A page source is one released record; a page never cites a page.
WikiSourceKind = Literal["memory", "note_segment", "attachment_segment"]
WIKI_SOURCE_KINDS: Final[tuple[str, ...]] = get_args(WikiSourceKind)
#: A source's lifecycle when the page was built. Anything but ``current`` may
#: be cited only in ``timeline`` (``disputed`` also in ``open``; design G6).
SourceStatus = Literal["current", "superseded", "retired", "expired", "disputed"]
SOURCE_STATUSES: Final[tuple[str, ...]] = get_args(SourceStatus)
#: Levels a page (and a job source) may have: ``local-private`` never leaves.
RELEASABLE_PRIVACY: Final[tuple[str, ...]] = ("public", "shared")

TokenCheck = Literal["pass", "not_run"]
TOKEN_CHECKS: Final[tuple[str, ...]] = get_args(TokenCheck)

#: Size limits. A page is at most 16 KiB as canonical JSON (design 5.2).
MAX_WIKI_PAGE_BYTES: Final = 16_384
MAX_CLAIM_CHARS: Final = 1_000
MAX_CLAIMS_PER_SECTION: Final = 50
MAX_CITES_PER_CLAIM: Final = 8
MAX_WIKI_SOURCES: Final = 200
MAX_OPEN_QUESTIONS: Final = 50
MAX_JOB_SOURCE_CHARS: Final = 8_000
MAX_JOB_TEXT_CHARS: Final = 2_000
MAX_JOB_LABELS: Final = 32
MAX_JOB_BYTES: Final = 2 * 1024 * 1024

#: Reason codes of :class:`WikiFormatError`.
WIKI_ERROR_CODES: Final[tuple[str, ...]] = (
    "format",
    "field",
    "too_large",
    "page_key",
    "digest_mismatch",
    "duplicate",
    "section_order",
    "summary_missing",
    "uncited_claim",
    "unknown_citation",
    "wiki_citation",
    "unversioned_memory",
    "claim_kind_section",
    "epistemic_cap",
    "causal_cap",
    "causal_outside_relations",
    "causal_above_source",
    "inactive_source_section",
    "privacy_not_strictest",
    "privacy_not_releasable",
    "too_deep",
    "wiki_as_memory",
    "not_canonical",
    "inactive_source_kind",
)

#: Deepest JSON nesting a page or job document may have (the formats need 6).
MAX_JSON_DEPTH: Final = 32

_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_CID_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_LINE_SEPARATORS: Final = frozenset({"\u2028", "\u2029"})
#: Cf characters text needs: ZWNJ and ZWJ (Persian/Indic shaping, emoji sequences).
_ALLOWED_FORMAT: Final = frozenset({"\u200c", "\u200d"})


def _unsafe_text(text: str) -> bool:
    """True when ``text`` has a control, format (Cf, e.g. bidi) or line/paragraph separator."""

    return bool(_CONTROL_RE.search(text)) or any(
        char in _LINE_SEPARATORS
        or (unicodedata.category(char) == "Cf" and char not in _ALLOWED_FORMAT)
        for char in text
    )


def _token_re(limit: int) -> re.Pattern[str]:
    return re.compile(r"^[^\s\x00-\x1f\x7f]{1," + str(limit) + r"}$")


_TOKEN_64: Final = _token_re(64)
_TOKEN_128: Final = _token_re(128)
_TOKEN_192: Final = _token_re(192)
_CONCEPT_RE: Final = _token_re(256)
_TENANT_RE: Final = re.compile(r"^[^\x00-\x1f\x7f]{0,128}$")


class WikiFormatError(ValueError):
    """A wiki page or job that breaks its format. ``code`` is one of ``WIKI_ERROR_CODES``."""

    def __init__(self, code: str, message: str) -> None:
        assert code in WIKI_ERROR_CODES, code
        super().__init__(f"{code}: {message}")
        self.code = code


def _fail(code: str, message: str) -> WikiFormatError:
    return WikiFormatError(code, message)


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise _fail(code, message)


def _text(value: object, what: str, pattern: re.Pattern[str]) -> str:
    _require(type(value) is str and bool(pattern.fullmatch(value)), "field", f"bad {what}")
    return value  # type: ignore[return-value]


def _ref_error(exc: CitationRefError, what: str) -> WikiFormatError:
    code = {"wiki_as_memory": "wiki_as_memory", "not_canonical": "not_canonical"}.get(
        exc.code, "field"
    )
    return _fail(code, f"bad {what}")


def _digest(payload: object) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def canonical_json(value: object) -> bytes:
    """UTF-8 JSON with sorted keys and no spaces: the form digests and limits use."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def wiki_page_key(tenant: str, concept: str) -> str:
    """``wiki-`` + the first 8 bytes (hex) of sha256 over ``[tenant, concept]``.

    Stable across renames of the concept's labels: only the tenant and the oid
    go in.
    """

    data = json.dumps([tenant, concept], ensure_ascii=False, separators=(",", ":"))
    return "wiki-" + hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


# --- page parts ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WikiSource:
    """One released record a page was built from, with its content hash then."""

    ref: str
    kind: WikiSourceKind
    sha256: str
    privacy: str
    causal: str
    status: SourceStatus = "current"
    #: The ref that replaced this source (``superseded``/``retired`` only).
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        try:
            kind = citation_ref_kind(self.ref)
        except CitationRefError as exc:
            raise _ref_error(exc, "source ref") from exc
        _require(kind != "wiki_page", "wiki_citation", "a page cannot use a wiki page as source")
        _require(self.kind in WIKI_SOURCE_KINDS, "field", "bad source kind")
        _require(kind == self.kind, "field", "source kind does not match its ref")
        if kind == "memory":
            parsed = parse_memory_ref(self.ref)
            _require(
                parsed is not None and parsed[1] is not None,
                "unversioned_memory",
                "a memory source needs mem:<id>@v<n>",
            )
        _text(self.sha256, "source sha256", _SHA256_RE)
        _require(self.privacy in PRIVACY_LEVELS, "field", "bad source privacy")
        _require(self.causal in CAUSAL_VALUES, "field", "bad source causal")
        _require(self.status in SOURCE_STATUSES, "field", "bad source status")
        _require(
            self.status != "superseded" or self.superseded_by is not None,
            "field",
            "a superseded source names its superseded_by (design G6)",
        )
        if self.superseded_by is not None:
            _require(
                self.status in ("superseded", "retired"),
                "field",
                "superseded_by needs status superseded or retired",
            )
            try:
                successor = citation_ref_kind(self.superseded_by)
            except CitationRefError as exc:
                raise _ref_error(exc, "superseded_by ref") from exc
            _require(successor != "wiki_page", "wiki_citation", "a wiki page replaces nothing")
            _require(self.superseded_by != self.ref, "field", "a source cannot replace itself")

    def to_json(self) -> dict[str, object]:
        out: dict[str, object] = {
            "ref": self.ref,
            "kind": self.kind,
            "sha256": self.sha256,
            "privacy": self.privacy,
            "causal": self.causal,
            "status": self.status,
        }
        if self.superseded_by is not None:
            out["superseded_by"] = self.superseded_by
        return out


@dataclass(frozen=True, slots=True)
class WikiClaim:
    """One cited sentence in one section.

    ``citations`` are :class:`~memfw.ports.types.Citation` values with a
    ``ref`` (``mem:<id>@v<n>``, ``kh:``, ``att:``; never ``wiki:``).
    ``tokens`` records the deterministic token check (``pass`` or
    ``not_run``; a failed claim is dropped, never published) and ``nli_min``
    the entailment score, when one was computed.
    """

    cid: str
    section: WikiSection
    text: str
    citations: tuple[Citation, ...]
    causal: str = "none"
    kind: ClaimKind = "statement"
    epistemic: str = WIKI_EPISTEMIC
    tokens: TokenCheck = "not_run"
    nli_min: float | None = None

    def __post_init__(self) -> None:
        _text(self.cid, "claim cid", _CID_RE)
        _require(self.section in WIKI_SECTIONS, "field", "bad claim section")
        text = self.text
        _require(
            type(text) is str and bool(text.strip()) and len(text) <= MAX_CLAIM_CHARS,
            "field",
            "claim text must be 1 to 1000 characters",
        )
        _require(not _unsafe_text(text), "field", "claim text has a control or format character")
        citations = tuple(self.citations)
        object.__setattr__(self, "citations", citations)
        _require(bool(citations), "uncited_claim", "every claim cites at least one source")
        _require(len(citations) <= MAX_CITES_PER_CLAIM, "too_large", "too many citations")
        refs: list[str] = []
        for citation in citations:
            _require(
                type(citation) is Citation and citation.ref is not None,
                "field",
                "claim citations must be Citation values with a ref",
            )
            assert citation.ref is not None
            _require(citation.ref_kind != "wiki_page", "wiki_citation", "a page cannot cite a page")
            if citation.ref_kind == "memory":
                _require(
                    citation.version is not None,
                    "unversioned_memory",
                    "a memory citation needs mem:<id>@v<n>",
                )
            refs.append(citation.ref)
        _require(len(set(refs)) == len(refs), "duplicate", "a claim cites a source twice")
        _require(self.epistemic == WIKI_EPISTEMIC, "epistemic_cap", "claims are inference only")
        _require(self.causal in CAUSAL_VALUES, "field", "bad claim causal")
        _require(self.causal in WIKI_CAUSAL_VALUES, "causal_cap", "claims cap at hypothesis")
        _require(
            self.causal == "none" or self.section == "relations",
            "causal_outside_relations",
            "a causal claim belongs in relations",
        )
        _require(self.kind in CLAIM_KINDS, "field", "bad claim kind")
        _require(
            self.section in _KIND_SECTIONS[self.kind],
            "claim_kind_section",
            "claim kind not allowed in this section",
        )
        _require(self.tokens in TOKEN_CHECKS, "field", "bad token check result")
        if self.nli_min is not None:
            score = self.nli_min
            _require(
                type(score) in (int, float) and math.isfinite(score) and 0.0 <= score <= 1.0,
                "field",
                "nli score must be a number in [0, 1]",
            )
            object.__setattr__(self, "nli_min", float(score))

    def refs(self) -> tuple[str, ...]:
        return tuple(citation.ref for citation in self.citations if citation.ref is not None)

    def to_json(self) -> dict[str, object]:
        checks: dict[str, object] = {"tokens": self.tokens}
        if self.nli_min is not None:
            checks["nli"] = {"min": self.nli_min}
        return {
            "cid": self.cid,
            "text": self.text,
            "cites": list(self.refs()),
            "causal": self.causal,
            "kind": self.kind,
            "epistemic": self.epistemic,
            "checks": checks,
        }


_BUILD_FIELDS: Final[tuple[tuple[str, re.Pattern[str] | None], ...]] = (
    ("generator", None),
    ("generator_version", _TOKEN_64),
    ("model", _TOKEN_128),
    ("model_digest", _DIGEST_RE),
    ("prompt_version", _TOKEN_64),
    ("checker_version", _TOKEN_64),
    ("nli_model", _TOKEN_192),
    ("view_version", _TOKEN_128),
    ("job_digest", _DIGEST_RE),
)


@dataclass(frozen=True, slots=True)
class WikiBuild:
    """Who built the page with what: generator, model, prompt, checker, inputs."""

    generator: str
    generator_version: str
    model: str
    model_digest: str
    prompt_version: str
    checker_version: str
    nli_model: str
    view_version: str
    job_digest: str

    def __post_init__(self) -> None:
        for name, pattern in _BUILD_FIELDS:
            value = getattr(self, name)
            if pattern is None:
                try:
                    check_name(value, what=name)
                except ValueError as exc:
                    raise _fail("field", f"bad build {name}") from exc
            else:
                _text(value, f"build {name}", pattern)

    def to_json(self) -> dict[str, object]:
        return {name: getattr(self, name) for name, _ in _BUILD_FIELDS}


def wiki_sources_digest(sources: Sequence[WikiSource]) -> str:
    """``sha256:<hex>`` over the sorted ``[ref, sha256]`` pairs (``built_from_digest``)."""

    return _digest(sorted([source.ref, source.sha256] for source in sources))


@dataclass(frozen=True, slots=True)
class WikiPage:
    """A ``memfw-wiki-page/1`` page. Construction enforces every format rule.

    ``claims`` are in page order: grouped by section in ``WIKI_SECTIONS``
    order, ``summary`` first and never empty. ``privacy`` must equal the
    strictest source level and be ``public`` or ``shared``. ``page_key`` must
    be :func:`wiki_page_key` of ``(tenant, concept)`` and
    ``built_from_digest`` :func:`wiki_sources_digest` of ``sources``.
    """

    page_key: str
    concept: str
    tenant: str
    privacy: str
    build: WikiBuild
    sources: tuple[WikiSource, ...]
    claims: tuple[WikiClaim, ...]
    open_questions: tuple[str, ...] = ()
    built_from_digest: str = ""

    def __post_init__(self) -> None:
        _text(self.concept, "concept", _CONCEPT_RE)
        _text(self.tenant, "tenant", _TENANT_RE)
        _text(self.page_key, "page_key", WIKI_PAGE_KEY_RE)
        _require(
            self.page_key == wiki_page_key(self.tenant, self.concept),
            "page_key",
            "page_key is not wiki_page_key(tenant, concept)",
        )
        _require(type(self.build) is WikiBuild, "field", "build must be a WikiBuild")
        sources = tuple(self.sources)
        claims = tuple(self.claims)
        questions = tuple(self.open_questions)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "claims", claims)
        object.__setattr__(self, "open_questions", questions)
        _require(bool(sources), "field", "a page lists at least one source")
        _require(len(sources) <= MAX_WIKI_SOURCES, "too_large", "too many sources")
        _require(all(type(s) is WikiSource for s in sources), "field", "bad source value")
        by_ref = {source.ref: source for source in sources}
        _require(len(by_ref) == len(sources), "duplicate", "a source is listed twice")
        _require(len(questions) <= MAX_OPEN_QUESTIONS, "too_large", "too many open questions")
        for question in questions:
            _text(question, "open question id", MEMORY_ID_RE)
        _require(len(set(questions)) == len(questions), "duplicate", "open question listed twice")
        self._check_claims(claims, by_ref)
        level = strictest(source.privacy for source in sources)
        _require(self.privacy in PRIVACY_LEVELS, "field", "bad page privacy")
        _require(
            self.privacy == level,
            "privacy_not_strictest",
            "page privacy must equal the strictest source level",
        )
        _require(
            level in RELEASABLE_PRIVACY,
            "privacy_not_releasable",
            "a page with a local-private source is never built",
        )
        _require(
            self.built_from_digest == wiki_sources_digest(sources),
            "digest_mismatch",
            "built_from_digest is not the digest of sources",
        )
        _require(
            len(canonical_json(self.to_json())) <= MAX_WIKI_PAGE_BYTES,
            "too_large",
            "page exceeds 16 KiB as canonical JSON",
        )

    @staticmethod
    def _check_claims(claims: tuple[WikiClaim, ...], by_ref: Mapping[str, WikiSource]) -> None:
        _require(all(type(c) is WikiClaim for c in claims), "field", "bad claim value")
        order = [WIKI_SECTIONS.index(claim.section) for claim in claims]
        _require(order == sorted(order), "section_order", "claims must be in section order")
        _require(
            bool(claims) and claims[0].section == "summary",
            "summary_missing",
            "a page needs at least one summary claim",
        )
        cids = [claim.cid for claim in claims]
        _require(len(set(cids)) == len(cids), "duplicate", "claim cid used twice")
        for section in WIKI_SECTIONS:
            count = sum(1 for claim in claims if claim.section == section)
            _require(count <= MAX_CLAIMS_PER_SECTION, "too_large", "too many claims in a section")
        for claim in claims:
            cited: list[WikiSource] = []
            for ref in claim.refs():
                source = by_ref.get(ref)
                _require(source is not None, "unknown_citation", "claim cites an unlisted source")
                assert source is not None
                cited.append(source)
            ceiling = min(
                max(_CAUSAL_RANK[source.causal] for source in cited),
                _CAUSAL_RANK[WIKI_CAUSAL_CAP],
            )
            _require(
                _CAUSAL_RANK[claim.causal] <= ceiling,
                "causal_above_source",
                "claim causal grade exceeds its cited sources",
            )
            for source in cited:
                if source.status == "current":
                    continue
                allowed = claim.section == "timeline" or (
                    source.status == "disputed" and claim.section == "open"
                )
                _require(
                    allowed,
                    "inactive_source_section",
                    "a non-current source may be cited only in timeline (disputed: open)",
                )
                _require(
                    claim.section != "timeline" or claim.kind == "superseded",
                    "inactive_source_kind",
                    "a timeline claim citing a non-current source is kind superseded",
                )

    def section_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for claim in self.claims:
            if claim.section not in seen:
                seen.append(claim.section)
        return tuple(seen)

    def to_json(self) -> dict[str, object]:
        """The ``memfw-wiki-page/1`` document (parse it back with :func:`parse_wiki_page`)."""

        return {
            "format": WIKI_PAGE_FORMAT,
            "page_key": self.page_key,
            "concept": self.concept,
            "tenant": self.tenant,
            "privacy": self.privacy,
            "build": self.build.to_json(),
            "sources": [source.to_json() for source in self.sources],
            "sections": [
                {
                    "id": section,
                    "claims": [c.to_json() for c in self.claims if c.section == section],
                }
                for section in self.section_ids()
            ],
            "open_questions": list(self.open_questions),
            "built_from_digest": self.built_from_digest,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.to_json())

    def page_digest(self) -> str:
        """``sha256:<hex>`` over :meth:`canonical_bytes` (a job's ``previous_page`` hash)."""

        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


# --- parsing ---------------------------------------------------------------------


def _load(data: object, limit: int) -> Mapping[str, Any]:
    if isinstance(data, (bytes, bytearray)):
        _require(len(data) <= limit, "too_large", "document is too large")
        try:
            data = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail("format", "document is not UTF-8") from exc
    if isinstance(data, str):
        _require(len(data.encode("utf-8")) <= limit, "too_large", "document is too large")
        _require(_json_depth(data) <= MAX_JSON_DEPTH, "too_deep", "document is nested too deeply")
        try:
            data = json.loads(data, parse_constant=_no_constant, object_pairs_hook=_no_duplicates)
        except ValueError as exc:
            if isinstance(exc, WikiFormatError):
                raise
            raise _fail("format", "document is not JSON") from exc
        except RecursionError as exc:
            raise _fail("too_deep", "document is nested too deeply") from exc
    _require(isinstance(data, Mapping), "format", "document must be a JSON object")
    return data  # type: ignore[return-value]


def _json_depth(text: str) -> int:
    """Deepest ``[``/``{`` nesting in JSON text, outside strings (a pre-scan, no recursion)."""

    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
                if deepest > MAX_JSON_DEPTH:
                    return deepest
        elif char in "]}":
            depth -= 1
    return deepest


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("format", "duplicate key in a JSON object")
        result[key] = value
    return result


def _no_constant(name: str) -> object:
    raise _fail("format", "NaN and Infinity are not JSON")


def _object(
    value: object, what: str, keys: set[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), "field", f"{what} must be an object")
    assert isinstance(value, Mapping)
    present = set(value)
    _require(all(type(k) is str for k in present), "field", f"{what} keys must be strings")
    _require(keys - optional <= present, "field", f"{what} misses a required key")
    _require(present <= keys, "field", f"{what} has an unknown key")
    return value


def _list(value: object, what: str) -> list[Any]:
    _require(isinstance(value, list), "field", f"{what} must be a list")
    return value  # type: ignore[return-value]


_SOURCE_KEYS: Final = {"ref", "kind", "sha256", "privacy", "causal", "status", "superseded_by"}
_SOURCE_OPTIONAL: Final = frozenset({"superseded_by"})
_CLAIM_KEYS: Final = {"cid", "text", "cites", "causal", "kind", "epistemic", "checks"}
_PAGE_KEYS: Final = {
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


def _source(raw: object) -> WikiSource:
    item = _object(raw, "source", _SOURCE_KEYS, _SOURCE_OPTIONAL)
    return WikiSource(
        ref=item["ref"],
        kind=item["kind"],
        sha256=item["sha256"],
        privacy=item["privacy"],
        causal=item["causal"],
        status=item["status"],
        superseded_by=item.get("superseded_by"),
    )


def _citation(ref: object) -> Citation:
    try:
        return Citation.from_ref(ref)  # type: ignore[arg-type]
    except CitationRefError as exc:
        if exc.code != "malformed":
            raise _ref_error(exc, "claim cite") from exc
        if isinstance(ref, str) and ref.startswith("wiki:"):
            raise _fail("wiki_citation", "a page cannot cite a page") from exc
        raise _fail("unknown_citation", "claim cite is not a citation ref") from exc
    except ValueError as exc:
        raise _fail("unknown_citation", "claim cite is not a citation ref") from exc


def _claim(raw: object, section: str) -> WikiClaim:
    item = _object(raw, "claim", _CLAIM_KEYS)
    checks = _object(item["checks"], "claim checks", {"tokens", "nli"}, frozenset({"nli"}))
    nli: object = None
    if "nli" in checks:
        nli_raw = _object(checks["nli"], "claim nli check", {"min"})
        nli = nli_raw["min"]
        _require(type(nli) in (int, float), "field", "nli score must be a number")
    cites = _list(item["cites"], "claim cites")
    return WikiClaim(
        cid=item["cid"],
        section=section,  # type: ignore[arg-type]
        text=item["text"],
        citations=tuple(_citation(ref) for ref in cites),
        causal=item["causal"],
        kind=item["kind"],
        epistemic=item["epistemic"],
        tokens=checks["tokens"],
        nli_min=nli,  # type: ignore[arg-type]
    )


def _page(data: Mapping[str, Any]) -> WikiPage:
    doc = _object(data, "page", _PAGE_KEYS)
    _require(doc["format"] == WIKI_PAGE_FORMAT, "format", "format must be memfw-wiki-page/1")
    build = _object(doc["build"], "build", {name for name, _ in _BUILD_FIELDS})
    claims: list[WikiClaim] = []
    seen: set[str] = set()
    for raw in _list(doc["sections"], "sections"):
        section = _object(raw, "section", {"id", "claims"})
        sid = section["id"]
        _require(sid in WIKI_SECTIONS, "field", "unknown section id")
        _require(sid not in seen, "duplicate", "section listed twice")
        seen.add(sid)
        items = _list(section["claims"], "section claims")
        _require(bool(items), "field", "a listed section has at least one claim")
        claims.extend(_claim(item, sid) for item in items)
    return WikiPage(
        page_key=doc["page_key"],
        concept=doc["concept"],
        tenant=doc["tenant"],
        privacy=doc["privacy"],
        build=WikiBuild(**{name: build[name] for name, _ in _BUILD_FIELDS}),
        sources=tuple(_source(item) for item in _list(doc["sources"], "sources")),
        claims=tuple(claims),
        open_questions=tuple(_list(doc["open_questions"], "open_questions")),
        built_from_digest=doc["built_from_digest"],
    )


def _wrap(error: Exception) -> WikiFormatError:
    if isinstance(error, WikiFormatError):
        return error
    return _fail("field", "malformed value")


def parse_wiki_page(data: Mapping[str, Any] | str | bytes) -> WikiPage:
    """Read a ``memfw-wiki-page/1`` document. Raises :class:`WikiFormatError`."""

    try:
        return _page(_load(data, MAX_WIKI_PAGE_BYTES * 4))
    except RecursionError as exc:
        raise _fail("too_deep", "document is nested too deeply") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise _wrap(exc) from exc


# --- jobs ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WikiJobSource:
    """A released source in a job: the page-source fields plus its released text.

    The job builder cuts a long text with :func:`truncate_source_text` and
    records ``truncated=True`` and ``full_sha256`` (of the whole text); the
    parser never truncates and refuses a text over 8000 characters.
    """

    source: WikiSource
    text: str
    truncated: bool = False
    full_sha256: str | None = None

    def __post_init__(self) -> None:
        _require(type(self.source) is WikiSource, "field", "bad job source")
        _require(
            self.source.privacy in RELEASABLE_PRIVACY,
            "privacy_not_releasable",
            "a job carries released (public/shared) sources only",
        )
        _require(
            type(self.text) is str
            and bool(self.text.strip())
            and len(self.text) <= MAX_JOB_SOURCE_CHARS,
            "field",
            "job source text must be 1 to 8000 characters",
        )
        _require(type(self.truncated) is bool, "field", "truncated must be a bool")
        if self.truncated:
            _text(self.full_sha256, "full_sha256", _SHA256_RE)
        else:
            _require(self.full_sha256 is None, "field", "full_sha256 needs truncated")

    def digest_fields(self) -> dict[str, object]:
        """Everything but the text (its hash is the source's ``sha256``)."""

        out = self.source.to_json()
        if self.truncated:
            out["truncated"] = True
            out["full_sha256"] = self.full_sha256
        return out

    def to_json(self) -> dict[str, object]:
        return {**self.digest_fields(), "text": self.text}


def truncate_source_text(text: str) -> tuple[str, bool, str | None]:
    """The job builder's fixed cut: ``(text, truncated, full_sha256)``.

    A text of at most ``MAX_JOB_SOURCE_CHARS`` code points is kept whole. A
    longer one keeps its first ``MAX_JOB_SOURCE_CHARS`` code points, cut back
    to the last newline inside them when that newline lies past half the limit
    (the newline is dropped); a cut that leaves only blank text falls back to
    the hard cut. It reports the sha256 of the whole text.
    """

    if len(text) <= MAX_JOB_SOURCE_CHARS:
        return text, False, None
    hard = text[:MAX_JOB_SOURCE_CHARS]
    head = hard
    cut = hard.rfind("\n")
    if cut > MAX_JOB_SOURCE_CHARS // 2:
        head = hard[:cut]
    if not head.strip():
        head = hard
    return head, True, hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WikiOpenQuestion:
    """An existing ``open-question`` record, as the job shows it to the generator."""

    id: str
    question: str
    missing: str = ""
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "open question id", MEMORY_ID_RE)
        for value, what in ((self.question, "question"), (self.missing, "missing")):
            _require(
                type(value) is str and len(value) <= MAX_JOB_TEXT_CHARS,
                "field",
                f"bad open question {what}",
            )
        _require(bool(self.question.strip()), "field", "open question text required")
        conflicts = tuple(self.conflicts)
        object.__setattr__(self, "conflicts", conflicts)
        for memory_id in conflicts:
            _text(memory_id, "conflicting memory id", MEMORY_ID_RE)

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "question": self.question,
            "missing": self.missing,
            "conflicts": list(self.conflicts),
        }


_GENERATOR_FIELDS: Final = (
    "generator",
    "generator_version",
    "model",
    "model_digest",
    "prompt_version",
)


@dataclass(frozen=True, slots=True)
class WikiConcept:
    """The concept a page is about: oid, labels and definition (no path)."""

    oid: str
    pref_label: str
    alt_labels: tuple[str, ...] = ()
    definition: str = ""

    def __post_init__(self) -> None:
        _text(self.oid, "concept oid", _CONCEPT_RE)
        labels = tuple(self.alt_labels)
        object.__setattr__(self, "alt_labels", labels)
        _require(len(labels) <= MAX_JOB_LABELS, "too_large", "too many labels")
        for label in (self.pref_label, *labels):
            _require(
                type(label) is str and bool(label.strip()) and len(label) <= 200,
                "field",
                "concept labels are 1 to 200 characters",
            )
            _require(not _unsafe_text(label), "field", "label has a control or format character")
        _require(
            type(self.definition) is str and len(self.definition) <= MAX_JOB_TEXT_CHARS,
            "field",
            "bad concept definition",
        )

    def to_json(self) -> dict[str, object]:
        return {
            "oid": self.oid,
            "pref_label": self.pref_label,
            "alt_labels": list(self.alt_labels),
            "definition": self.definition,
        }


def wiki_job_digest(
    *,
    concept: WikiConcept,
    tenant: str,
    sources: Sequence[WikiJobSource],
    open_questions: Sequence[WikiOpenQuestion],
    previous_page: WikiPage | None,
    generator: Mapping[str, str],
    checker_version: str,
) -> str:
    """``sha256:<hex>`` identifying one unit of work (design 5.3).

    Over the concept (oid, labels and definition: a rename regenerates), every
    source's fields (ref, kind, hash, privacy, causal grade, status,
    successor, truncation; the text is covered by its hash), the open
    questions, the approved page it replaces, the generator configuration and
    the checker version. The same inputs give the same digest, so a
    resubmitted job maps to the same candidate; any change that could change
    the page gives a new one. The view version is deliberately left out: a
    new view with the same sources is the same work.
    """

    return _digest(
        {
            "concept": concept.to_json(),
            "tenant": tenant,
            "sources": sorted(
                (item.digest_fields() for item in sources), key=lambda fields: str(fields["ref"])
            ),
            "open_questions": sorted(_digest(question.to_json()) for question in open_questions),
            "previous_page": None if previous_page is None else previous_page.page_digest(),
            "generator": {name: generator[name] for name in _GENERATOR_FIELDS},
            "checker_version": checker_version,
        }
    )


@dataclass(frozen=True, slots=True)
class WikiJob:
    """A ``memfw-wiki-job/1`` work package. Construction enforces the format."""

    job_id: str
    job_digest: str
    tenant: str
    concept: WikiConcept
    sources: tuple[WikiJobSource, ...]
    generator: Mapping[str, str]
    checker_version: str
    view_version: str
    open_questions: tuple[WikiOpenQuestion, ...] = ()
    previous_page: WikiPage | None = None

    def __post_init__(self) -> None:
        _text(self.job_id, "job_id", _TOKEN_64)
        _text(self.job_digest, "job_digest", _DIGEST_RE)
        _text(self.tenant, "tenant", _TENANT_RE)
        _require(type(self.concept) is WikiConcept, "field", "bad job concept")
        sources = tuple(self.sources)
        questions = tuple(self.open_questions)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "open_questions", questions)
        _require(bool(sources), "field", "a job has at least one source")
        _require(len(sources) <= MAX_WIKI_SOURCES, "too_large", "too many sources")
        _require(all(type(s) is WikiJobSource for s in sources), "field", "bad job source")
        refs = [item.source.ref for item in sources]
        _require(len(set(refs)) == len(refs), "duplicate", "a source is listed twice")
        _require(len(questions) <= MAX_OPEN_QUESTIONS, "too_large", "too many open questions")
        _require(all(type(q) is WikiOpenQuestion for q in questions), "field", "bad question")
        ids = [question.id for question in questions]
        _require(len(set(ids)) == len(ids), "duplicate", "open question listed twice")
        generator = dict(self.generator) if isinstance(self.generator, Mapping) else None
        _require(
            generator is not None and set(generator) == set(_GENERATOR_FIELDS),
            "field",
            "generator needs exactly the generator fields",
        )
        assert generator is not None
        object.__setattr__(self, "generator", generator)
        patterns = dict(_BUILD_FIELDS)
        for name in _GENERATOR_FIELDS:
            pattern = patterns[name]
            if pattern is None:
                try:
                    check_name(generator[name], what=name)
                except ValueError as exc:
                    raise _fail("field", f"bad generator {name}") from exc
            else:
                _text(generator[name], f"generator {name}", pattern)
        _text(self.checker_version, "checker_version", _TOKEN_64)
        _text(self.view_version, "view_version", _TOKEN_128)
        previous = self.previous_page
        if previous is not None:
            _require(type(previous) is WikiPage, "field", "previous_page must be a WikiPage")
            _require(
                previous.concept == self.concept.oid and previous.tenant == self.tenant,
                "field",
                "previous_page is about another concept or tenant",
            )
        expected = wiki_job_digest(
            concept=self.concept,
            tenant=self.tenant,
            sources=sources,
            open_questions=questions,
            previous_page=previous,
            generator=generator,
            checker_version=self.checker_version,
        )
        _require(self.job_digest == expected, "digest_mismatch", "job_digest does not match")
        _require(
            len(canonical_json(self.to_json())) <= MAX_JOB_BYTES,
            "too_large",
            "job exceeds 2 MiB as canonical JSON",
        )

    @property
    def page_key(self) -> str:
        return wiki_page_key(self.tenant, self.concept.oid)

    def to_json(self) -> dict[str, object]:
        return {
            "format": WIKI_JOB_FORMAT,
            "job_id": self.job_id,
            "job_digest": self.job_digest,
            "tenant": self.tenant,
            "page_key": self.page_key,
            "concept": self.concept.to_json(),
            "sources": [item.to_json() for item in self.sources],
            "open_questions": [question.to_json() for question in self.open_questions],
            "previous_page": None if self.previous_page is None else self.previous_page.to_json(),
            "generator": dict(self.generator),
            "checker_version": self.checker_version,
            "view_version": self.view_version,
        }


_JOB_KEYS: Final = {
    "format",
    "job_id",
    "job_digest",
    "tenant",
    "page_key",
    "concept",
    "sources",
    "open_questions",
    "previous_page",
    "generator",
    "checker_version",
    "view_version",
}


def _job(data: Mapping[str, Any]) -> WikiJob:
    doc = _object(data, "job", _JOB_KEYS)
    _require(doc["format"] == WIKI_JOB_FORMAT, "format", "format must be memfw-wiki-job/1")
    concept_raw = _object(
        doc["concept"], "concept", {"oid", "pref_label", "alt_labels", "definition"}
    )
    concept = WikiConcept(
        oid=concept_raw["oid"],
        pref_label=concept_raw["pref_label"],
        alt_labels=tuple(_list(concept_raw["alt_labels"], "alt_labels")),
        definition=concept_raw["definition"],
    )
    sources = []
    for raw in _list(doc["sources"], "sources"):
        item = _object(
            raw,
            "job source",
            _SOURCE_KEYS | {"text", "truncated", "full_sha256"},
            _SOURCE_OPTIONAL | {"truncated", "full_sha256"},
        )
        _require(item.get("truncated", True) is True, "field", "truncated is true when present")
        sources.append(
            WikiJobSource(
                source=_source({k: item[k] for k in _SOURCE_KEYS if k in item}),
                text=item["text"],
                truncated="truncated" in item,
                full_sha256=item.get("full_sha256"),
            )
        )
    questions = []
    for raw in _list(doc["open_questions"], "open_questions"):
        item = _object(raw, "open question", {"id", "question", "missing", "conflicts"})
        questions.append(
            WikiOpenQuestion(
                id=item["id"],
                question=item["question"],
                missing=item["missing"],
                conflicts=tuple(_list(item["conflicts"], "conflicts")),
            )
        )
    previous = doc["previous_page"]
    _require(
        previous is None or isinstance(previous, Mapping),
        "field",
        "previous_page is null or a page object",
    )
    generator = _object(doc["generator"], "generator", set(_GENERATOR_FIELDS))
    job = WikiJob(
        job_id=doc["job_id"],
        job_digest=doc["job_digest"],
        tenant=doc["tenant"],
        concept=concept,
        sources=tuple(sources),
        generator=dict(generator),
        checker_version=doc["checker_version"],
        view_version=doc["view_version"],
        open_questions=tuple(questions),
        previous_page=None if previous is None else _page(_load(previous, MAX_JOB_BYTES)),
    )
    _require(doc["page_key"] == job.page_key, "page_key", "page_key does not match the concept")
    return job


def parse_wiki_job(data: Mapping[str, Any] | str | bytes) -> WikiJob:
    """Read a ``memfw-wiki-job/1`` document. Raises :class:`WikiFormatError`."""

    try:
        return _job(_load(data, MAX_JOB_BYTES))
    except RecursionError as exc:
        raise _fail("too_deep", "document is nested too deeply") from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise _wrap(exc) from exc


# --- JSON Schema -----------------------------------------------------------------


def _string(pattern: str | None = None, *, enum: Sequence[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string"}
    if pattern is not None:
        out["pattern"] = pattern
    if enum is not None:
        out["enum"] = list(enum)
    return out


_REF_PATTERN: Final = r"^(mem:[A-Za-z0-9][A-Za-z0-9_-]{0,127}@v[1-9][0-9]{0,8}|kh:.+|att:prx-.+)$"
_SOURCE_PROPERTIES: Final[dict[str, Any]] = {
    "ref": _string(_REF_PATTERN),
    "kind": _string(enum=WIKI_SOURCE_KINDS),
    "sha256": _string(_SHA256_RE.pattern),
    "privacy": _string(enum=PRIVACY_LEVELS),
    "causal": _string(enum=CAUSAL_VALUES),
    "status": _string(enum=SOURCE_STATUSES),
    "superseded_by": _string(r"^(mem:|kh:|att:)."),
}


def _closed(properties: dict[str, Any], optional: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": [key for key in properties if key not in optional],
        "additionalProperties": False,
    }


def _page_schema() -> dict[str, Any]:
    claim = _closed(
        {
            "cid": _string(_CID_RE.pattern),
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_CLAIM_CHARS},
            "cites": {
                "type": "array",
                "items": _string(_REF_PATTERN),
                "minItems": 1,
                "maxItems": MAX_CITES_PER_CLAIM,
            },
            "causal": _string(enum=WIKI_CAUSAL_VALUES),
            "kind": _string(enum=CLAIM_KINDS),
            "epistemic": {"const": WIKI_EPISTEMIC},
            "checks": _closed(
                {
                    "tokens": _string(enum=TOKEN_CHECKS),
                    "nli": _closed({"min": {"type": "number", "minimum": 0, "maximum": 1}}),
                },
                optional=("nli",),
            ),
        }
    )
    build = _closed(
        {
            name: _string(r"^[a-z][a-z0-9_-]{0,63}$" if pattern is None else pattern.pattern)
            for name, pattern in _BUILD_FIELDS
        }
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:memfw:schema:memfw-wiki-page:1",
        "title": WIKI_PAGE_FORMAT,
        "description": (
            "Structural rules only. A page is valid only when memfw.ports.wiki."
            "parse_wiki_page accepts it: it also checks that cites resolve to sources, "
            "privacy is the strictest source level (public or shared), each claim's causal "
            "grade is at most its sources' (capped at hypothesis), non-current sources "
            "appear only in timeline, section order, page_key and built_from_digest."
        ),
        **_closed(
            {
                "format": {"const": WIKI_PAGE_FORMAT},
                "page_key": _string(WIKI_PAGE_KEY_RE.pattern),
                "concept": _string(_CONCEPT_RE.pattern),
                "tenant": {"type": "string", "maxLength": 128},
                "privacy": _string(enum=RELEASABLE_PRIVACY),
                "build": build,
                "sources": {
                    "type": "array",
                    "items": _closed(dict(_SOURCE_PROPERTIES), optional=("superseded_by",)),
                    "minItems": 1,
                    "maxItems": MAX_WIKI_SOURCES,
                },
                "sections": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": len(WIKI_SECTIONS),
                    "items": _closed(
                        {
                            "id": _string(enum=WIKI_SECTIONS),
                            "claims": {
                                "type": "array",
                                "items": claim,
                                "minItems": 1,
                                "maxItems": MAX_CLAIMS_PER_SECTION,
                            },
                        }
                    ),
                },
                "open_questions": {
                    "type": "array",
                    "items": _string(MEMORY_ID_RE.pattern),
                    "maxItems": MAX_OPEN_QUESTIONS,
                },
                "built_from_digest": _string(_DIGEST_RE.pattern),
            }
        ),
    }


def _job_schema() -> dict[str, Any]:
    page = _page_schema()
    for key in ("$schema", "$id"):
        page.pop(key)
    source = _closed(
        {
            **_SOURCE_PROPERTIES,
            "privacy": _string(enum=RELEASABLE_PRIVACY),
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_JOB_SOURCE_CHARS},
            "truncated": {"const": True},
            "full_sha256": _string(_SHA256_RE.pattern),
        },
        optional=("superseded_by", "truncated", "full_sha256"),
    )
    label = {"type": "string", "minLength": 1, "maxLength": 200}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:memfw:schema:memfw-wiki-job:1",
        "title": WIKI_JOB_FORMAT,
        "description": (
            "Structural rules only. A job is valid only when memfw.ports.wiki."
            "parse_wiki_job accepts it: it also checks job_digest, page_key, and that "
            "previous_page is a valid page about the same concept and tenant."
        ),
        "$defs": {"page": page},
        **_closed(
            {
                "format": {"const": WIKI_JOB_FORMAT},
                "job_id": _string(_TOKEN_64.pattern),
                "job_digest": _string(_DIGEST_RE.pattern),
                "tenant": {"type": "string", "maxLength": 128},
                "page_key": _string(WIKI_PAGE_KEY_RE.pattern),
                "concept": _closed(
                    {
                        "oid": _string(_CONCEPT_RE.pattern),
                        "pref_label": label,
                        "alt_labels": {
                            "type": "array",
                            "items": label,
                            "maxItems": MAX_JOB_LABELS,
                        },
                        "definition": {"type": "string", "maxLength": MAX_JOB_TEXT_CHARS},
                    }
                ),
                "sources": {
                    "type": "array",
                    "items": source,
                    "minItems": 1,
                    "maxItems": MAX_WIKI_SOURCES,
                },
                "open_questions": {
                    "type": "array",
                    "maxItems": MAX_OPEN_QUESTIONS,
                    "items": _closed(
                        {
                            "id": _string(MEMORY_ID_RE.pattern),
                            "question": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_JOB_TEXT_CHARS,
                            },
                            "missing": {"type": "string", "maxLength": MAX_JOB_TEXT_CHARS},
                            "conflicts": {
                                "type": "array",
                                "items": _string(MEMORY_ID_RE.pattern),
                            },
                        }
                    ),
                },
                "previous_page": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/page"}]},
                "generator": _closed(
                    {
                        name: page["properties"]["build"]["properties"][name]
                        for name in _GENERATOR_FIELDS
                    }
                ),
                "checker_version": _string(_TOKEN_64.pattern),
                "view_version": _string(_TOKEN_128.pattern),
            }
        ),
    }


WIKI_PAGE_SCHEMA: Final[Mapping[str, Any]] = _page_schema()
WIKI_JOB_SCHEMA: Final[Mapping[str, Any]] = _job_schema()


def wiki_page_schema() -> dict[str, Any]:
    """A fresh copy of the ``memfw-wiki-page/1`` JSON Schema."""

    return copy.deepcopy(dict(WIKI_PAGE_SCHEMA))


def wiki_job_schema() -> dict[str, Any]:
    """A fresh copy of the ``memfw-wiki-job/1`` JSON Schema."""

    return copy.deepcopy(dict(WIKI_JOB_SCHEMA))


__all__ = [
    "CLAIM_KINDS",
    "MAX_JOB_SOURCE_CHARS",
    "MAX_JSON_DEPTH",
    "MAX_WIKI_PAGE_BYTES",
    "RELEASABLE_PRIVACY",
    "SOURCE_STATUSES",
    "WIKI_CAUSAL_CAP",
    "WIKI_EPISTEMIC",
    "WIKI_ERROR_CODES",
    "WIKI_JOB_FORMAT",
    "WIKI_JOB_SCHEMA",
    "WIKI_PAGE_FORMAT",
    "WIKI_PAGE_SCHEMA",
    "WIKI_SECTIONS",
    "WIKI_SOURCE_KINDS",
    "ClaimKind",
    "SourceStatus",
    "WikiBuild",
    "WikiClaim",
    "WikiConcept",
    "WikiFormatError",
    "WikiJob",
    "WikiJobSource",
    "WikiOpenQuestion",
    "WikiPage",
    "WikiSection",
    "WikiSource",
    "canonical_json",
    "parse_wiki_job",
    "parse_wiki_page",
    "truncate_source_text",
    "wiki_job_digest",
    "wiki_job_schema",
    "wiki_page_key",
    "wiki_page_schema",
    "wiki_sources_digest",
]
