"""Wiring for the read-only HTTP service's ``memfw-http/3`` ``POST /context``.

``scripts/memfw_readonly_service.py`` imports this module only when a v3
client calls ``/context`` (the rest of the service runs without ``memfw``
installed). :func:`http_context_pack` builds the ``memory_context`` v2 pack
(``core`` slot) through the same ``Runtime.context_pack`` the MCP server uses,
with every write switched off: no context cache, no ``memory_access`` event,
no usage signal. It reads Markdown only and never opens a transaction.

The caller (the HTTP handler) has already bound the tenant and resolved the
privacy clearance (MM0): ``clearance=None`` filters nothing, as on the other
HTTP exits; otherwise a memory whose level the clearance does not admit is
never carried, and a withheld id never appears in ``truncated_ids``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .runtime import build_runtime

#: The only slot served over HTTP. ``handoff`` reads the conversation share as
#: a calling app and ``topics`` counts across the vault; neither has an HTTP
#: caller identity yet, so both are refused (fail closed).
HTTP_CONTEXT_SLOTS: tuple[str, ...] = ("core",)
#: HTTP defaults to the recall strict policy (status, lifecycle, supersession,
#: validity and archive on top of tenant and privacy). ``compat_fts`` is
#: accepted when asked for.
HTTP_CONTEXT_POLICY = "strict"


def http_context_pack(
    root: Path | str,
    *,
    tenant: str,
    clearance: frozenset[str] | None,
    policy: str = HTTP_CONTEXT_POLICY,
    limit_tokens: int = 2000,
    if_version: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The ``memory_context`` v2 payload for ``tenant`` under ``clearance``.

    Raises ``ValueError`` with a fixed, safe message for an unknown policy or
    a malformed ``if_version``, and ``ValueError("vault root does not
    exist")`` when ``root`` is not a vault (it never creates one).
    """

    runtime = build_runtime(
        root,
        tenant=tenant,
        allowed_sources=(),
        context_cache=False,
        usage_signal=False,
        record_access=False,
        clearance=clearance,
    )
    version, policy_name = runtime.context_request(2, policy)
    expected = runtime.context_if_version_request(version, if_version)
    repository = runtime.open_repository()
    pack = runtime.context_pack(
        repository,
        policy_name=policy_name,
        limit_tokens=int(limit_tokens),
        slots=HTTP_CONTEXT_SLOTS,
        now=now,
    )
    if expected is not None and pack.pack_version == expected:
        return {
            "v": 2,
            "limit_tokens": limit_tokens,
            "pack_version": pack.pack_version,
            "policy": pack.policy,
            "generated_at": pack.generated_at.isoformat(),
            "unchanged": True,
        }
    payload: dict[str, Any] = {"v": 2, "limit_tokens": limit_tokens, **pack.as_dict()}
    if expected is not None:
        payload["unchanged"] = False
    return payload


__all__ = ["HTTP_CONTEXT_POLICY", "HTTP_CONTEXT_SLOTS", "http_context_pack"]
