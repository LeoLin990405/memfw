"""Dream operation ② — curate: duplicates, conflicts and stale memories.

Everything here is a *proposal*. ``curate`` reads documents and returns
:class:`~memfw.dream.model.Candidate` objects; it never writes, merges,
supersedes or deletes anything. A human decides.

The rules are deterministic and local (no model, no network, no randomness):

* **merge** — inside one concept (``concept_key``), memories whose normalised
  bodies are identical or whose word sets are near-identical (Jaccard >= 0.8)
  form one cluster (union-find), and so do paraphrases: same values (numbers,
  number words, attribute values) and shared context. The earliest memory is kept.
* **supersede** — inside one concept, two memories stating the same attribute
  (``key: value`` / ``key = value`` lines) with different values, or prose with
  the same context (Jaccard >= 0.5 on non-numeric words) but different numbers.
  The newer one is proposed to supersede the older one.
* **mark-stale** — a memory superseded by an existing memory that has no
  ``valid_to``; or an unreferenced, unconfirmed memory not updated for
  ``stale_days``. The proposal sets ``valid_to``; nothing is deleted.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from memfw.core.models import MemoryDocument
from memfw.dream.model import Candidate, concept_key

MERGE_THRESHOLD = 0.8
CONFLICT_CONTEXT_THRESHOLD = 0.5
MAX_GROUP = 500
"""Pairwise checks run on at most this many memories per concept (smallest ids first)."""

_WORD = re.compile(r"\w+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_DIGITS_ONLY = re.compile(r"^[\d_]+$")
_SPACE = re.compile(r"\s+")
_ATTRIBUTE = re.compile(r"^\s*(?:[-*+]\s+)?([A-Za-z][A-Za-z0-9 _-]{0,47}?)\s*[:=]\s*(\S.*?)\s*$")
_KEY_SEPARATORS = re.compile(r"[\s-]+")


def _words(text: str) -> set[str]:
    return {token for token in _WORD.findall(text.lower()) if len(token) >= 2}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _normalised_body(text: str) -> str:
    return _SPACE.sub(" ", text).strip().lower()


_PROSE_ATTRIBUTE = re.compile(
    r"^(?:the\s+)?(?P<key>[a-z][a-z0-9 _-]{1,47}?)\s+(?:is|are|was|were)\s+"
    r"(?:now\s+|still\s+)?(?P<value>[^\s].{0,79}?)\s*$"
)
_SENTENCE = re.compile(r"[.!?;\n]+")


def _prose_attributes(body: str) -> dict[str, str]:
    """``The <thing> <attribute> is <value>.`` sentences, keyed by the subject phrase."""

    found: dict[str, str] = {}
    for sentence in _SENTENCE.split(body.lower()):
        match = _PROSE_ATTRIBUTE.match(sentence.strip())
        if match is None:
            continue
        key = _KEY_SEPARATORS.sub("_", match.group("key").strip()).strip("_")
        value = _SPACE.sub(" ", match.group("value")).strip()
        if key and value and key not in found:
            found[key] = value
    return found


def _attributes(body: str) -> dict[str, str]:
    """``key: value`` / ``key = value`` lines, then ``X is Y`` prose sentences.

    The first occurrence of a key wins.
    """

    found: dict[str, str] = {}
    for key, value in _prose_attributes(body).items():
        found.setdefault(key, value)
    for line in body.splitlines():
        match = _ATTRIBUTE.match(line)
        if match is None:
            continue
        raw_value = match.group(2)
        if raw_value.startswith("//"):  # a URL such as ``https://...`` is not an attribute
            continue
        key = _KEY_SEPARATORS.sub("_", match.group(1).strip().lower()).strip("_")
        value = _SPACE.sub(" ", raw_value).strip().lower()
        if key and value:
            found[key] = value
    return found


def _effective(document: MemoryDocument) -> datetime:
    """When a memory's statement took effect: ``valid_from``, else ``updated_at``."""

    if document.valid_from is not None:
        return document.valid_from
    return document.updated_at or document.created_at


_SPELLING = {"colour": "color", "behaviour": "behavior", "centre": "center", "grey": "gray"}
_NUMBER_WORDS = {
    word: str(value)
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve".split()
    )
}
_FILLER = frozenset("the a an is are was were be on in of for at to and holds has have".split())
PARAPHRASE_CONTEXT = 0.3
"""Shared-word Jaccard two memories need, besides equal values, to be paraphrases."""


def _canonical(word: str) -> str:
    word = _SPELLING.get(word, word)
    word = _NUMBER_WORDS.get(word, word)
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


def _canonical_text(text: str) -> str:
    return " ".join(_canonical(token) for token in _WORD.findall(text.lower()))


class _Features:
    __slots__ = ("words", "context", "numbers", "body", "attributes", "stems", "signature")

    def __init__(self, document: MemoryDocument) -> None:
        text = f"{document.title}\n{document.body}"
        self.words = _words(text)
        self.context = {word for word in self.words if not _DIGITS_ONLY.match(word)}
        self.numbers = set(_NUMBER.findall(document.body))
        self.body = _normalised_body(document.body)
        canonical_body = _canonical_text(document.body)
        self.attributes = {
            _canonical_text(key).replace(" ", "_"): _canonical_text(value)
            for key, value in _attributes(document.body).items()
        }
        tokens = canonical_body.split()
        self.stems = {
            token for token in tokens if token not in _FILLER and not _DIGITS_ONLY.match(token)
        }
        # What a memory asserts: its numbers (number words included), or else
        # the non-numeric values of its attributes. Equal signatures plus shared
        # context make two differently worded memories duplicates.
        numbers = {token for token in tokens if _DIGITS_ONLY.match(token)}
        values = {value for value in self.attributes.values() if not _NUMBER.search(value)}
        self.signature = frozenset(numbers or values)


def _conflict(left: _Features, right: _Features) -> list[str]:
    """Human-readable descriptions of how two memories disagree (empty: no conflict)."""

    reasons: list[str] = []
    for key in sorted(left.attributes.keys() & right.attributes.keys()):
        if left.attributes[key] != right.attributes[key]:
            reasons.append(f"attribute '{key}' differs")
    if (
        not reasons
        and left.numbers
        and right.numbers
        and left.numbers != right.numbers
        and _jaccard(left.context, right.context) >= CONFLICT_CONTEXT_THRESHOLD
    ):
        reasons.append("same context with different numbers")
    return reasons


def _linked(left: MemoryDocument, right: MemoryDocument) -> bool:
    return left.superseded_by == right.id or right.superseded_by == left.id


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            # Smallest id is the root: deterministic regardless of pair order.
            low, high = sorted((a, b))
            self.parent[high] = low


def _merge_candidates(
    oid: str,
    members: list[MemoryDocument],
    features: dict[str, _Features],
) -> list[Candidate]:
    finder = _UnionFind(document.id for document in members)
    for index, left in enumerate(members):
        for right in members[index + 1 :]:
            if _linked(left, right):
                continue
            a, b = features[left.id], features[right.id]
            identical = a.body == b.body
            paraphrase = (
                bool(a.signature)
                and a.signature == b.signature
                and _jaccard(a.stems, b.stems) >= PARAPHRASE_CONTEXT
            )
            if not identical and not paraphrase and _jaccard(a.words, b.words) < MERGE_THRESHOLD:
                continue
            if not identical and _conflict(a, b):
                continue  # near-identical but disagreeing: that is a conflict, not a duplicate
            finder.union(left.id, right.id)

    clusters: dict[str, list[MemoryDocument]] = defaultdict(list)
    for document in members:
        clusters[finder.find(document.id)].append(document)

    candidates: list[Candidate] = []
    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        # Keep the earliest record (the original); later ones merge into it.
        keep = min(cluster, key=lambda doc: (_effective(doc).timestamp(), doc.id))
        drop = sorted(document.id for document in cluster if document.id != keep.id)
        candidates.append(
            Candidate(
                operation="curate",
                action="merge",
                memory_ids=[keep.id, *drop],
                oid=oid,
                reason=(
                    f"{len(cluster)} memories under '{oid}' say the same thing "
                    f"(identical body, word overlap >= {MERGE_THRESHOLD}, or the same values "
                    f"in other words); proposal: keep the earliest memory {keep.id} and merge "
                    f"{', '.join(drop)} into it. A human decides; nothing is merged yet."
                ),
                changes={"merge_into": keep.id},
            )
        )
    return candidates


def _supersede_candidates(
    oid: str,
    members: list[MemoryDocument],
    features: dict[str, _Features],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for index, left in enumerate(members):
        for right in members[index + 1 :]:
            reasons = _conflict(features[left.id], features[right.id])
            if not reasons:
                continue
            newer, older = sorted(
                (left, right), key=lambda doc: (-_effective(doc).timestamp(), doc.id)
            )
            if older.superseded_by == newer.id:
                continue
            valid_to = _effective(newer).isoformat()
            candidates.append(
                Candidate(
                    operation="curate",
                    action="supersede",
                    memory_ids=[newer.id, older.id],
                    oid=oid,
                    reason=(
                        f"{newer.id} and {older.id} under '{oid}' conflict "
                        f"({'; '.join(reasons)}); proposal: the newer {newer.id} supersedes "
                        f"{older.id} from {valid_to}. A human decides which is right; "
                        "nothing is silently overwritten."
                    ),
                    changes={"superseded_by": newer.id, "valid_to": valid_to},
                )
            )
    return candidates


def _stale_candidates(
    documents: list[MemoryDocument],
    by_id: dict[str, MemoryDocument],
    *,
    now: datetime,
    stale_days: int,
) -> list[Candidate]:
    cutoff = now - timedelta(days=stale_days)
    candidates: list[Candidate] = []
    for document in documents:
        if document.valid_to is not None:
            continue
        superseder = by_id.get(document.superseded_by) if document.superseded_by else None
        if superseder is not None and superseder.id != document.id:
            valid_to = _effective(superseder).isoformat()
            reason = (
                f"{document.id} is superseded by {superseder.id} but has no valid_to; "
                f"proposal: close it at {valid_to}. Nothing is deleted."
            )
        elif (
            document.reference_count == 0
            and document.status != "confirmed"
            and document.updated_at < cutoff
        ):
            valid_to = now.isoformat()
            reason = (
                f"{document.id} was last updated {document.updated_at.date().isoformat()}, "
                f"more than {stale_days} days ago, is not referenced and not confirmed; "
                f"proposal: mark it stale (valid_to {valid_to}). Nothing is deleted."
            )
        else:
            continue
        candidates.append(
            Candidate(
                operation="curate",
                action="mark-stale",
                memory_ids=[document.id],
                oid=concept_key(document),
                reason=reason,
                changes={"valid_to": valid_to},
            )
        )
    return candidates


def curate(
    documents: list[MemoryDocument], *, now: datetime, stale_days: int = 180
) -> list[Candidate]:
    """Propose merges, supersessions and stale marks for one tenant's memories.

    ``documents`` are not modified. The result is sorted by
    ``(oid, action, memory_ids)``.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    if stale_days < 0:
        raise ValueError("stale_days must be >= 0")

    ordered = sorted(documents, key=lambda doc: doc.id)
    by_id: dict[str, MemoryDocument] = {}
    for document in ordered:
        by_id.setdefault(document.id, document)
    unique = list(by_id.values())

    groups: dict[str, list[MemoryDocument]] = defaultdict(list)
    for document in unique:
        groups[concept_key(document)].append(document)

    candidates: list[Candidate] = []
    for oid in sorted(groups):
        members = groups[oid][:MAX_GROUP]
        if len(members) < 2:
            continue
        features = {document.id: _Features(document) for document in members}
        candidates.extend(_merge_candidates(oid, members, features))
        candidates.extend(_supersede_candidates(oid, members, features))
    candidates.extend(_stale_candidates(unique, by_id, now=now, stale_days=stale_days))

    candidates.sort(key=lambda item: (item.oid, item.action, item.memory_ids))
    return candidates
