"""Dream operation ③ — distill: a Lesson from at least two independent events.

Event memories (``type`` or a tag such as ``incident``, ``failure``, ``fix``)
about the same concept that share content words form a cluster. A cluster
yields one **proposal** for a new ``lesson`` memory only when it holds at least
``min_events`` (2) *independent* events: events that cite transcript sessions
(evidence refs) that do not overlap. Two notes written from one session are one
event, not two, and an event that cites no session never counts.

Everything is deterministic and local (no model). The proposal lists the
supporting events, the evidence refs they cite, and a counterexample check:
memories in the same concept that negate the recurring words, are disputed, or
were marked disputed by this run's verify step. A counterexample never hides
the proposal; it is named in the reason so the reviewer sees it.

Nothing is written. The draft text is built from memory titles and recurring
words only (never transcript text), redacted, and dropped if it still carries
a secret. No memory field or status value is new: the proposed memory uses
``type: lesson``, ``status: unconfirmed`` and ``evidence`` as they exist.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from memfw.core.models import MemoryDocument
from memfw.history import has_secret, redact

from .model import Candidate, EvidenceRef, concept_key
from .verify import context_words, is_current

MIN_EVENTS = 2
"""A lesson needs at least two independent events (memfw-dreams v0.2)."""
LINK_SHARED = 2
LINK_JACCARD = 0.3
MAX_EVENTS = 500
"""Pairwise checks run on at most this many events per concept (smallest ids first)."""
MAX_TITLES = 5

EVENT_KINDS = frozenset(
    "event incident episode failure fix bug outage postmortem observation".split()
)
LESSON_KINDS = frozenset({"lesson"})
_NEGATION = re.compile(r"(?i)\b(?:not|never|no longer|isn't|wasn't|aren't|don't|doesn't)\b")


@dataclass
class DistillStats:
    events: int = 0
    clusters: int = 0
    proposed: int = 0
    rejected_single_event: int = 0
    """Clusters of one event memory: nothing to generalise from."""
    rejected_dependent: int = 0
    """Clusters of several event memories but fewer than ``min_events`` independent ones."""
    rejected_no_shared_words: int = 0
    uncited_events: int = 0
    """Event memories that cite no transcript session: never counted as independent."""
    covered_by_lesson: int = 0
    dropped_unsafe: int = 0
    min_events: int = MIN_EVENTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "clusters": self.clusters,
            "proposed": self.proposed,
            "rejected_single_event": self.rejected_single_event,
            "rejected_dependent": self.rejected_dependent,
            "rejected_no_shared_words": self.rejected_no_shared_words,
            "uncited_events": self.uncited_events,
            "covered_by_lesson": self.covered_by_lesson,
            "dropped_unsafe": self.dropped_unsafe,
            "min_events": self.min_events,
        }


def _kinds(document: MemoryDocument) -> set[str]:
    kinds = {tag.strip().lower() for tag in document.tags}
    if document.type:
        kinds.add(document.type.strip().lower())
    return kinds


def is_event(document: MemoryDocument) -> bool:
    kinds = _kinds(document)
    return bool(kinds & EVENT_KINDS) and not kinds & LESSON_KINDS


def is_lesson(document: MemoryDocument) -> bool:
    return bool(_kinds(document) & LESSON_KINDS)


def session_of(ref: str) -> str:
    """The session a transcript ref points into, in one canonical form.

    ``SOURCE:FILE_ID@OFFSET``, ``conv:APP:SESSION@OFFSET`` and
    ``conv:APP:SESSION#turn3`` all lose their position; case, a ``conv:``
    prefix and trailing slashes are normalised, so ``conv:Claude:S1@20`` and
    ``claude:s1@10`` name the same session.
    """

    value = ref.strip().lower()
    for mark in ("#", "@"):
        value = value.split(mark, 1)[0]
    value = value.rstrip("/ ")
    if value.startswith("conv:"):
        value = value[len("conv:") :]
    return value


def event_key(document: MemoryDocument, extra_refs: Iterable[str] = ()) -> frozenset[str]:
    """The transcript sessions an event cites (canonical, see ``session_of``).

    Empty for an event that cites no session: such an event cannot show it is
    independent of another one (two notes from one session look alike), so it
    never counts towards ``min_events``.
    """

    return frozenset(
        session for ref in (*document.evidence, *extra_refs) if ref and (session := session_of(ref))
    )


def _effective(document: MemoryDocument) -> datetime:
    return document.valid_from or document.created_at


def _words(document: MemoryDocument) -> set[str]:
    return context_words(f"{document.title}\n{document.body}")


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _clusters(events: list[MemoryDocument], words: dict[str, set[str]]) -> list[list[str]]:
    parent = {document.id: document.id for document in events}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for index, left in enumerate(events):
        for right in events[index + 1 :]:
            a, b = words[left.id], words[right.id]
            if len(a & b) >= LINK_SHARED and _jaccard(a, b) >= LINK_JACCARD:
                low, high = sorted((find(left.id), find(right.id)))
                parent[high] = low
    grouped: dict[str, list[str]] = defaultdict(list)
    for document in events:
        grouped[find(document.id)].append(document.id)
    return [sorted(members) for _root, members in sorted(grouped.items())]


def _independent(
    members: list[MemoryDocument], keys: dict[str, frozenset[str]]
) -> list[MemoryDocument]:
    """A greedy maximal set of cited events with pairwise disjoint sessions (oldest first)."""

    chosen: list[MemoryDocument] = []
    used: set[str] = set()
    for document in sorted(members, key=lambda item: (_effective(item).timestamp(), item.id)):
        key = keys[document.id]
        if not key or key & used:  # uncited events never count
            continue
        chosen.append(document)
        used |= key
    return chosen


def _clean(text: str) -> str | None:
    cleaned = redact(" ".join(text.split()))
    return None if has_secret(cleaned) else cleaned


def distill(
    documents: Sequence[MemoryDocument],
    verified: Sequence[Candidate] = (),
    *,
    min_events: int = MIN_EVENTS,
) -> tuple[list[Candidate], DistillStats]:
    """Propose lessons from clusters of at least ``min_events`` independent events.

    ``documents`` are one tenant's memories (not modified); ``verified`` are
    this run's verify candidates, whose evidence refs also count towards an
    event's sessions and whose ``mark-disputed`` results count as
    counterexamples.
    """

    if min_events < MIN_EVENTS:
        raise ValueError(f"min_events must be >= {MIN_EVENTS}")
    stats = DistillStats(min_events=min_events)
    current = sorted((doc for doc in documents if is_current(doc)), key=lambda doc: doc.id)
    fresh_refs: dict[str, list[EvidenceRef]] = defaultdict(list)
    disputed: set[str] = set()
    for candidate in verified:
        if candidate.operation != "verify":
            continue
        for memory_id in candidate.memory_ids:
            if candidate.action == "mark-disputed":
                disputed.add(memory_id)
            fresh_refs[memory_id].extend(
                item for item in candidate.evidence if item.polarity == "support"
            )

    by_concept: dict[str, list[MemoryDocument]] = defaultdict(list)
    unlinked: list[MemoryDocument] = []
    for document in current:
        oid = concept_key(document)
        if oid.startswith("memory-"):
            unlinked.append(document)
        else:
            by_concept[oid].append(document)
    if unlinked:
        by_concept[""] = unlinked  # events not linked to any concept cluster together

    candidates: list[Candidate] = []
    for oid in sorted(by_concept):
        members = by_concept[oid]
        events = [doc for doc in members if is_event(doc)][:MAX_EVENTS]
        stats.events += len(events)
        if not events:
            continue
        words = {doc.id: _words(doc) for doc in members}
        by_id = {doc.id: doc for doc in members}
        lessons = [doc for doc in members if is_lesson(doc)]
        for cluster_ids in _clusters(events, words):
            stats.clusters += 1
            cluster = [by_id[item] for item in cluster_ids]
            if len(cluster) < 2:
                stats.rejected_single_event += 1
                continue
            keys = {
                doc.id: event_key(doc, [item.ref for item in fresh_refs.get(doc.id, [])])
                for doc in cluster
            }
            stats.uncited_events += sum(1 for key in keys.values() if not key)
            independent = _independent(cluster, keys)
            if len(independent) < min_events:
                stats.rejected_dependent += 1
                continue
            counts = Counter(word for doc in independent for word in words[doc.id])
            titled = {word for doc in independent for word in context_words(doc.title)}
            # Most shared first; words from the events' titles before body words.
            recurring = sorted(
                (word for word, count in counts.items() if count >= min_events),
                key=lambda word: (-counts[word], word not in titled, word),
            )
            if len(recurring) < LINK_SHARED:
                stats.rejected_no_shared_words += 1
                continue
            recurring_set = set(recurring)
            needed = max(LINK_SHARED, (len(recurring_set) + 1) // 2)
            if any(len(words[doc.id] & recurring_set) >= needed for doc in lessons):
                stats.covered_by_lesson += 1
                continue
            proposal = _proposal(
                oid,
                cluster,
                independent,
                recurring,
                counterexamples=_counterexamples(
                    members, cluster_ids, words, recurring_set, disputed
                ),
                fresh_refs=fresh_refs,
                min_events=min_events,
            )
            if proposal is None:
                stats.dropped_unsafe += 1
                continue
            stats.proposed += 1
            candidates.append(proposal)
    return candidates, stats


def _counterexamples(
    members: list[MemoryDocument],
    cluster_ids: list[str],
    words: dict[str, set[str]],
    recurring: set[str],
    disputed: set[str],
) -> list[str]:
    inside = set(cluster_ids)
    found = {memory_id for memory_id in cluster_ids if memory_id in disputed}
    for document in members:
        if document.id in inside or is_lesson(document):
            continue
        if len(words[document.id] & recurring) < LINK_SHARED:
            continue
        text = f"{document.title}\n{document.body}"
        if document.status == "disputed" or _NEGATION.search(text):
            found.add(document.id)
    return sorted(found)


def _proposal(
    oid: str,
    cluster: list[MemoryDocument],
    independent: list[MemoryDocument],
    recurring: list[str],
    *,
    counterexamples: list[str],
    fresh_refs: dict[str, list[EvidenceRef]],
    min_events: int,
) -> Candidate | None:
    refs: dict[str, EvidenceRef] = {}
    for document in cluster:
        for ref in document.evidence:
            refs.setdefault(
                ref, EvidenceRef(ref=ref, polarity="support", session_id=session_of(ref))
            )
        for item in fresh_refs.get(document.id, []):
            refs.setdefault(item.ref, item)
    evidence = [refs[key] for key in sorted(refs)]
    topic = " ".join(recurring[:5])
    titles = [document.title for document in independent[:MAX_TITLES]]
    title = _clean(f"Lesson: {topic}")
    body = _clean(
        f"Seen in {len(independent)} independent events: "
        + "; ".join(titles)
        + f". Recurring: {', '.join(recurring[:12])}. "
        "Draft: state the lesson in one sentence and check the counterexamples before "
        "applying."
    )
    if title is None or body is None:
        return None
    events = [document.id for document in independent]
    target = oid or f"memory-{min(document.id for document in cluster)}"
    new_memory: dict[str, Any] = {
        "type": "lesson",
        "title": title,
        "body": body,
        "status": "unconfirmed",
        "evidence": [item.ref for item in evidence],
    }
    if oid:
        new_memory["about"] = [oid]
    caveat = (
        f"; counterexamples to check: {', '.join(counterexamples)}"
        if counterexamples
        else "; no counterexample found"
    )
    return Candidate(
        operation="distill",
        action="distill",
        memory_ids=sorted(document.id for document in cluster),
        oid=target,
        reason=(
            f"{len(independent)} independent events (at least {min_events} needed, distinct "
            f"transcript sessions) share '{topic}'; proposal: a new lesson memory"
            f"{caveat}. A human decides; nothing is written."
        ),
        evidence=evidence,
        changes={
            "new_memory": new_memory,
            "independent_events": len(independent),
            "events": events,
            "counterexamples": counterexamples,
        },
    )
