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
# Clauses inside a sentence: "the host is db1 and the port is 5432",
# "18 bar, set by the night shift".
_CLAUSE = re.compile(r",\s*(?:and\s+)?|\s+and\s+")


_APOSTROPHE = re.compile(r"[\u2019\u2018`]")
_CONTRACTION = re.compile(
    r"\b(?P<stem>is|are|was|were|do|does|did|has|have|had|should|could|would|must)n't\b"
)
_NEGATION_WORDS = re.compile(r"\b(?:no\s+longer|not|never|cannot|can't|won't|no|none|without)\b")
_ANTONYMS = {
    "enabled": "disabled",
    "disabled": "enabled",
    "active": "inactive",
    "inactive": "active",
    "available": "unavailable",
    "unavailable": "available",
    "allowed": "forbidden",
    "forbidden": "allowed",
    "true": "false",
    "false": "true",
    "required": "optional",
    "optional": "required",
}
"""Words whose negation is another word: "not enabled" states "disabled"."""


def _positive(clause: str) -> tuple[str, bool]:
    """A clause without its negation words, and whether it was negated.

    ``The cache isn't enabled`` -> (``The cache is enabled``, True). Negation is
    judged per clause, so a "not" in one clause never flips another clause.
    """

    text = _APOSTROPHE.sub("'", clause.lower())
    text, contractions = _CONTRACTION.subn(lambda match: match.group("stem"), text)
    text = (
        text.replace("can't", "can not").replace("won't", "will not").replace("cannot", "can not")
    )
    text, negations = _NEGATION_WORDS.subn(" ", text)
    return _SPACE.sub(" ", text).strip(), bool(contractions or negations)


def _clauses(body: str) -> list[tuple[str, bool]]:
    """Every clause of ``body`` as ``(positive text, negated)``."""

    found: list[tuple[str, bool]] = []
    for sentence in _SENTENCE.split(body):
        for clause in _CLAUSE.split(sentence):
            if clause.strip():
                found.append(_positive(clause))
    return found


def _polar_value(value: str, negated: bool) -> tuple[str, bool]:
    """Fold "not enabled" into "disabled"; other negated values keep the flag."""

    if negated and value in _ANTONYMS:
        return _ANTONYMS[value], False
    return value, negated


def _prose_attributes(body: str) -> dict[str, tuple[str, bool]]:
    """``The <thing> <attribute> is [not] <value>.`` clauses: key -> (value, negated)."""

    found: dict[str, tuple[str, bool]] = {}
    for clause, negated in _clauses(body):
        match = _PROSE_ATTRIBUTE.match(clause)
        if match is None:
            continue
        key = _KEY_SEPARATORS.sub("_", match.group("key").strip()).strip("_")
        value = _SPACE.sub(" ", match.group("value")).strip()
        if key and value and key not in found:
            found[key] = _polar_value(value, negated)
    return found


def _attributes(body: str) -> dict[str, tuple[str, bool]]:
    """``key: value`` / ``key = value`` lines, then ``X is Y`` prose clauses.

    Values map to ``(value, negated)``; ``cache: not enabled`` is
    ``("disabled", False)``. A line overrides a prose clause for the same key.
    """

    found: dict[str, tuple[str, bool]] = {}
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
        positive, negated = _positive(raw_value)
        text = _SPACE.sub(" ", positive).strip()
        if key and text:
            found[key] = _polar_value(text, negated)
    return found


def _effective(document: MemoryDocument) -> datetime:
    """When a memory's statement took effect: ``valid_from``, else ``updated_at``."""

    if document.valid_from is not None:
        return document.valid_from
    return document.updated_at or document.created_at


_SPELLING = {"colour": "color", "behaviour": "behavior", "centre": "center", "grey": "gray"}
_SYNONYM_GROUPS = {
    "deploy": "deploys deployed deploying deployment deployments",
    "database": "db dbs databases",
    "configuration": "config configs configurations",
    "repository": "repo repos repositories",
    "production": "prod",
    "kubernetes": "k8s",
    "environment": "env envs environments",
    "application": "app apps applications",
    "directory": "dir dirs directories folder folders",
    "documentation": "docs",
    "begin": "begins began start starts started",
    "happen": "happens happened occur occurs occurred scheduled",
    "stop": "stops stopped halt halts halted",
}
"""A small fixed synonym table. Opposites (enabled/disabled) are never folded."""
_SYNONYMS = {
    variant: canonical
    for canonical, variants in _SYNONYM_GROUPS.items()
    for variant in variants.split()
}
_NUMBER_WORDS = {
    word: str(value)
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve".split()
    )
}
_FILLER = frozenset("the a an is are was were be on in of for at to and holds has have".split())
_NEGATIONS = frozenset(
    "not no never none nobody nothing nowhere cannot can't don't doesn't didn't isn't "
    "aren't wasn't weren't won't without".split()
)
# Keys that name *which* thing a memory is about. host, region, owner, team and
# the like are attributes of a thing and may change (a move, a hand-over).
_IDENTITY_KEYS = frozenset(
    "service name env environment project repo repository cluster account tenant app "
    "application".split()
)
PARAPHRASE_CONTEXT = 0.3
"""Shared-word Jaccard two memories need, besides equal values, to be paraphrases."""
CLAUSE_MATCH = 0.6
"""Stem Jaccard two clauses need to state the same thing (one negating the other)."""

_VALUE_TOKEN = re.compile(r"[a-z][a-z0-9_]*|v?\d+(?:[.,]\d+)*[a-z%]*")
_NUMERIC = re.compile(r"^v?\d")
_LINK_WORDS = frozenset(
    "the a an is are was were be been on in of for at to by from with as now still set "
    "uses use using runs run listens listen has have holds".split()
)
_DO_WORDS = frozenset({"do", "doe", "does", "did"})


def _canonical(word: str) -> str:
    word = _SPELLING.get(word, word)
    word = _NUMBER_WORDS.get(word, word)
    if word in _SYNONYMS:
        return _SYNONYMS[word]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return _SYNONYMS.get(word, word)


def _canonical_text(text: str) -> str:
    return " ".join(_canonical(token) for token in _WORD.findall(text.lower()))


def _clause_stems(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in _canonical_text(text).split()
        if token not in _FILLER
        and token not in _DO_WORDS
        and token not in _NEGATIONS
        and not _NUMERIC.match(token)
    )


def _value_signatures(text: str) -> dict[str, frozenset[str]]:
    """Values tied to the noun before (or else after) them: ``port 8000``, ``45s timeout``.

    ``The api runs on port 8000`` -> ``{"port": {"8000"}}``. Link words (``on``,
    ``of``, ``is``, ``uses``...) between the noun and the value are skipped.
    """

    tokens = _VALUE_TOKEN.findall(text)
    found: dict[str, set[str]] = defaultdict(set)
    for position, token in enumerate(tokens):
        if not _NUMERIC.match(token):
            continue
        key = ""
        for back in range(position - 1, max(-1, position - 4), -1):
            word = tokens[back]
            if _NUMERIC.match(word):
                break
            if word in _LINK_WORDS:
                continue
            key = word
            break
        if not key and position + 1 < len(tokens):
            following = tokens[position + 1]
            if not _NUMERIC.match(following) and following not in _LINK_WORDS:
                key = following
        if key and key not in _NEGATIONS:
            found[_canonical(key)].add(token.lstrip("v").replace(",", "."))
    return {key: frozenset(values) for key, values in found.items()}


class _Features:
    __slots__ = (
        "words",
        "context",
        "numbers",
        "body",
        "attributes",
        "polarity",
        "values",
        "clauses",
        "stems",
        "signature",
        "numeric",
        "subject",
        "negated",
    )

    def __init__(self, document: MemoryDocument) -> None:
        text = f"{document.title}\n{document.body}"
        self.words = _words(text)
        self.context = {word for word in self.words if not _DIGITS_ONLY.match(word)}
        self.numbers = set(_NUMBER.findall(document.body))
        self.body = _normalised_body(document.body)
        clauses = _clauses(document.body)
        positive_body = " ".join(clause for clause, _negated in clauses)
        canonical_body = _canonical_text(positive_body)
        self.attributes: dict[str, str] = {}
        self.polarity: dict[str, bool] = {}
        for key, (value, negated) in _attributes(document.body).items():
            name = _canonical_text(key).replace(" ", "_")
            self.attributes[name] = _canonical_text(value)
            self.polarity[name] = negated
        # Values tied to a noun (``port 8000``); attribute keys take precedence.
        self.values = {
            key: values
            for key, values in _value_signatures(positive_body).items()
            if key not in self.attributes
        }
        # Each clause's content stems and whether that clause is negated.
        self.clauses = [
            (stems, negated)
            for stems, negated in ((_clause_stems(clause), neg) for clause, neg in clauses)
            if stems
        ]
        tokens = canonical_body.split()
        self.stems = {
            token for token in tokens if token not in _FILLER and not _NUMERIC.match(token)
        }
        # What a memory asserts: its numbers (number words included), or else
        # the non-numeric values of its attributes (a negated one as "not X").
        # Equal signatures plus shared context make two differently worded
        # memories duplicates.
        numbers = {token for token in tokens if _DIGITS_ONLY.match(token)}
        values = {
            ("not " if self.polarity[key] else "") + value
            for key, value in self.attributes.items()
            if not _NUMBER.search(value)
        }
        self.signature = frozenset(numbers or values)
        self.numeric = frozenset(numbers)
        # What a memory is about: its content words without the values it states
        # and without negation words. Two memories describe the same subject only
        # when one subject is contained in the other (staging vs production, Alice
        # vs Bob are different subjects).
        value_words = {word for value in self.attributes.values() for word in value.split()}
        self.negated = any(negated for _stems, negated in clauses)
        self.subject = self.stems - value_words - _NEGATIONS - _DO_WORDS


def _same_subject(left: _Features, right: _Features) -> bool:
    """Whether two memories talk about the same thing (see ``_Features.subject``).

    A differing identity key (``service: api`` vs ``service: worker``) always
    means two different things, however many other attributes agree.
    """

    if not (left.subject <= right.subject or right.subject <= left.subject):
        return False
    shared = left.attributes.keys() & right.attributes.keys()
    for key in shared:
        if key.split("_")[-1] in _IDENTITY_KEYS and (
            left.attributes[key] != right.attributes[key]
            or left.polarity[key] != right.polarity[key]
        ):
            return False
    return True


def _opposed_clauses(left: _Features, right: _Features) -> list[str]:
    """Clauses that state the same thing, negated in exactly one memory."""

    opposed: list[str] = []
    for left_stems, left_negated in left.clauses:
        for right_stems, right_negated in right.clauses:
            if left_negated == right_negated:
                continue
            shared = left_stems & right_stems
            if not shared:
                continue
            if (
                left_stems == right_stems
                or (
                    min(len(left_stems), len(right_stems)) >= 2
                    and (left_stems <= right_stems or right_stems <= left_stems)
                )
                or _jaccard(set(left_stems), set(right_stems)) >= CLAUSE_MATCH
            ):
                opposed.append(" ".join(sorted(shared)))
    return sorted(set(opposed))


def _conflict(left: _Features, right: _Features) -> list[str]:
    """Human-readable descriptions of how two memories disagree (empty: no conflict).

    Only memories about the same subject can conflict (see ``_same_subject``).
    """

    if not _same_subject(left, right):
        return []
    reasons: list[str] = []
    shared = sorted(left.attributes.keys() & right.attributes.keys())
    for key in shared:
        same_value = left.attributes[key] == right.attributes[key]
        same_polarity = left.polarity[key] == right.polarity[key]
        if same_value and not same_polarity:
            reasons.append(f"attribute '{key}' is negated in one memory")
        elif not same_value and same_polarity and not left.polarity[key]:
            reasons.append(f"attribute '{key}' differs")
        # "owner is not alice" vs "owner is bob" does not disagree.
    if not reasons:
        reasons.extend(
            f"value of '{key}' differs"
            for key in sorted(left.values.keys() & right.values.keys())
            if not left.values[key] & right.values[key]
        )
    if not reasons:
        reasons.extend(
            f"one memory negates the other ({clause})" for clause in _opposed_clauses(left, right)
        )
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
                (
                    bool(a.signature)
                    and a.signature == b.signature
                    and _jaccard(a.stems, b.stems) >= PARAPHRASE_CONTEXT
                )
                or (
                    # Same subject words and the same (possibly no) values: a reworded
                    # duplicate such as "Do not deploy on Fridays" / "Never deploy on
                    # a Friday".
                    len(a.subject) >= 2 and a.subject == b.subject and a.signature == b.signature
                )
                or (
                    # The same content words once synonyms are folded, stated as an
                    # attribute in one and as prose in the other: "Deploys happen on
                    # Fridays" / "Deployments are scheduled on Friday".
                    len(a.stems) >= 2 and a.stems == b.stems and a.numeric == b.numeric
                )
            )
            if not identical and not paraphrase and _jaccard(a.words, b.words) < MERGE_THRESHOLD:
                continue
            if not identical and (not _same_subject(a, b) or _opposed_clauses(a, b)):
                continue  # different subjects, or one clause says "not": never a duplicate
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
