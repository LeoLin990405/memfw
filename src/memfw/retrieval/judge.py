"""Optional local judge. Off unless the server allow-list names an endpoint.

No hosted endpoint is preset. A request cannot supply one; that rejection
lives on the HTTP and MCP boundaries. Scores are cached by passage hash and
each query has a hard call cap. The default search path does not call a judge.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from typing import Any, Protocol

JudgeTransport = Callable[[str, str, str, str], float]


class Judge(Protocol):
    def score(self, question: str, passage: str, context: str) -> float: ...


class NullJudge:
    """The default. It is never asked to score."""

    def score(self, question: str, passage: str, context: str) -> float:
        del question, passage, context
        return 0.0


class LocalJudge:
    """Score with a transport the server configured. The endpoint must be listed."""

    def __init__(
        self,
        endpoint: str,
        allowlist: tuple[str, ...],
        transport: JudgeTransport | None = None,
    ) -> None:
        if endpoint not in allowlist:
            raise ValueError("judge endpoint is not allow-listed")
        self.endpoint = endpoint
        self.transport = transport

    def score(self, question: str, passage: str, context: str) -> float:
        if self.transport is None:
            raise RuntimeError("local judge has no transport")
        value = float(self.transport(self.endpoint, question, passage, context))
        if value < 0.0 or value > 1.0:
            raise ValueError("judge score must be in [0, 1]")
        return value


def judge_cap(environ: Any | None = None) -> int:
    """Hard cap on judge calls for one query. The default is 8."""

    env = os.environ if environ is None else environ
    raw = str(env.get("MEMFW_JUDGE_CAP", "8")).strip()
    try:
        cap = int(raw)
    except ValueError as exc:
        raise ValueError("MEMFW_JUDGE_CAP must be an integer") from exc
    if cap < 0:
        raise ValueError("judge cap must be non-negative")
    return cap


def judge_from_environment(environ: Any | None = None) -> LocalJudge | None:
    """Build a judge only when both the URL and the allow-list are set."""

    env = os.environ if environ is None else environ
    endpoint = str(env.get("MEMFW_JUDGE_URL", "")).strip()
    if not endpoint:
        return None
    raw_allowlist = str(env.get("MEMFW_JUDGE_ALLOWLIST", ""))
    allowlist = tuple(item.strip() for item in raw_allowlist.split(",") if item.strip())
    return LocalJudge(endpoint, allowlist)


def apply_judge(
    results: list[dict[str, Any]],
    passages: dict[str, str],
    question: str,
    judge: Judge,
    *,
    cap: int,
    context: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """Rerank with a cached judge. Calls stop at ``cap``. Unscored hits stay last."""

    if cap < 0:
        raise ValueError("judge cap must be non-negative")
    cache: dict[str, float] = {}
    calls = 0
    scored: list[dict[str, Any]] = []
    for item in results:
        passage = passages.get(str(item["id"]), "")
        digest = hashlib.sha256(passage.encode("utf-8")).hexdigest()
        if digest in cache:
            value: float | None = cache[digest]
        elif calls >= cap:
            value = None
        else:
            calls += 1
            value = judge.score(question, passage, context)
            cache[digest] = value
        updated = dict(item)
        updated["judge_score"] = value
        scored.append(updated)
    scored.sort(
        key=lambda item: (
            item["judge_score"] is None,
            -(item["judge_score"] or 0.0),
            str(item["id"]),
        )
    )
    return scored, calls
