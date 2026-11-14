"""Find procedures repeated across sessions and verify their steps.

Episodes are clustered greedily (in a fixed order, so reruns agree) by a
similarity that mixes the request words and the step signatures. A cluster
becomes a *procedure* only when it spans at least ``min_sessions`` distinct
sessions. Verify-first: a step is kept only when evidence turns for it exist
in at least ``min_sessions`` of those sessions; every other step is dropped.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..verify import _WORD
from .episodes import Episode

MAX_REFS_PER_STEP = 5
MAX_KEYWORDS = 8
_FILLER = frozenset(
    "please now let lets let's need want help thanks thank again also okay could would "
    "can run make sure".split()
)
"""Request words that say nothing about the task."""
_KEYWORD = re.compile(r"^(?:[a-z][a-z-]{2,23}|[\u4e00-\u9fff]{2,8})$")
"""Keywords are plain words: anything with digits (host7, ticket ids) is left out."""


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def similarity(left: Episode, right: Episode) -> float:
    """Half request words, half step signatures; zero without a shared step."""

    steps = jaccard(left.signatures, right.signatures)
    if steps == 0.0:
        return 0.0
    return 0.5 * jaccard(left.words, right.words) + 0.5 * steps


@dataclass
class VerifiedStep:
    key: str
    """Stable id inside the procedure (``s1``, ``s2``, ...), in step order."""
    signature: str
    sessions: int
    refs: list[str]
    position: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "signature": self.signature,
            "sessions": self.sessions,
            "refs": list(self.refs),
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
        """0..1: how well steps are supported, how often and how widely it repeats."""

        total = max(1, len(self.sessions))
        support = sum(step.sessions for step in self.steps) / (len(self.steps) * total)
        repeat = min(1.0, len(self.sessions) / (2 * max(1, min_sessions)))
        breadth = min(1.0, (len(self.agents) - 1) / 2) if self.agents else 0.0
        kept = len(self.steps) / max(1, len(self.steps) + len(self.dropped))
        return round(0.45 * support + 0.25 * repeat + 0.15 * breadth + 0.15 * kept, 2)


def cluster(episodes: Sequence[Episode], threshold: float) -> list[list[Episode]]:
    ordered = sorted(episodes, key=lambda item: (item.timestamp, item.source, item.ref))
    clusters: list[list[Episode]] = []
    for episode in ordered:
        for members in clusters:
            if similarity(members[0], episode) >= threshold:
                members.append(episode)
                break
        else:
            clusters.append([episode])
    return clusters


def verify_steps(
    members: Sequence[Episode], *, min_sessions: int, min_steps: int
) -> Procedure | None:
    """The procedure a cluster supports, or None when it repeats too rarely."""

    sessions: dict[tuple[str, str], str] = {}
    for episode in members:
        sessions.setdefault(episode.session_key, episode.agent)
    if len(sessions) < min_sessions:
        return None
    by_signature: dict[str, dict[tuple[str, str], list[tuple[float, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for episode in members:
        for action in episode.steps:
            by_signature[action.signature][episode.session_key].append(
                (action.position, action.ref)
            )
    kept: list[VerifiedStep] = []
    dropped: list[str] = []
    for signature, per_session in sorted(by_signature.items()):
        if len(per_session) < min_sessions:
            dropped.append(signature)
            continue
        first = [sorted(hits)[0] for _, hits in sorted(per_session.items())]
        position = sum(item[0] for item in first) / len(first)
        refs = sorted(item[1] for item in first if item[1])[:MAX_REFS_PER_STEP]
        kept.append(
            VerifiedStep(
                key="", signature=signature, sessions=len(per_session), refs=refs, position=position
            )
        )
    if len(kept) < min_steps:
        return None
    kept.sort(key=lambda step: (step.position, step.signature))
    for number, verified in enumerate(kept, start=1):
        verified.key = f"s{number}"
    words: Counter[str] = Counter()
    for episode in members:
        words.update(episode.words)
    half = max(2, (len(members) + 1) // 2)
    seed = [word.lower() for word in _WORD.findall(members[0].request)]
    order = {word: seed.index(word) if word in seed else len(seed) for word in words}
    keywords = sorted(
        (
            word
            for word, count in words.items()
            if count >= half and word not in _FILLER and _KEYWORD.match(word)
        ),
        key=lambda word: (-words[word], order[word], word),
    )[:MAX_KEYWORDS]
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
    )


def distill(
    episodes: Sequence[Episode],
    *,
    min_sessions: int = 3,
    min_steps: int = 2,
    threshold: float = 0.4,
) -> list[Procedure]:
    procedures = [
        procedure
        for members in cluster(episodes, threshold)
        if (procedure := verify_steps(members, min_sessions=min_sessions, min_steps=min_steps))
        is not None
    ]
    return sorted(procedures, key=lambda item: item.candidate_id)
