"""Settings, backend selection and the :class:`OcrDeriver` (memfw ``Deriver`` port).

Settings (``adapters.ocr`` in the operator's config; every engine path is set
here explicitly, nothing is discovered)::

    backend: mcp                 # mcp | pdftotext | "module:factory"
    mcp_command: ["/abs/path/to/ocr-server", "--stdio"]
    mcp_env: {}                  # extra environment for the server (no credentials)
    lang: en
    engine: ""                   # passed to the server's OCR tools when set
    pdftotext: /abs/path/to/pdftotext
    source_roots: ["/abs/dir"]   # where the deriver may read originals
    clearance: [public, shared]  # privacy levels that may be handed to the engine
    timeout_seconds: 600
    max_pages: 200

The deriver reads one original per request, from inside a configured source
root, and returns the proxy candidate as a read-only view. It never reads or
writes the vault.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ._ports import PRIVACY_LEVELS, Availability, Citation, DerivedView, DeriveRequest
from .backend import OcrBackend, OcrBudget, OcrError
from .mcp import McpOcrBackend
from .pdftotext import PdftotextBackend
from .proxy import DEFAULT_CLEARANCE, generate_proxy

NAME = "ocr"
VIEW_KIND = "ocr_proxy"
FORBIDDEN_OPTIONS = frozenset({"api_key", "key", "token", "authorization", "password"})
_SPEC = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_SECRETISH = re.compile(r"(?i)(key|token|secret|password)")


@dataclass(frozen=True)
class Settings:
    backend: str = "mcp"
    mcp_command: Sequence[str] = ()
    mcp_env: Mapping[str, str] = field(default_factory=dict)
    lang: str = "en"
    engine: str = ""
    dpi: int = 200
    text_layer_min_words: int = 10
    sync_max_pages: int = 5
    poll_seconds: float = 2.0
    allow_async_jobs: bool = False
    pdftotext: str = ""
    pdfinfo: str = ""
    backend_options: Mapping[str, Any] = field(default_factory=dict)
    source_roots: Sequence[str] = ()
    clearance: Sequence[str] = tuple(sorted(DEFAULT_CLEARANCE))
    timeout_seconds: float = 600.0
    max_pages: int = 200

    def __post_init__(self) -> None:
        if self.backend not in ("mcp", "pdftotext") and not _SPEC.fullmatch(str(self.backend)):
            raise OcrError("bad_backend")
        if not isinstance(self.allow_async_jobs, bool):
            raise OcrError("bad_allow_async_jobs")
        if not isinstance(self.mcp_env, Mapping) or any(
            _SECRETISH.search(str(k)) for k in self.mcp_env
        ):
            raise OcrError("bad_mcp_env")
        for root in self.source_roots:
            if not isinstance(root, str) or not Path(root).is_absolute():
                raise OcrError("source_root_not_absolute")
        if not all(level in PRIVACY_LEVELS for level in self.clearance):
            raise OcrError("bad_clearance")
        OcrBudget(self.timeout_seconds, self.max_pages)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> Settings:
        data = dict(raw or {})
        names = {str(k).lower() for k in data} | {
            str(k).lower() for k in dict(data.get("backend_options") or {})
        }
        if FORBIDDEN_OPTIONS & names:
            raise OcrError("key_in_config")
        if set(data) - {item.name for item in fields(cls)}:
            raise OcrError("unknown_setting")
        for key in ("mcp_command", "source_roots", "clearance"):
            if key in data:
                value = data[key]
                if isinstance(value, str) or not isinstance(value, Sequence):
                    raise OcrError(f"bad_{key}")
                data[key] = tuple(value)
        return cls(**data)

    @property
    def budget(self) -> OcrBudget:
        return OcrBudget(self.timeout_seconds, self.max_pages)


def build_backend(settings: Settings) -> OcrBackend:
    """The configured :class:`OcrBackend` (``module:factory`` gets ``backend_options``)."""

    if settings.backend == "mcp":
        return McpOcrBackend(
            settings.mcp_command,
            env=settings.mcp_env,
            lang=settings.lang,
            engine=settings.engine,
            dpi=settings.dpi,
            text_layer_min_words=settings.text_layer_min_words,
            sync_max_pages=settings.sync_max_pages,
            poll_seconds=settings.poll_seconds,
            allow_async_jobs=settings.allow_async_jobs,
        )
    if settings.backend == "pdftotext":
        return PdftotextBackend(settings.pdftotext, pdfinfo=settings.pdfinfo)
    module_name, attr = settings.backend.split(":", 1)
    backend = getattr(importlib.import_module(module_name), attr)(dict(settings.backend_options))
    if not isinstance(backend, OcrBackend):
        raise OcrError("bad_backend")
    return backend


def resolve_source(roots: Sequence[str], root_index: int, rel: str) -> Path:
    """``roots[root_index]/rel``: relative, no ``..``, no symlink below the root."""

    if isinstance(root_index, bool) or not isinstance(root_index, int):
        raise OcrError("bad_root")
    if not 0 <= root_index < len(roots):
        raise OcrError("bad_root")
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        raise OcrError("bad_source")
    parts = PurePosixPath(rel).parts
    if any(part in ("", ".", "..") for part in parts) or any(ord(c) < 0x20 for c in rel):
        raise OcrError("bad_source")
    base = Path(roots[root_index]).resolve(strict=True)
    current = base
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise OcrError("source_missing") from None
        if stat.S_ISLNK(info.st_mode):
            raise OcrError("symlink_refused")
    if not current.resolve(strict=True).is_relative_to(base):
        raise OcrError("outside_root")
    return current


class OcrDeriver:
    """A :class:`Deriver`: one attachment's original → a proxy candidate view.

    ``DeriveRequest(kind="ocr_proxy", memory_ids=("att-...",), options={
    "source": "<path relative to a source root>", "root": 0, "sha256": "...",
    "media_type": "application/pdf", "privacy": "shared"})``. The view's
    ``content`` is the candidate as JSON; it cites the attachment id, claims
    at most ``experience`` (an observation) and no cause.
    """

    name = NAME

    def __init__(self, settings: Settings, *, backend: OcrBackend | None = None) -> None:
        self.settings = settings
        self.backend: OcrBackend | None = backend
        self.reason = ""
        if backend is None:
            try:
                self.backend = build_backend(settings)
            except OcrError as exc:  # a bad engine setting: unavailable, not a crash
                self.reason = exc.code

    def __repr__(self) -> str:
        return f"OcrDeriver(backend={getattr(self.backend, 'name', None)!r})"

    def available(self) -> Availability:
        if self.backend is None:
            return Availability.missing(self.reason or "no_backend")
        if not self.settings.source_roots:
            return Availability.missing("no_source_roots")
        return self.backend.available()

    def derive(
        self, vault: Path, request: DeriveRequest, *, now: datetime | None = None
    ) -> DerivedView:
        del vault  # never read or written
        if request.kind != VIEW_KIND:
            raise OcrError("bad_kind")
        if len(request.memory_ids) != 1:
            raise OcrError("one_attachment_per_request")
        (attachment_id,) = request.memory_ids
        if self.backend is None:
            raise OcrError(self.reason or "no_backend")
        options = dict(request.options)
        root, rel = options.get("root", 0), options.get("source")
        if not isinstance(root, int) or not isinstance(rel, str):
            raise OcrError("bad_source")
        path = resolve_source(self.settings.source_roots, root, rel)
        candidate = generate_proxy(
            path,
            backend=self.backend,
            attachment_id=attachment_id,
            sha256=str(options.get("sha256", "")),
            media_type=str(options.get("media_type", "")),
            privacy=str(options.get("privacy", "local-private")),
            clearance=frozenset(self.settings.clearance),
            budget=self.settings.budget,
            now=now,
        )
        return DerivedView(
            deriver=self.name,
            kind=VIEW_KIND,
            content=json.dumps(candidate, ensure_ascii=False, sort_keys=True),
            citations=(Citation(attachment_id),),
            epistemic_cap="experience",
            causal_cap="none",
        )


def factory(settings: Mapping[str, Any] | None = None) -> OcrDeriver:
    """Build the deriver from its settings (the registry passes ``adapters.ocr``)."""

    return OcrDeriver(Settings.from_mapping(settings))


__all__ = [
    "NAME",
    "VIEW_KIND",
    "OcrDeriver",
    "Settings",
    "build_backend",
    "factory",
    "resolve_source",
]
