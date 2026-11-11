"""Operation ①: verify memories against the transcript index.

For each cited memory, search ``memfw history`` for entries about the same
thing and classify each one as supporting, contradicting or neutral. The
outcome is a candidate:

* **confirm** -- only supporting evidence;
* **correct** -- the newest evidence contradicts the memory and offers one
  replacement value (``18`` -> ``21``);
* **mark-disputed** -- contradicting evidence without a clear replacement,
  or support and contradiction together.

Matching is deterministic and lexical: shared context words plus the values
(numbers, versions) a memory states. A local judge callable can replace the
per-entry classification; none is used by default. Transcript text is only
read through the history index (redacted, secrets withheld), is treated as
data, and never leaves this module: candidates carry opaque refs only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from memfw.core.models import MemoryDocument
from memfw.history import HistoryError

from .model import Candidate, EvidenceRef, Polarity, concept_key

Judge = Callable[[str, str], str]
"""Optional local hook: ``judge(memory_text, evidence_text) -> polarity``."""

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_'-]{2,}|[一-鿿]{2,}")
_VALUE = re.compile(r"(?<![\w.])v?\d+(?:\.\d+)*(?![\w.]*\d)")
_MARKER = re.compile(r"\[(?:REDACTED|withheld)[^\]]*\]")
_NEGATION = re.compile(
    r"(?i)\b(?:not|no longer|never|isn't|wasn't|aren't|incorrect|wrong|outdated|"
    r"obsolete|deprecated|changed to|moved to)\b"
)
_STOP = frozenset(
    "the and for with that this from are was were has have had but not you your our its "
    "into onto than then them they their there here what when where which who will would "
    "should could can may might also just very more most some any all each per via use "
    "used using about after before over under been being does did done".split()
)
RELEVANCE = 0.5
"""Share of a memory's context words an entry must contain to count."""
SUPPORT_WITHOUT_VALUES = 0.7
"""Stricter relevance needed to support a memory that states no values."""


SearchIndex = Any
"""A ``TranscriptIndex``, or anything with its ``search`` and ``read`` methods."""


@dataclass
class VerifyStats:
    memories_checked: int = 0
    memories_without_evidence: int = 0
    entries_read: int = 0
    sessions: list[str] = field(default_factory=list)
    judge_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "memories_checked": self.memories_checked,
            "memories_without_evidence": self.memories_without_evidence,
            "entries_read": self.entries_read,
            "sessions_read": len(self.sessions),
            "session_ids": list(self.sessions),
            "judge_errors": self.judge_errors,
        }


def _clean(text: str) -> str:
    return _MARKER.sub(" ", text)


def context_words(text: str) -> set[str]:
    """Lowercased content words, without the values."""

    words = {word.lower() for word in _WORD.findall(_clean(text))}
    return {word for word in words if word not in _STOP and not _VALUE.fullmatch(word)}


def values(text: str) -> set[str]:
    """Numbers and versions a text states (``18``, ``3.2``, ``v1.4.0``)."""

    return {value.lstrip("v") for value in _VALUE.findall(_clean(text))}


def classify(memory_text: str, evidence_text: str) -> tuple[Polarity, set[str]]:
    """Polarity of one transcript entry for one memory, plus replacement values."""

    memory_words = context_words(memory_text)
    evidence_words = context_words(evidence_text)
    if not memory_words:
        return "neutral", set()
    shared = memory_words & evidence_words
    relevance = len(shared) / len(memory_words)
    if len(shared) < 2 or relevance < RELEVANCE:
        return "neutral", set()
    stated = values(memory_text)
    seen = values(evidence_text)
    negated = bool(_NEGATION.search(evidence_text))
    if stated:
        if stated <= seen and not negated:
            return "support", set()
        if seen and not stated & seen:
            return "contradict", seen
        if stated <= seen and negated:
            return "contradict", seen - stated
        return "neutral", set()
    if negated:
        return "contradict", set()
    if relevance >= SUPPORT_WITHOUT_VALUES:
        return "support", set()
    return "neutral", set()


def is_current(document: MemoryDocument) -> bool:
    return document.valid_to is None and document.superseded_by is None


def cited(documents: Sequence[MemoryDocument]) -> list[MemoryDocument]:
    """Memories that are referenced, linked to a concept, linked from another
    memory, or verified before. Most-referenced first, then by id."""

    linked = {target for document in documents for target in document.related}
    chosen = [
        document
        for document in documents
        if document.reference_count > 0
        or document.about
        or document.applies_to
        or document.id in linked
        or document.status != "unconfirmed"
    ]
    return sorted(chosen, key=lambda document: (-document.reference_count, document.id))


def _query(document: MemoryDocument, focus: str) -> str:
    words = sorted(context_words(f"{document.title} {document.body}"))
    # Keep the query short: title words first, then body words.
    title = sorted(context_words(document.title))
    ordered = title + [word for word in words if word not in title]
    extra = sorted(context_words(focus))
    return " ".join([*ordered[:12], *extra[:4]])


def _replace_value(body: str, old: str, new: str) -> str | None:
    pattern = re.compile(r"(?<![\w.])(v?)" + re.escape(old) + r"(?![\w.]*\d)")
    replaced, count = pattern.subn(lambda match: match.group(1) + new, body)
    return replaced if count else None


def verify(
    documents: Sequence[MemoryDocument],
    index: SearchIndex | None,
    *,
    focus: str = "",
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
    max_sessions: int = 20,
    hits_per_memory: int = 10,
    judge: Judge | None = None,
) -> tuple[list[Candidate], VerifyStats]:
    """Run operation ① over ``documents`` (already filtered to cited, current ones)."""

    stats = VerifyStats()
    candidates: list[Candidate] = []
    if index is None:
        return candidates, stats
    allowed: list[str] = []
    for document in documents:
        stats.memories_checked += 1
        memory_text = f"{document.title}\n{document.body}"
        try:
            hits = index.search(
                _query(document, focus),
                source=source,
                since=since,
                until=until,
                limit=hits_per_memory,
            )["results"]
        except HistoryError:
            hits = []
        evidence: list[tuple[str, EvidenceRef, set[str]]] = []
        for hit in sorted(hits, key=lambda item: (item.get("timestamp") or "", item["ref"])):
            session = str(hit.get("session_id") or "")
            if session not in allowed:
                if len(allowed) >= max_sessions:
                    continue
                allowed.append(session)
            try:
                window = index.read(hit["ref"], before=0, after=0)
            except HistoryError:
                continue
            anchors = [entry for entry in window["entries"] if entry.get("anchor")]
            if not anchors:
                continue
            text = str(anchors[0].get("text") or "")
            if not text or text.startswith("[withheld"):
                continue
            stats.entries_read += 1
            polarity, replacement = classify(memory_text, text)
            if judge is not None:
                try:
                    verdict = judge(memory_text, text)
                    if verdict in {"support", "contradict", "neutral"}:
                        polarity = verdict  # type: ignore[assignment]
                except Exception:  # noqa: BLE001 - a local hook must not break the dream
                    stats.judge_errors += 1
            if polarity == "neutral":
                continue
            timestamp = hit.get("timestamp")
            evidence.append(
                (
                    str(timestamp or ""),
                    EvidenceRef(
                        ref=str(hit["ref"]),
                        polarity=polarity,
                        session_id=session,
                        timestamp=str(timestamp) if timestamp else None,
                    ),
                    replacement,
                )
            )
        candidate = _decide(document, evidence)
        if candidate is None:
            if not evidence:
                stats.memories_without_evidence += 1
            continue
        candidates.append(candidate)
    stats.sessions = sorted(session for session in allowed if session)
    return candidates, stats


def _decide(
    document: MemoryDocument, evidence: list[tuple[str, EvidenceRef, set[str]]]
) -> Candidate | None:
    if not evidence:
        return None
    evidence.sort(key=lambda item: (item[0], item[1].ref))
    refs = [item[1] for item in evidence]
    supports = [item for item in evidence if item[1].polarity == "support"]
    contradicts = [item for item in evidence if item[1].polarity == "contradict"]
    oid = concept_key(document)
    ref_strings = sorted({item.ref for item in refs})
    if not contradicts:
        if document.status == "confirmed" and set(document.evidence) >= set(ref_strings):
            return None
        return Candidate(
            operation="verify",
            action="confirm",
            memory_ids=[document.id],
            oid=oid,
            reason=f"{len(supports)} transcript entr{'y' if len(supports) == 1 else 'ies'} "
            "support this memory",
            evidence=refs,
            changes={"status": "confirmed", "evidence": ref_strings},
        )
    newest = evidence[-1]
    stated = values(f"{document.title}\n{document.body}")
    replacements = {frozenset(item[2]) for item in contradicts if item[2]}
    latest_replacement = newest[2] if newest[1].polarity == "contradict" else set()
    if newest[1].polarity == "contradict" and len(stated) == 1 and len(latest_replacement) == 1:
        old = next(iter(stated))
        new = next(iter(latest_replacement))
        corrected = _replace_value(document.body, old, new)
        if corrected is not None and corrected != document.body:
            return Candidate(
                operation="verify",
                action="correct",
                memory_ids=[document.id],
                oid=oid,
                reason=f"the newest transcript evidence states {new} where this memory "
                f"states {old}; {len(contradicts)} contradicting, {len(supports)} supporting",
                evidence=refs,
                changes={"body": corrected, "status": "confirmed", "evidence": ref_strings},
            )
    detail = (
        "contradicting evidence without a single replacement value"
        if not supports
        else "supporting and contradicting evidence"
    )
    if len(replacements) > 1:
        detail += f" ({len(replacements)} different replacement values)"
    return Candidate(
        operation="verify",
        action="mark-disputed",
        memory_ids=[document.id],
        oid=oid,
        reason=f"{detail}: {len(contradicts)} contradicting, {len(supports)} supporting",
        evidence=refs,
        changes={"status": "disputed", "evidence": ref_strings},
    )
