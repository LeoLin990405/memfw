"""``memory_context`` and the ``memfw://context/<slot>`` resources, built by the Runtime.

U6: the optional ``current`` slot is listed (``tools/list`` slots enum,
``resources/list``) and readable only while the vault enables it.
"""

from __future__ import annotations

import json
from typing import Any

from memfw.mcp.base import GatewayBase
from memfw.mcp.schema import (
    CONTEXT_POLICIES,
    CONTEXT_RESOURCE_PREFIX,
    CONTEXT_SLOTS,
    CURRENT_CONTEXT_SLOT,
    CURRENT_RESOURCE,
    CURRENT_RESOURCE_POLICY,
    RESOURCE_POLICIES,
    RESOURCES,
    memory_context_tool_with_current,
)


class ResourceQueryError(ValueError):
    """A ``resources/read`` URI with a query other than ``policy=`` and ``if_version=``."""


class ContextTools(GatewayBase):
    """``memory_context`` and the context resources."""

    def context(
        self,
        limit_tokens: int = 2000,
        *,
        v: int | None = 1,
        policy: str | None = "compat_fts",
        slots: Any = None,
        if_version: Any = None,
    ) -> dict[str, Any]:
        """``memory_context``. v1 (default): the original payload, unchanged.

        ``v=2`` returns a ``ContextPack`` filtered with ``policy``
        (``compat_fts`` default, or ``strict``) with the requested ``slots``
        (default ``["core"]``, in the given order); the Runtime builds it.

        ``if_version`` (v=2 only, C3): the ``pack_version`` the client already
        holds. The pack is still built from the vault; when its version is the
        same the reply is the pack's header with ``unchanged: true`` and no
        slots, otherwise the full pack with ``unchanged: false``. Without
        ``if_version`` the reply has no ``unchanged`` key.
        """

        version, policy_name = self.runtime.context_request(v, policy)
        names = self.runtime.context_slots_request(version, slots)
        expected = self.runtime.context_if_version_request(version, if_version)
        repository = self._repo()
        if version == 1:
            return self.runtime.context_v1(repository, limit_tokens=limit_tokens)
        pack = self.runtime.context_pack(
            repository,
            policy_name=policy_name,
            limit_tokens=int(limit_tokens),
            slots=names,
            app=self.app,
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
        payload = {"v": 2, "limit_tokens": limit_tokens, **pack.as_dict()}
        if expected is not None:
            payload["unchanged"] = False
        return payload

    def context_current_enabled(self) -> bool:
        """The ``current`` slot exists (U6; the Runtime decides, default off)."""

        return self.runtime.context_current_enabled()

    def tools(self) -> list[dict[str, Any]]:
        """``tools/list``: unchanged unless ``current`` is enabled, which adds it to ``slots``."""

        tools: list[dict[str, Any]] = super().tools()  # type: ignore[misc]
        if not self.context_current_enabled():
            return tools
        return [
            memory_context_tool_with_current(tool) if tool["name"] == "memory_context" else tool
            for tool in tools
        ]

    def resources(self) -> list[dict[str, Any]]:
        """``resources/list``: ``RESOURCES``, plus ``memfw://context/current`` when enabled."""

        if not self.context_current_enabled():
            return RESOURCES
        return [*RESOURCES, CURRENT_RESOURCE]

    def read_resource(self, uri: str) -> dict[str, Any] | None:
        """``resources/read``: one slot's v=2 pack, or ``None`` for an unknown URI.

        ``memfw://context/<slot>``, optionally with a query of
        ``policy=<name>`` (``compat_fts`` or ``strict``) and/or
        ``if_version=<pack_version>`` (C3, as ``memory_context``), each at
        most once, joined by ``&``; any other query (an unknown key, a repeat,
        an empty value, an invalid version) raises ``ResourceQueryError``:
        fail closed. Without ``policy=`` the policy is
        ``RESOURCE_POLICIES`` for the slot: ``strict`` for ``topics`` (its counts
        would otherwise include local-private memories), ``compat_fts`` for the
        others (``current`` included). The same caller as ``memory_context``
        (app, tenant, sources) and the default budget. ``memfw://context/current``
        is a known URI only while the slot is enabled (U6); off, it is
        ``None`` (resource not found), as before.
        """

        if not uri.startswith(CONTEXT_RESOURCE_PREFIX):
            return None
        rest = uri[len(CONTEXT_RESOURCE_PREFIX) :]
        slot, has_query, query = rest.partition("?")
        if slot in CONTEXT_SLOTS:
            policy = RESOURCE_POLICIES[slot]
        elif slot == CURRENT_CONTEXT_SLOT and self.context_current_enabled():
            policy = CURRENT_RESOURCE_POLICY
        else:
            return None
        if_version: str | None = None
        if has_query:
            params = _resource_query(query)
            policy = params.get("policy", policy)
            if_version = params.get("if_version")
        try:
            self.runtime.context_if_version_request(2, if_version)
        except ValueError as exc:
            raise ResourceQueryError(str(exc)) from exc
        payload = self.context(v=2, policy=policy, slots=[slot], if_version=if_version)
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }


_QUERY_ERROR = "resource query must be policy=compat_fts or strict and/or if_version=<pack_version>"


def _resource_query(query: str) -> dict[str, str]:
    """``policy=`` and ``if_version=`` from a resource query; anything else is refused."""

    params: dict[str, str] = {}
    for part in query.split("&"):
        key, has_value, value = part.partition("=")
        if key not in ("policy", "if_version") or not has_value or not value or key in params:
            raise ResourceQueryError(_QUERY_ERROR)
        params[key] = value
    if "policy" in params and params["policy"] not in CONTEXT_POLICIES:
        raise ResourceQueryError(_QUERY_ERROR)
    return params
