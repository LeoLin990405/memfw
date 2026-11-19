"""``retrieval_health``: one summary of how much to trust a retrieval answer.

Every retrieval output (``memory_search``, ``memory_context`` v=2,
``conversations_search``, and the recall coordinator's labels) already
carries its own degradation signals, under different names: ``degraded``
flags and lists, ``errors``, ``recall.degraded``, ``fallback_reason``,
``recall_metrics.dropped``, a slot's ``degraded`` code. This module reads
those signals and nothing else, and returns one ``retrieval-health/1``
object::

    {"schema": "retrieval-health/1", "mode": "full|degraded|fallback|empty",
     "confidence": "high|medium|low", "reasons": [<fixed codes>, ...],
     "score_kind": "rank|bm25|cosine|mixed|none", "note": "<one zh sentence>"}

It never changes results, their order or their scores; callers add it as
one new top-level key. Everything here is a pure function over plain
mappings (no vault, no I/O, no memfw imports), so a client that only has the
JSON (fleet-memory, say) derives the same codes with the same functions.

Rules, first match wins:

1. no results: ``empty`` / ``low``; the reasons say why (``no_match``,
   ``all_sources_failed`` or ``withheld_only``) plus any other signal;
2. the primary path failed and a fallback answered: ``fallback`` / ``low``;
3. any degrading reason: ``degraded``; ``low`` when only literal matching
   was left (``keyword_only``), else ``medium``;
4. otherwise ``full`` / ``high``.

Informational reasons (``withheld:<category>:<n>``, ``access_not_recorded``)
are listed but never degrade an answer. Every reason is one of
``REASON_CODES`` (``docs/retrieval-health.md`` has the same table; a test
checks they agree); an unknown code raises ``ValueError``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

__all__ = [
    "BUILTIN_PROVIDERS",
    "CONFIDENCES",
    "HEALTH_KEY",
    "INFORMATIONAL",
    "MODES",
    "REASON_CODES",
    "SCHEMA",
    "SCORE_KINDS",
    "WITHHELD_CATEGORIES",
    "check_reason",
    "derive",
    "from_context_pack",
    "from_conversation_search",
    "from_memory_search",
    "from_recall",
    "withheld_category",
]

SCHEMA: Final = "retrieval-health/1"
#: The top-level key every retrieval output carries it under.
HEALTH_KEY: Final = "retrieval_health"
MODES: Final = ("full", "degraded", "fallback", "empty")
CONFIDENCES: Final = ("high", "medium", "low")
#: ``rank``: a fused reciprocal-rank score (weight/(60+rank), normalised): it
#: orders hits and says nothing about relevance. ``none``: hits carry no score.
SCORE_KINDS: Final = ("rank", "bm25", "cosine", "mixed", "none")
WITHHELD_CATEGORIES: Final = ("privacy", "tenant", "retired", "other")
#: The coordinator's own providers; any other provider name is an adapter.
BUILTIN_PROVIDERS: Final = frozenset({"fts", "hybrid"})

#: Every reason code (``<name>``/``<n>``/``<category>`` are parameters) and
#: what it means. Extend it, and the docs table, together.
REASON_CODES: Final[Mapping[str, str]] = {
    "semantic_unavailable": "no embedder is configured: the semantic side did not run",
    "semantic_off": "a locality setting kept the query or summaries from the embedder",
    "semantic_partial": "some memories have no vector (kept from an off-host embedder)",
    "summaries_unavailable": "session summaries/tags could not be read",
    "fts_fallback": "the semantic side gave nothing; the answer is FTS only",
    "keyword_only": "only literal (keyword) matching produced the answer",
    "expansion_empty": "multihop expansion added no hit; first-hop results only",
    "route_fallback": "the routed providers failed; the remaining ones answered",
    "qmd_live_read_errors:<n>": "a QMD live index had <n> read errors",
    "source_timeout:<name>": "source <name> timed out",
    "source_busy:<name>": "source <name> was busy (in-flight cap) and was skipped",
    "source_error:<name>": "source <name> failed or returned invalid output",
    "source_unavailable:<name>": "source <name> reported itself unavailable",
    "resolver_error:<name>": "the <name> resolver failed; its candidates were dropped",
    "adapters_shadow": "recall adapters run in shadow mode (not in the answer)",
    "adapters_timeout": "at least one recall adapter timed out",
    "adapters_busy": "at least one recall adapter was busy",
    "withheld:<category>:<n>": "<n> matches withheld by policy (privacy|tenant|retired|other)",
    "access_not_recorded": "a returned hit's access event or warm-up failed (hits unaffected)",
    "no_match": "nothing matched",
    "all_sources_failed": "every source failed: an empty answer proves nothing",
    "withheld_only": "every match was withheld by policy",
}

#: Reasons that are reported but never degrade an answer.
INFORMATIONAL: Final = frozenset({"withheld", "access_not_recorded"})
#: Reasons that only explain an empty answer.
_EMPTY_ONLY: Final = frozenset({"no_match", "all_sources_failed", "withheld_only"})

_NAME = r"[A-Za-z0-9_.-]{1,64}"
_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(
        "^"
        + re.escape(template)
        .replace(re.escape("<name>"), _NAME)
        .replace(re.escape("<n>"), r"[0-9]{1,9}")
        .replace(re.escape("<category>"), "(?:" + "|".join(WITHHELD_CATEGORIES) + ")")
        + "$"
    )
    for template in REASON_CODES
)

_NOTES: Final[Mapping[str, str]] = {
    "full": "检索路径完整，未发现降级信号。",
    "degraded": "部分检索路径降级，结果可能不完整，见 reasons。",
    "degraded_low": "只剩字面匹配，语义召回不可用，换种说法的内容可能漏掉。",
    "fallback": "主检索路径失败，已回退到备用路径，结果可信度低。",
    "no_match": "没有匹配结果。",
    "all_sources_failed": "所有检索源都失败了，空结果不代表没有相关内容。",
    "withheld_only": "有匹配项，但都被策略隐藏。",
}


def check_reason(code: str) -> str:
    """``code`` if it is one of ``REASON_CODES``, else ``ValueError``."""

    if not isinstance(code, str) or not any(p.fullmatch(code) for p in _PATTERNS):
        raise ValueError(f"unknown retrieval_health reason: {code!r}")
    return code


def _head(code: str) -> str:
    return code.split(":", 1)[0]


def derive(
    *,
    result_count: int,
    reasons: Iterable[str] = (),
    score_kind: str = "rank",
    fallback: bool = False,
    failed: bool = False,
) -> dict[str, Any]:
    """The ``retrieval-health/1`` object for one answer (module docstring rules).

    ``fallback``: the primary path failed and another answered. ``failed``:
    every source failed (an empty answer then says ``all_sources_failed``).
    """

    if score_kind not in SCORE_KINDS:
        raise ValueError(f"unknown score_kind: {score_kind!r}")
    found = {check_reason(code) for code in reasons}
    if result_count <= 0:
        found -= _EMPTY_ONLY
        if failed:
            found.add("all_sources_failed")
        elif any(_head(code) == "withheld" for code in found):
            found.add("withheld_only")
        else:
            found.add("no_match")
        why = sorted(found & _EMPTY_ONLY)[0]
        return _health("empty", "low", found, score_kind, _NOTES[why])
    found -= _EMPTY_ONLY
    degrading = {code for code in found if _head(code) not in INFORMATIONAL}
    if fallback or failed:
        return _health("fallback", "low", found, score_kind, _NOTES["fallback"])
    if degrading:
        if "keyword_only" in degrading:
            return _health("degraded", "low", found, score_kind, _NOTES["degraded_low"])
        return _health("degraded", "medium", found, score_kind, _NOTES["degraded"])
    return _health("full", "high", found, score_kind, _NOTES["full"])


def _health(
    mode: str, confidence: str, reasons: set[str], score_kind: str, note: str
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "mode": mode,
        "confidence": confidence,
        "reasons": sorted(reasons),
        "score_kind": score_kind,
        "note": note,
    }


# -- per-output signal readers ------------------------------------------------------


def withheld_category(reason: str) -> str:
    """The ``withheld`` category of a recall policy drop reason."""

    if reason in ("privacy", "tenant"):
        return reason
    if reason in ("archived", "superseded", "expired", "status"):
        return "retired"
    return "other"


def _safe_name(value: object) -> str:
    text = str(value)
    return text if re.fullmatch(_NAME, text) else "unknown"


def _provider_reasons(degraded: Mapping[str, Any], adapters: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for name, code in degraded.items():
        status = _head(str(code))
        label = _safe_name(name)
        adapter = name in adapters
        if status == "timeout":
            out.add(f"source_timeout:{label}")
            if adapter:
                out.add("adapters_timeout")
        elif status == "busy":
            out.add(f"source_busy:{label}")
            if adapter:
                out.add("adapters_busy")
        elif status == "unavailable":
            out.add(f"source_unavailable:{label}")
        else:  # error, invalid, anything new
            out.add(f"source_error:{label}")
    return out


def from_recall(
    labels: Mapping[str, Any],
    metrics: Mapping[str, Any] | None = None,
    *,
    result_count: int,
    semantic_configured: bool = True,
    lexical: Iterable[str] = ("fts",),
    builtin: Iterable[str] = BUILTIN_PROVIDERS,
    extra_reasons: Iterable[str] = (),
) -> dict[str, Any]:
    """Health of one recall coordinator answer.

    ``labels`` is ``RecallLabels.as_dict()`` (``recall`` in a two_stage
    payload), ``metrics`` ``RecallMetrics.as_dict()`` (``recall_metrics``).
    ``semantic_configured`` false (no embedder) adds ``semantic_unavailable``.
    ``lexical`` names the keyword-only providers: when every provider that
    answered is one of them, ``keyword_only`` is added. Withheld counts come
    from ``metrics["dropped"]`` (already in the output), by category.
    """

    builtin_names = frozenset(builtin)
    degraded = dict(labels.get("degraded") or {})
    adapters = [name for name in degraded if name not in builtin_names]
    reasons = _provider_reasons(degraded, adapters)
    reasons.update(
        f"resolver_error:{_safe_name(kind)}" for kind in labels.get("resolvers_degraded") or {}
    )
    reasons.update(extra_reasons)
    answered = [str(name) for name in labels.get("providers") or ()]
    if not semantic_configured:
        reasons.add("semantic_unavailable")
    semantic_missing = not semantic_configured or any(code.endswith(":hybrid") for code in reasons)
    if answered and set(answered) <= set(lexical) and semantic_missing:
        # Only literal matching answered, and the semantic side was missing.
        reasons.add("keyword_only")
    withheld: dict[str, int] = {}
    for reason, count in dict((metrics or {}).get("dropped") or {}).items():
        if isinstance(count, int) and count > 0:
            category = withheld_category(str(reason))
            withheld[category] = withheld.get(category, 0) + count
    reasons.update(f"withheld:{name}:{count}" for name, count in withheld.items())
    fallback_reason = str(labels.get("fallback_reason") or "")
    failed = bool(labels.get("fail_closed")) or fallback_reason == "all_providers_failed"
    rerouted = fallback_reason == "routed_providers_failed"
    if rerouted:
        reasons.add("route_fallback")
    return derive(
        result_count=result_count,
        reasons=reasons,
        score_kind="rank",
        fallback=rerouted,
        failed=failed,
    )


#: ``errors`` entries of the vault search paths, by exact code or text prefix.
_ERROR_CODES: Final[Mapping[str, str]] = {
    "semantic_partial": "semantic_partial",
    "semantic_query_withheld": "semantic_off",
    "rerank_withheld": "semantic_off",
    "rewrite_withheld": "semantic_off",
    "access_not_recorded": "access_not_recorded",
}
_ERROR_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("hybrid unavailable", "source_error:hybrid"),
    ("fts index unavailable", "source_error:fts"),
    ("rerank:", "source_error:rerank"),
    ("rewrite:", "source_error:rewrite"),
    ("trace:", "source_error:trace"),
)


def _error_reasons(errors: Iterable[Any]) -> set[str]:
    out: set[str] = set()
    for raw in errors:
        text = str(raw)
        if text in _ERROR_CODES:
            out.add(_ERROR_CODES[text])
            continue
        for prefix, code in _ERROR_PREFIXES:
            if text.startswith(prefix):
                out.add(code)
                break
        else:
            out.add("source_error:unknown")
    return out


def from_memory_search(
    payload: Mapping[str, Any],
    *,
    requested_mode: str,
    semantic_configured: bool | None = None,
) -> dict[str, Any]:
    """Health of one ``memory_search`` payload (any mode).

    ``requested_mode`` is the mode the caller asked for (``payload["mode"]``
    is the one that answered). ``semantic_configured`` defaults to whether
    the payload names an embedding model (``model_versions.embedding``); for
    two_stage, whether the ``hybrid`` provider answered or degraded.
    """

    results = list(payload.get("results") or [])
    errors = list(payload.get("errors") or [])
    recall = payload.get("recall")
    if semantic_configured is None:
        if requested_mode == "two_stage" and isinstance(recall, Mapping):
            # two_stage has no model_versions: the hybrid provider ran or failed.
            names = {*(recall.get("providers") or ()), *(recall.get("degraded") or {})}
            semantic_configured = "hybrid" in names
        else:
            semantic_configured = bool((payload.get("model_versions") or {}).get("embedding"))
    if requested_mode == "two_stage" and isinstance(recall, Mapping):
        return from_recall(
            payload["recall"],
            payload.get("recall_metrics"),
            result_count=len(results),
            semantic_configured=semantic_configured,
            extra_reasons=_error_reasons(errors),
        )
    reasons = _error_reasons(errors)
    used = str(payload.get("mode") or requested_mode)
    fallback = False
    if requested_mode in ("hybrid", "vector") and used == "fts":
        if "source_error:hybrid" in reasons:
            fallback = True  # the semantic path raised; FTS answered
            reasons.add("fts_fallback")
        elif "semantic_off" in reasons:
            pass
        elif not semantic_configured:
            reasons.add("semantic_unavailable")
        else:
            reasons.add("fts_fallback")  # the semantic side returned nothing
        reasons.add("keyword_only")
    elif used == "multihop" and payload.get("degraded"):
        reasons.add("expansion_empty")
    elif payload.get("degraded") and not reasons and used in ("hybrid", "vector"):
        # The semantic side recalled nothing: the hits are the FTS side's.
        reasons.update(("fts_fallback", "keyword_only"))
    score_kind = {"fts": "bm25", "multihop": "bm25", "vector": "cosine"}.get(used, "rank")
    if requested_mode in ("hybrid", "vector") and used == "fts":
        score_kind = "bm25"
    return derive(
        result_count=len(results), reasons=reasons, score_kind=score_kind, fallback=fallback
    )


#: ``conversations_search`` two_stage ``degraded`` entries.
_CONVERSATION_CODES: Final[Mapping[str, str]] = {
    "no_embedder": "semantic_unavailable",
    "semantic_off": "semantic_off",
    "embedder_error": "source_error:embedder",
    "no_summaries": "summaries_unavailable",
}
_SEMANTIC_MISSING: Final = frozenset({"no_embedder", "semantic_off", "embedder_error"})


def from_conversation_search(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Health of one ``conversations_search`` payload (default or two_stage)."""

    if payload.get("mode") != "two_stage":
        # Default mode: newest-first keyword turns, by design; no score.
        return derive(result_count=len(payload.get("results") or []), score_kind="none")
    degraded = [str(code) for code in payload.get("degraded") or []]
    reasons = {_CONVERSATION_CODES.get(code, "source_error:unknown") for code in degraded}
    candidates = payload.get("candidates") or {}
    if _SEMANTIC_MISSING & set(degraded) and not candidates.get("tags"):
        reasons.add("keyword_only")
    return derive(
        result_count=len(payload.get("sessions") or []), reasons=reasons, score_kind="rank"
    )


def from_context_pack(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Health of one ``memory_context`` v=2 pack (its slots' ``degraded`` codes)."""

    slots = [slot for slot in payload.get("slots") or [] if isinstance(slot, Mapping)]
    status = {
        str(slot.get("name")): str(slot["degraded"]) for slot in slots if slot.get("degraded")
    }
    reasons = _provider_reasons(
        {
            name: ("unavailable" if code in ("unavailable", "not_found") else code)
            for name, code in status.items()
        },
        (),
    )
    count = sum(len(slot.get("entries") or []) for slot in slots)
    failed = bool(slots) and len(status) == len(slots)
    return derive(result_count=count, reasons=reasons, score_kind="none", failed=failed)
