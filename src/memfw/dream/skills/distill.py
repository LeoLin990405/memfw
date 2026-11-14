"""Find procedures repeated across independent sessions and verify their steps.

Episodes are clustered greedily (in a fixed order, so reruns agree) on what
was actually done: mostly the step signatures, weighted by how rare they are
(a ``git status`` every session runs says little; ``uv build`` then ``git
tag`` says a lot), then the request words and the agent's stated intents.

A cluster becomes a *procedure* only when it repeats in at least
``min_sessions`` *independent* sessions with a spread: sessions that carry
the same request text on the same day (a retried or fanned-out dispatch)
count once, and the
independent sessions must come from at least two agents or two days.
Verify-first: a step is kept only when it appears in at least
``min_sessions`` of those independent sessions; every other step is dropped.

Each kept step is drafted extractively: its most representative stated
intent, the flags and input kinds most sessions used, the checks its results
showed, and the sessions it was seen in. No model is called.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..verify import _WORD, context_words
from .boilerplate import has_content
from .episodes import Episode, Step

MAX_REFS_PER_STEP = 5
MAX_SESSION_IDS = 10
MAX_KEYWORDS = 8
MAX_CANDIDATE_CLUSTERS = 64
"""Clusters compared per episode (found through its rarest signatures)."""
_FILLER = frozenset(
    "please now let lets let's need want help thanks thank again also okay could would "
    "can run make sure check use using used task tasks work done first then next file "
    "files code look see read write update fix add new all any one two some more this that "
    "with from into for and the you your our will should must just like same each only "
    "step steps objective goal round 继续 请 一下 这个 那个 我们 你 我".split()
)
"""Request words that say nothing about the task."""
_KEYWORD = re.compile(r"^(?:[a-z][a-z-]{2,23}|[一-鿿]{2,8})$")
"""Keywords are plain words: anything with digits (host7, ticket ids) is left out."""
SETUP_SUBCOMMANDS = frozenset(
    {"sync", "install", "venv", "clone", "checkout", "switch", "worktree"}
)
MIN_CORPUS = 50
MIN_WEIGHT = 0.15
"""Signatures below this weight (in most episodes) cannot link two episodes alone."""


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def signature_weights(episodes: Sequence[Episode]) -> dict[str, float] | None:
    """Inverse document frequency of each signature over sessions, scaled to 0..1.

    None (plain Jaccard) below ``MIN_CORPUS`` sessions: a small corpus cannot
    tell a ubiquitous step from a repeated one.
    """

    sessions: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for episode in episodes:
        for signature in episode.signatures:
            sessions[signature].add(episode.session_key)
    total = len({episode.session_key for episode in episodes})
    if total < MIN_CORPUS:
        return None
    top = math.log(total)
    return {
        signature: max(0.02, math.log(total / len(keys)) / top)
        for signature, keys in sessions.items()
    }


def weighted_jaccard(
    left: frozenset[str], right: frozenset[str], weights: dict[str, float] | None
) -> float:
    if weights is None:
        return jaccard(left, right)
    union = sum(weights.get(item, 1.0) for item in left | right)
    if not union:
        return 0.0
    return sum(weights.get(item, 1.0) for item in left & right) / union


def similarity(left: Episode, right: Episode, weights: dict[str, float] | None = None) -> float:
    """Mostly the (rarity-weighted) shared steps; zero without an informative shared step."""

    shared = left.signatures & right.signatures
    if not shared:
        return 0.0
    if weights is not None and max(weights.get(item, 1.0) for item in shared) < MIN_WEIGHT:
        return 0.0
    steps = weighted_jaccard(left.signatures, right.signatures, weights)
    words = jaccard(left.words, right.words)
    intents = jaccard(left.intent_words, right.intent_words)
    if not left.intent_words and not right.intent_words:
        return 0.5 * steps + 0.5 * words
    return 0.5 * steps + 0.3 * words + 0.2 * intents


@dataclass
class VerifiedStep:
    key: str
    """Stable id inside the procedure (``s1``, ``s2``, ...), in step order."""
    signature: str
    sessions: int
    """Independent sessions the step was seen in."""
    refs: list[str]
    position: float
    intent: str = ""
    flags: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    """Opaque ``agent:session_id`` of the sessions that support the step."""

    @property
    def command(self) -> str:
        """The command as drafted: signature words plus the shared flags."""

        kind, _, value = self.signature.partition(":")
        if kind != "cmd":
            return value
        return " ".join([value, *self.flags])

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "signature": self.signature,
            "sessions": self.sessions,
            "refs": list(self.refs),
            "intent": self.intent,
            "command": self.command,
            "flags": list(self.flags),
            "inputs": list(self.inputs),
            "checks": list(self.checks),
            "session_ids": list(self.session_ids),
        }


@dataclass
class Procedure:
    episodes: list[Episode]
    steps: list[VerifiedStep]
    dropped: list[str]
    """Signatures seen in the cluster but not supported in enough sessions."""
    keywords: list[str]
    sessions: list[dict[str, str]] = field(default_factory=list)
    agents: dict[str, int] = field(default_factory=dict)
    independent: int = 0
    """Independent sessions (same request text counts once)."""
    days: list[str] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)

    @property
    def procedure_key(self) -> str:
        """Same steps, same key: links candidates across reruns with new evidence."""

        seed = "\n".join(step.signature for step in self.steps)
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    @property
    def candidate_id(self) -> str:
        """Same evidence, same id."""

        lines = [f"step {step.signature} {' '.join(step.refs)}" for step in self.steps]
        lines += [f"session {item['source']} {item['session_id']}" for item in self.sessions]
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]
        return f"skill-{digest}"

    def confidence(self, min_sessions: int) -> float:
        """0..1: step support, repetition, spread, kept steps and recorded success."""

        total = max(1, self.independent or len(self.sessions))
        support = sum(step.sessions for step in self.steps) / (len(self.steps) * total)
        repeat = min(1.0, total / (2 * max(1, min_sessions)))
        spread = max(len(self.agents), len(self.days))
        breadth = min(1.0, (spread - 1) / 2) if spread else 0.0
        kept = len(self.steps) / max(1, len(self.steps) + len(self.dropped))
        success = self.outcomes.get("success", 0) / total
        return round(
            0.4 * support + 0.2 * repeat + 0.15 * breadth + 0.1 * kept + 0.15 * min(1.0, success),
            2,
        )


def cluster(
    episodes: Sequence[Episode],
    threshold: float,
    weights: dict[str, float] | None = None,
) -> list[list[Episode]]:
    """Greedy clusters in timestamp order; each episode joins the first close seed."""

    ordered = sorted(episodes, key=lambda item: (item.timestamp, item.source, item.ref))
    clusters: list[list[Episode]] = []
    by_signature: dict[str, list[int]] = defaultdict(list)
    for episode in ordered:
        if weights is None:
            candidates = range(len(clusters))
        else:
            informative = sorted(
                (item for item in episode.signatures if weights.get(item, 1.0) >= MIN_WEIGHT),
                key=lambda item: (-weights.get(item, 1.0), item),
            )
            found: set[int] = set()
            for signature in informative:
                found.update(by_signature.get(signature, ()))
            candidates = sorted(found)[:MAX_CANDIDATE_CLUSTERS]
        for number in candidates:
            if similarity(clusters[number][0], episode, weights) >= threshold:
                clusters[number].append(episode)
                break
        else:
            clusters.append([episode])
            for signature in episode.signatures:
                by_signature[signature].append(len(clusters) - 1)
    return clusters


def _components(members: Sequence[Episode]) -> dict[tuple[str, str], int]:
    """Session -> independent group: sessions that share a request text are one group."""

    parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(key: tuple[str, str]) -> tuple[str, str]:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    first: dict[str, tuple[str, str]] = {}
    for episode in members:
        key = episode.session_key
        parent.setdefault(key, key)
        task = episode.task_key
        if task in first:
            left, right = find(first[task]), find(key)
            if left != right:
                parent[max(left, right)] = min(left, right)
        else:
            first[task] = key
    roots = sorted({find(key) for key in parent})
    number = {root: index for index, root in enumerate(roots)}
    return {key: number[find(key)] for key in parent}


def _representative_intent(intents: Sequence[str]) -> str:
    """The stated intent sharing most words with the others (ties: shorter, then sorted)."""

    options = sorted({item for item in intents if has_content(item)})
    if not options:
        return ""
    words = {item: context_words(item) for item in options}
    usable = [item for item in intents if item in words]

    def score(item: str) -> tuple[float, int, str]:
        overlap = sum(jaccard(frozenset(words[item]), frozenset(words[other])) for other in usable)
        return (-overlap, len(item), item)

    return min(options, key=score)


def _shared(values: dict[int, set[str]], need: int) -> list[str]:
    counts: Counter[str] = Counter()
    for items in values.values():
        counts.update(items)
    return [value for value, count in sorted(counts.items()) if count >= need]


def _preconditions(steps: Sequence[VerifiedStep]) -> list[str]:
    programs: list[str] = []
    setup: list[str] = []
    for step in steps:
        kind, _, value = step.signature.partition(":")
        if kind != "cmd":
            continue
        words = value.split()
        if words[0] not in programs:
            programs.append(words[0])
        if set(words[1:]) & SETUP_SUBCOMMANDS and step.position <= 0.34:
            setup.append(value)
    out = []
    if {"git", "gh"} & set(programs):
        out.append("Work inside a git checkout of the project.")
    if programs:
        out.append("Programs on PATH: " + ", ".join(f"`{item}`" for item in programs) + ".")
    for value in setup:
        out.append(f"Environment prepared first (`{value}`).")
    return out


def _reject(stats: MutableMapping[str, int] | None, reason: str) -> None:
    if stats is not None:
        stats[reason] = stats.get(reason, 0) + 1


def verify_steps(
    members: Sequence[Episode],
    *,
    min_sessions: int,
    min_steps: int,
    stats: MutableMapping[str, int] | None = None,
) -> Procedure | None:
    """The procedure a cluster supports, or None when it repeats too rarely.

    ``stats`` (optional) counts why clusters were rejected.
    """

    sessions: dict[tuple[str, str], str] = {}
    for episode in members:
        sessions.setdefault(episode.session_key, episode.agent)
    if len(sessions) < min_sessions:
        _reject(stats, "rejected_too_few_sessions")
        return None
    group = _components(members)
    independent = len(set(group.values()))
    if independent < min_sessions:
        _reject(stats, "rejected_not_independent")
        return None
    group_agents: dict[int, set[str]] = defaultdict(set)
    group_days: dict[int, set[str]] = defaultdict(set)
    for episode in members:
        group_agents[group[episode.session_key]].add(episode.agent or "unknown")
        if episode.day:
            group_days[group[episode.session_key]].add(episode.day)
    agents_seen = set().union(*group_agents.values())
    days_seen = set().union(*group_days.values()) if group_days else set()
    if len(agents_seen) < 2 and len(days_seen) < 2:
        _reject(stats, "rejected_no_spread")
        return None

    hits: dict[str, dict[int, list[tuple[Episode, Step]]]] = defaultdict(lambda: defaultdict(list))
    for episode in members:
        for action in episode.steps:
            hits[action.signature][group[episode.session_key]].append((episode, action))
    kept: list[VerifiedStep] = []
    dropped: list[str] = []
    for signature, per_group in sorted(hits.items()):
        if len(per_group) < min_sessions:
            dropped.append(signature)
            continue
        firsts = [
            min(found, key=lambda item: (item[1].position, item[1].ref))
            for _, found in sorted(per_group.items())
        ]
        position = sum(step.position for _, step in firsts) / len(firsts)
        refs = sorted(step.ref for _, step in firsts if step.ref)[:MAX_REFS_PER_STEP]
        need = max(2, (len(per_group) + 1) // 2)
        flags = {
            number: {f for _, s in found for f in s.flags} for number, found in per_group.items()
        }
        inputs = {
            number: {kind for _, s in found for kind in s.inputs}
            for number, found in per_group.items()
        }
        checks = {
            number: {s.check for _, s in found if s.check} for number, found in per_group.items()
        }
        session_ids = sorted(
            {
                f"{episode.agent or 'unknown'}:{episode.session_id}"
                for _, found in per_group.items()
                for episode, _ in found
            }
        )[:MAX_SESSION_IDS]
        kept.append(
            VerifiedStep(
                key="",
                signature=signature,
                sessions=len(per_group),
                refs=refs,
                position=position,
                intent=_representative_intent(
                    [s.intent for found in per_group.values() for _, s in found]
                ),
                flags=_shared(flags, need),
                inputs=_shared(inputs, need),
                checks=_shared(checks, 2),
                session_ids=session_ids,
            )
        )
    if len(kept) < min_steps:
        _reject(stats, "rejected_too_few_steps")
        return None
    kept.sort(key=lambda step: (step.position, step.signature))
    for number, verified in enumerate(kept, start=1):
        verified.key = f"s{number}"

    words: Counter[str] = Counter()
    group_words: dict[int, set[str]] = defaultdict(set)
    for episode in members:
        group_words[group[episode.session_key]].update(episode.words | episode.intent_words)
    for items in group_words.values():
        words.update(items)
    half = max(2, (independent + 1) // 2)
    seed = [word.lower() for word in _WORD.findall(members[0].request)]
    order = {word: seed.index(word) if word in seed else len(seed) for word in words}
    request_words: set[str] = set().union(*(episode.words for episode in members))
    keywords = sorted(
        (
            word
            for word, count in words.items()
            if count >= half and word not in _FILLER and _KEYWORD.match(word)
        ),
        key=lambda word: (word not in request_words, -words[word], order[word], word),
    )[:MAX_KEYWORDS]

    outcomes: Counter[str] = Counter()
    for number in sorted(set(group.values())):
        found = [episode.outcome for episode in members if group[episode.session_key] == number]
        outcomes[
            "success" if "success" in found else "failed" if "failed" in found else "unknown"
        ] += 1
    agents: Counter[str] = Counter(agent or "unknown" for agent in sessions.values())
    return Procedure(
        episodes=list(members),
        steps=kept,
        dropped=dropped,
        keywords=keywords,
        sessions=[
            {"source": source, "session_id": session, "agent": agent or "unknown"}
            for (source, session), agent in sorted(sessions.items())
        ],
        agents=dict(sorted(agents.items())),
        independent=independent,
        days=sorted(days_seen),
        outcomes=dict(sorted(outcomes.items())),
        preconditions=_preconditions(kept),
    )


def distill(
    episodes: Sequence[Episode],
    *,
    min_sessions: int = 3,
    min_steps: int = 2,
    threshold: float = 0.4,
    stats: MutableMapping[str, int] | None = None,
) -> list[Procedure]:
    weights = signature_weights(episodes)
    clusters = cluster(episodes, threshold, weights)
    if stats is not None:
        stats["clusters"] = len(clusters)
    procedures = [
        procedure
        for members in clusters
        if (
            procedure := verify_steps(
                members, min_sessions=min_sessions, min_steps=min_steps, stats=stats
            )
        )
        is not None
    ]
    return sorted(procedures, key=lambda item: item.candidate_id)
