"""Who may read whose conversations, and which apps the worker may process.

Configured in ``vault.yaml``::

    conversation_share:
      enabled: true              # off unless set: the share tools refuse to run
      default: {readable: true, can_read: true, process: true}
      apps:
        cursor: {readable: false}          # its sessions are never returned
        grok: {can_read: false}            # grok may not read anything
        kimi: {can_read: [claude, codex]}  # kimi may read only these apps
        stepfun: {process: false}          # the worker never summarises it
    conversation_optout:
      agents: [private-app]      # excluded everywhere (not readable, not processed)

``readable`` controls being read, ``can_read`` controls reading, ``process``
controls the asynchronous worker. Apps not listed get ``default``. The caller
names itself (``MEMFW_APP`` / ``--app``); the name is a visibility setting
between cooperating local apps, not an authentication boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .store import ConversationError, check_app

OPERATOR = "operator"
"""Caller name of the person at the terminal (the ``memfw conversations`` CLI)."""


@dataclass(frozen=True)
class AppRule:
    readable: bool = True
    can_read: bool | tuple[str, ...] = True
    process: bool = True


def _rule(raw: Any, base: AppRule) -> AppRule:
    if raw is None:
        return base
    if not isinstance(raw, Mapping):
        raise ConversationError("conversation_share app rules must be mappings")
    readable = raw.get("readable", base.readable)
    process = raw.get("process", base.process)
    can_read_raw = raw.get("can_read", base.can_read)
    if not isinstance(readable, bool) or not isinstance(process, bool):
        raise ConversationError("readable and process must be true or false")
    can_read: bool | tuple[str, ...]
    if isinstance(can_read_raw, bool):
        can_read = can_read_raw
    elif isinstance(can_read_raw, (list, tuple)):
        can_read = tuple(sorted({check_app(str(item)) for item in can_read_raw}))
    else:
        raise ConversationError("can_read must be true, false or a list of app names")
    return AppRule(readable=readable, can_read=can_read, process=process)


@dataclass(frozen=True)
class SharePolicy:
    enabled: bool = False
    default: AppRule = AppRule()
    apps: Mapping[str, AppRule] = field(default_factory=dict)
    optout: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> SharePolicy:
        config = config or {}
        share = config.get("conversation_share") or {}
        if not isinstance(share, Mapping):
            raise ConversationError("conversation_share must be a mapping")
        enabled = share.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConversationError("conversation_share.enabled must be true or false")
        default = _rule(share.get("default"), AppRule())
        apps_raw = share.get("apps") or {}
        if not isinstance(apps_raw, Mapping):
            raise ConversationError("conversation_share.apps must be a mapping")
        apps = {check_app(str(name)): _rule(raw, default) for name, raw in apps_raw.items()}
        optout_raw = (config.get("conversation_optout") or {}).get("agents") or []
        if not isinstance(optout_raw, (list, tuple)):
            raise ConversationError("conversation_optout.agents must be a list")
        optout = frozenset(check_app(str(item)) for item in optout_raw)
        return cls(enabled=enabled, default=default, apps=apps, optout=optout)

    def rule(self, app: str) -> AppRule:
        return self.apps.get(app, self.default)

    def readable(self, app: str) -> bool:
        return app not in self.optout and self.rule(app).readable

    def processable(self, app: str) -> bool:
        return app not in self.optout and self.rule(app).process

    def hidden(self) -> tuple[str, ...]:
        """Apps whose sessions are never returned or processed for reading."""

        named = {app for app, rule in self.apps.items() if not rule.readable}
        return tuple(sorted(named | set(self.optout)))

    def scope(self, caller: str) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        """``(include, exclude)`` for ``caller``; ``include=None`` means every app.

        Raises when the share is off or the caller may not read.
        """

        if not self.enabled:
            raise ConversationError(
                "conversation sharing is off (set conversation_share.enabled in vault.yaml)"
            )
        caller = check_app(caller)
        if caller in self.optout:
            raise ConversationError(f"app {caller} has opted out of conversation sharing")
        can_read = self.rule(caller).can_read if caller != OPERATOR else True
        if can_read is False:
            raise ConversationError(f"app {caller} may not read shared conversations")
        exclude = self.hidden()
        if can_read is True:
            return None, exclude
        include = tuple(app for app in can_read if app not in exclude)
        return include, exclude

    def allowed(self, caller: str, app: str) -> bool:
        include, exclude = self.scope(caller)
        if app in exclude:
            return False
        return include is None or app in include

    def narrow(
        self, caller: str, requested: Sequence[str] | None
    ) -> tuple[tuple[str, ...] | None, tuple[str, ...]]:
        """Scope for ``caller`` narrowed to ``requested`` apps (when given)."""

        include, exclude = self.scope(caller)
        if not requested:
            return include, exclude
        wanted = tuple(sorted({check_app(app) for app in requested}))
        if include is not None:
            wanted = tuple(app for app in wanted if app in include)
        return tuple(app for app in wanted if app not in exclude), exclude
