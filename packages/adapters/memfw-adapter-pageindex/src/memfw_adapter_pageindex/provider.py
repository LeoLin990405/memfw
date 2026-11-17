"""The PageIndex recall provider.

``search`` loads the trees of the configured index directory, keeps those of
the caller's tenant, and finds sections in one of two modes:

* ``reasoning`` (default): one :class:`ModelBackend` call per search with the
  query and the documents' tables of contents (titles, node ids, page ranges
  or lines, summaries when the tree kept them; never body text). The model
  names the nodes; they come back as candidates in its order.
* ``titles``: no model; nodes ranked by how many query terms their title
  (and summary) share. Useful without a backend and as a check.

Gates (fail closed):

* A backend that is not ``local`` needs ``cloud: true`` in the adapter's
  settings; otherwise the provider is unavailable (``cloud_not_allowed``).
* A ``local-private`` tree is never put in a request to a backend that is not
  ``local``, even with ``cloud: true``; such a search simply does not see it.
* A backend that declares ``tasks`` must list :data:`~.prompt.TASK`.
* Per-run budget: each provider instance (one process) may spend at most
  ``max_run_tokens`` (estimated prompt plus ``max_output_tokens``, then the
  reported usage) and ``max_calls`` calls. When the next call would not fit,
  the search raises :class:`ProviderUnavailable` and the provider reports
  ``budget_spent`` from then on. A backend's own
  :class:`BackendBudgetExhausted` does the same. A
  :class:`BackendUnavailable` (network, auth, deadline) only degrades that one
  search (``backend_unavailable``); the next search tries again.

Candidates are path refs (``kh:<path>#<heading>``, ``<source>#page=<n>``) with
``extra`` holding only ``kind`` (``section``), ``page`` (PDF) and ``why``
(``tree_search`` or ``title_match``). The query and the replies are never
logged.
"""

from __future__ import annotations

import importlib
import json
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from ._ports import (
    Availability,
    BackendBudgetExhausted,
    BackendUnavailable,
    Candidate,
    ProviderUnavailable,
    RecallBudget,
)
from .prompt import PROMPT_VERSION, TASK, parse_tree_search_reply, render_tree_search_prompt
from .tree import Source, TreeError, iter_nodes, load_index, node_ref, parse_source

NAME = "pageindex"
#: Keys memfw's runtime recall loader passes to every factory.
RUNTIME_KEYS = ("vault", "embedder")
MODES = ("reasoning", "titles")
#: Options that would put a credential in configuration: always refused.
FORBIDDEN_OPTIONS = frozenset({"api_key", "key", "token", "authorization", "password"})
_SPEC = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class SettingsError(ValueError):
    """Bad adapter settings. ``code`` is a short code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Settings:
    """The adapter's settings (``adapters.pageindex`` in the operator's config)."""

    index_dir: str = ""
    mode: str = "reasoning"
    backend: str = ""
    backend_options: Mapping[str, Any] | None = None
    cloud: bool = False
    max_documents: int = 20
    max_nodes_per_document: int = 300
    max_prompt_tokens: int = 8000
    max_output_tokens: int = 256
    max_run_tokens: int = 200_000
    max_calls: int = 500

    def __post_init__(self) -> None:
        if not isinstance(self.index_dir, str) or not Path(self.index_dir).is_absolute():
            raise SettingsError("index_dir_not_absolute")
        if self.mode not in MODES:
            raise SettingsError("bad_mode")
        spec = self.backend
        if not isinstance(spec, str) or (spec and not _SPEC.fullmatch(spec)):
            raise SettingsError("bad_backend_spec")
        if self.backend_options is not None and not isinstance(self.backend_options, Mapping):
            raise SettingsError("bad_backend_options")
        if FORBIDDEN_OPTIONS & {str(k).lower() for k in (self.backend_options or {})}:
            raise SettingsError("key_in_config")
        if not isinstance(self.cloud, bool):
            raise SettingsError("bad_cloud")
        for name in (
            "max_documents",
            "max_nodes_per_document",
            "max_prompt_tokens",
            "max_output_tokens",
            "max_run_tokens",
            "max_calls",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SettingsError("bad_limit")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> Settings:
        data = dict(raw or {})
        if FORBIDDEN_OPTIONS & {str(k).lower() for k in data}:
            raise SettingsError("key_in_config")
        known = {item.name for item in fields(cls)}
        if set(data) - known:
            raise SettingsError("unknown_setting")
        return cls(**data)


def estimate_tokens(text: str) -> int:
    """A conservative token estimate (about 3 characters per token, CJK about 1)."""

    wide = sum(1 for char in text if ord(char) > 0x2E80)
    return 1 + (len(text) - wide) // 3 + wide


def terms(text: str) -> set[str]:
    """Lower-cased words; a run of CJK characters adds each character and each pair."""

    out: set[str] = set()
    for word in _WORD.findall(text.lower()):
        if any(ord(c) > 0x2E80 for c in word):
            out.update(word)
            out.update(word[i : i + 2] for i in range(len(word) - 1))
        elif len(word) > 1 or word.isdigit():
            out.add(word)
    return out


def _node_score(query: set[str], node: Mapping[str, Any]) -> float:
    title = terms(str(node.get("title", "")))
    summary = terms(str(node.get("summary", "")))
    return len(query & title) + 0.5 * len(query & (summary - title))


@dataclass(frozen=True)
class _Hit:
    ref: str
    page: int | None


class PageIndexProvider:
    """A :class:`RecallProvider` over PageIndex document trees."""

    name = NAME
    lexical = False
    prompt_version = PROMPT_VERSION

    def __init__(self, settings: Settings, *, backend: Any | None = None) -> None:
        self.settings = settings
        self.backend = backend
        self._lock = threading.Lock()
        self.calls = 0
        self.tokens = 0
        self.stopped = ""

    def __repr__(self) -> str:
        return f"PageIndexProvider(mode={self.settings.mode!r})"

    # -- availability ----------------------------------------------------------------

    def _backend_reason(self) -> str:
        backend = self.backend
        if backend is None:
            return "no_backend"
        if not all(hasattr(backend, attr) for attr in ("name", "local", "run")):
            return "bad_backend"
        if backend.local is not True and not self.settings.cloud:
            return "cloud_not_allowed"
        tasks = getattr(backend, "tasks", None)
        if tasks is not None and TASK not in tasks:
            return "backend_task_unsupported"
        return ""

    def available(self) -> Availability:
        try:
            load_index(Path(self.settings.index_dir))
        except TreeError as exc:
            return Availability.missing(exc.code)
        if self.settings.mode == "reasoning":
            reason = self._backend_reason()
            if reason:
                return Availability.missing(reason)
            if self.stopped:
                return Availability.missing("budget_spent")
        return Availability.ok()

    def usage(self) -> dict[str, Any]:
        """This run's counts (no text)."""

        return {"calls": self.calls, "tokens": self.tokens, "stopped": self.stopped}

    # -- search -------------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        del as_of  # trees carry no validity window; the coordinator filters
        if not isinstance(query, str) or not query.strip() or limit < 1:
            return []
        limit = min(int(limit), budget.max_candidates)
        try:
            index = load_index(Path(self.settings.index_dir))
        except TreeError as exc:
            raise ProviderUnavailable(exc.code) from None
        trees = [tree for tree in index.trees if tree["tenant"] == tenant]
        if not trees:
            return []
        if self.settings.mode == "titles":
            hits, why = self._titles(query, trees, limit), "title_match"
        else:
            hits, why = self._reasoning(query, trees, limit), "tree_search"
        out: list[Candidate] = []
        for rank, hit in enumerate(hits[:limit], 1):
            extra: dict[str, Any] = {"kind": "section", "why": why}
            if hit.page is not None:
                extra["page"] = hit.page
            out.append(
                Candidate(
                    ref=hit.ref,
                    ref_kind="path",
                    score=round(1.0 / rank, 6),
                    provider=self.name,
                    rank=rank,
                    extra=extra,
                )
            )
        return out

    @staticmethod
    def _hit(source: Source, node: Mapping[str, Any]) -> _Hit | None:
        ref = node_ref(source, node)
        if ref is None:
            return None
        page = node.get("start_index") if source.media == "pdf" else None
        return _Hit(ref, page if isinstance(page, int) else None)

    def _titles(self, query: str, trees: Sequence[dict[str, Any]], limit: int) -> list[_Hit]:
        wanted = terms(query)
        scored: list[tuple[float, int, _Hit]] = []
        order = 0
        for tree in trees:
            source = parse_source(tree["source"])
            for _, node in iter_nodes(tree["structure"]):
                order += 1
                score = _node_score(wanted, node)
                hit = self._hit(source, node) if score > 0 else None
                if hit is not None:
                    scored.append((-score, order, hit))
        scored.sort(key=lambda item: (item[0], item[1]))
        return _unique([hit for _, _, hit in scored], limit)

    def _sendable(self, trees: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        local = getattr(self.backend, "local", False) is True
        return [tree for tree in trees if local or tree["privacy"] != "local-private"]

    def _outline(
        self, query: str, trees: Sequence[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, _Hit]]:
        wanted = terms(query)

        def best(tree: dict[str, Any]) -> float:
            scores = (_node_score(wanted, n) for _, n in iter_nodes(tree["structure"]))
            return max(scores, default=0)

        ranked = sorted(enumerate(trees), key=lambda item: (-best(item[1]), item[0]))
        documents: list[dict[str, Any]] = []
        hits: dict[str, _Hit] = {}
        room = self.settings.max_prompt_tokens
        used = _prompt_tokens({"query": query, "max_nodes": 0, "documents": []})
        for number, (_, tree) in enumerate(ranked[: self.settings.max_documents], 1):
            source = parse_source(tree["source"])
            doc = f"d{number}"
            nodes: list[dict[str, Any]] = []
            for depth, node in iter_nodes(tree["structure"]):
                if len(nodes) >= self.settings.max_nodes_per_document:
                    break
                hit = self._hit(source, node)
                if hit is None:
                    continue
                entry: dict[str, Any] = {
                    "id": f"{doc}:{node['node_id']}",
                    "depth": depth,
                    "title": node["title"],
                }
                if source.media == "pdf":
                    entry["pages"] = f"{node['start_index']}-{node['end_index']}"
                elif isinstance(node.get("line_num"), int):
                    entry["line"] = node["line_num"]
                if isinstance(node.get("summary"), str):
                    entry["summary"] = node["summary"]
                cost = estimate_tokens(json.dumps(entry, ensure_ascii=False)) + 2
                if used + cost > room:
                    break
                used += cost
                nodes.append(entry)
                hits[entry["id"]] = hit
            if nodes:
                documents.append({"doc": doc, "media": source.media, "nodes": nodes})
                used += 10
            if used >= room:
                break
        return {"query": query, "max_nodes": 0, "documents": documents}, hits

    def _fit(self, data: dict[str, Any], hits: dict[str, _Hit]) -> tuple[str, str]:
        """Render the prompt, dropping trailing nodes until it is within ``max_prompt_tokens``.

        The estimate is of the rendered text (escapes and CJK included), so
        the cap holds whatever the characters are. Raises
        :class:`ProviderUnavailable` (``prompt_too_large``) when not even the
        query fits.
        """

        documents: list[dict[str, Any]] = data["documents"]
        while True:
            system, user = render_tree_search_prompt(data)
            if estimate_tokens(system + user) <= self.settings.max_prompt_tokens:
                return system, user
            if not documents:
                raise ProviderUnavailable("prompt_too_large")
            last = documents[-1]["nodes"].pop()
            hits.pop(last["id"], None)
            if not documents[-1]["nodes"]:
                documents.pop()

    def _reserve(self, tokens: int) -> None:
        with self._lock:
            if self.stopped:
                raise ProviderUnavailable("budget_spent")
            if self.calls + 1 > self.settings.max_calls:
                self.stopped = "calls"
            elif self.tokens + tokens > self.settings.max_run_tokens:
                self.stopped = "tokens"
            if self.stopped:
                raise ProviderUnavailable("budget_spent")
            self.calls += 1
            self.tokens += tokens

    def _reasoning(self, query: str, trees: Sequence[dict[str, Any]], limit: int) -> list[_Hit]:
        reason = self._backend_reason()
        if reason:
            raise ProviderUnavailable(reason)
        sendable = self._sendable(trees)
        if not sendable:
            return []
        data, hits = self._outline(query, sendable)
        if not hits:
            raise ProviderUnavailable("prompt_too_large")
        data["max_nodes"] = limit
        system, user = self._fit(data, hits)
        if not hits:
            raise ProviderUnavailable("prompt_too_large")
        worst = estimate_tokens(system + user) + self.settings.max_output_tokens
        self._reserve(worst)
        backend = self.backend
        assert backend is not None
        try:
            reply = backend.run(TASK, data, max_tokens=self.settings.max_output_tokens)
        except BackendBudgetExhausted:
            with self._lock:  # the backend's own budget is spent: stop for this run
                self.stopped = self.stopped or "backend_budget"
            raise ProviderUnavailable("budget_spent") from None
        except BackendUnavailable:
            # transient (network, auth, deadline): this search degrades, later ones retry
            raise ProviderUnavailable("backend_unavailable") from None
        text = getattr(reply, "text", None)
        if not isinstance(text, str):
            raise ProviderUnavailable("bad_reply")
        used = _reported(reply)
        if used is not None:
            with self._lock:  # replace the worst case with the reported usage
                self.tokens += used - worst
        ids = parse_tree_search_reply(text, list(hits), limit=limit)
        if ids is None:
            raise ProviderUnavailable("bad_reply")
        return _unique([hits[i] for i in ids], limit)


def _prompt_tokens(data: Mapping[str, Any]) -> int:
    system, user = render_tree_search_prompt(data)
    return estimate_tokens(system + user)


def _reported(reply: Any) -> int | None:
    tokens_in, tokens_out = getattr(reply, "tokens_in", 0), getattr(reply, "tokens_out", 0)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (tokens_in, tokens_out)):
        return None
    total = tokens_in + tokens_out
    return total if total > 0 and math.isfinite(total) else None


def _unique(hits: Sequence[_Hit], limit: int) -> list[_Hit]:
    seen: set[str] = set()
    out: list[_Hit] = []
    for hit in hits:
        if hit.ref not in seen:
            seen.add(hit.ref)
            out.append(hit)
        if len(out) >= limit:
            break
    return out


def load_backend(spec: str, options: Mapping[str, Any] | None) -> Any:
    """Build a backend from ``module:factory`` (called with the options mapping)."""

    if not _SPEC.fullmatch(spec):
        raise SettingsError("bad_backend_spec")
    module_name, attr = spec.split(":", 1)
    factory: Callable[..., Any] = getattr(importlib.import_module(module_name), attr)
    backend = factory(dict(options or {}))
    if not all(hasattr(backend, a) for a in ("name", "local", "run")):
        raise SettingsError("bad_backend")
    return backend


def factory(settings: Mapping[str, Any] | None = None) -> PageIndexProvider:
    """Build the provider from its settings (the registry passes ``adapters.pageindex``)."""

    data = dict(settings or {})
    # memfw's runtime recall loader (``runtime.recall.adapters``) adds these two
    # keys; trees live in ``index_dir`` and no embedder is used, so both are ignored.
    for key in RUNTIME_KEYS:
        data.pop(key, None)
    parsed = Settings.from_mapping(data)
    backend = None
    if parsed.mode == "reasoning" and parsed.backend:
        backend = load_backend(parsed.backend, parsed.backend_options)
    return PageIndexProvider(parsed, backend=backend)


__all__ = [
    "FORBIDDEN_OPTIONS",
    "MODES",
    "NAME",
    "PageIndexProvider",
    "Settings",
    "SettingsError",
    "estimate_tokens",
    "factory",
    "load_backend",
    "terms",
]
