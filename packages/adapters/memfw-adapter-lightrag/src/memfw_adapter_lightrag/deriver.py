"""The LightRAG ``Deriver``: graph and answer views from a loopback ``lightrag-server``.

memfw never runs LightRAG. The operator runs ``lightrag-server`` (pinned
:data:`EXTERNAL_VERSION`) bound to ``127.0.0.1`` with an API key, and this
adapter talks to it over loopback HTTP only:

* :meth:`LightRagDeriver.sync` offers memories to the server
  (``POST /documents/text``, one per memory). Only ``public`` / ``shared``
  memories are sent (:mod:`._select`); each is named ``<id>.v<version>.<hash>.md`` so
  every answer can be mapped back to a memory *and* the version that was sent.
* :meth:`LightRagDeriver.derive` asks a question (``request.options["query"]``)
  and returns a read-only ``DerivedView``: kind ``graph`` from
  ``POST /query/data`` (entities and relationships), kind ``answer`` from
  ``POST /query`` (the server's LLM answer plus its references).

The server's output is data. A graph row is kept only when every source it
names (LightRAG joins several with ``<SEP>``) is a requested memory that is
still shareable *and* still at the version that was sent; an answer is
refused unless every reference is. So a view never cites, or carries text
from, a memory outside the request, a memory that turned private, or a stale
version. Views cap at ``inference`` / causal ``association``; :func:`candidates`
turns rows into staging candidates (``causal: none``), never memory.

Network rules: the base URL must be ``http``/``https`` to a loopback address;
proxy variables are ignored for it and redirects are refused. The API key is
read from the environment variable *named* in the settings (default
``LIGHTRAG_API_KEY``), sent as ``X-API-Key`` and never logged, printed or put
in an error. A key value in the settings is refused.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._ports import (
    AdapterSpec,
    Availability,
    Citation,
    DerivedView,
    DeriveRequest,
    IngestItem,
    MemoryFile,
    read_memory,
)
from ._select import is_shareable, shareable_memories

__version__ = "0.1.1"

NAME = "lightrag"
#: The lightrag-server release whose API the fixtures were taken from.
EXTERNAL_VERSION = "1.5.7"
SOURCE = "https://github.com/HKUDS/LightRAG"
LICENSE = "MIT"
KINDS: tuple[str, ...] = ("graph", "answer")
MODES: tuple[str, ...] = ("local", "global", "hybrid", "naive", "mix")
VIEW_FORMAT = "memfw-lightrag-v1"
#: The only mode an ``answer`` view may use (chunk retrieval, references complete).
ANSWER_MODE = "naive"
DEFAULT_BASE_URL = "http://127.0.0.1:9621"
DEFAULT_KEY_ENV = "LIGHTRAG_API_KEY"
USER_AGENT = f"memfw-adapter-lightrag/{__version__}"
RESPONSE_BYTES = 4 << 20
#: How LightRAG joins several sources in one ``file_path``.
SOURCE_SEPARATOR = "<SEP>"
#: The ``file_source`` this adapter gives a memory: ``<id>.v<version>.<hash>.md``, where
#: ``<hash>`` is the first 12 hex digits of the SHA-256 of the text sent.
SOURCE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.v[0-9]{1,9}\.[0-9a-f]{12}\.md$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
FORBIDDEN_OPTIONS = frozenset({"api_key", "key", "token", "authorization", "password"})

MAX_NAME_CHARS = 120
MAX_TYPE_CHARS = 40
MAX_DESCRIPTION_CHARS = 400
MAX_KEYWORDS_CHARS = 200


class LightRagError(RuntimeError):
    """A request to the server failed. ``code`` (e.g. ``http_401``) is all it carries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Settings:
    """The adapter's settings (``adapters.lightrag`` in the operator's config)."""

    base_url: str = DEFAULT_BASE_URL
    #: The NAME of the environment variable holding the server's API key.
    api_key_env: str = DEFAULT_KEY_ENV
    #: Refuse to run without a key (the server must not run without auth).
    require_api_key: bool = True
    timeout: float = 60.0
    mode: str = "mix"
    top_k: int = 20
    max_memories: int = 20
    max_chars: int = 8_000
    max_rows: int = 50
    max_query_chars: int = 1_000
    max_answer_chars: int = 4_000
    #: Allow a server whose LLM or embedding host is not a literal loopback address.
    cloud: bool = False

    def __post_init__(self) -> None:
        check_loopback_url(self.base_url)
        if not isinstance(self.api_key_env, str) or not _ENV_NAME_RE.fullmatch(self.api_key_env):
            raise ValueError("bad_option")
        if not isinstance(self.require_api_key, bool) or not isinstance(self.cloud, bool):
            raise ValueError("bad_option")
        if self.mode not in MODES:
            raise ValueError("bad_option")
        timeout = self.timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("bad_option")
        for name in (
            "top_k",
            "max_memories",
            "max_chars",
            "max_rows",
            "max_query_chars",
            "max_answer_chars",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("bad_option")

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any] | None) -> Settings:
        options = dict(settings or {})
        if FORBIDDEN_OPTIONS & {str(name).lower() for name in options}:
            raise ValueError("key_in_config")
        if set(options) - {item.name for item in fields(cls)}:
            raise ValueError("bad_option")
        return replace(cls(), **options)


def _loopback(host: str) -> bool:
    """A literal loopback IP (``127.0.0.0/8``, ``::1``). Names, ``localhost`` included, are not."""

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_loopback_url(url: Any) -> str:
    """``url`` without a trailing slash, when it is http(s) to a loopback host."""

    if not isinstance(url, str) or len(url) > 200:
        raise ValueError("bad_base_url")
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not parts.hostname
        or not _loopback(parts.hostname)
    ):
        raise ValueError("bad_base_url")
    return url.rstrip("/")


class _Key:
    """Holds the key; prints as ``***`` and cannot be pickled or compared."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "***"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("the API key cannot be pickled")

    def __eq__(self, other: object) -> bool:
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the key must not follow it elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


Opener = Callable[[urllib.request.Request, float], Any]


def loopback_opener() -> Opener:
    """An opener that never uses a proxy and never follows a redirect."""

    built = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return lambda request, timeout: built.open(request, timeout=timeout)


class LightRagClient:
    """JSON over loopback HTTP to one ``lightrag-server``."""

    def __init__(self, settings: Settings, *, key: str | None, opener: Opener | None = None):
        self.base_url = check_loopback_url(settings.base_url)
        self.timeout = float(settings.timeout)
        self._key = _Key(key) if key else None
        self._opener = opener or loopback_opener()
        self.requests: list[str] = []  # paths only, for tests and reports

    def __repr__(self) -> str:
        return f"LightRagClient(base_url={self.base_url!r}, key={'***' if self._key else None})"

    def call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._key is not None:
            headers["X-API-Key"] = self._key.value()
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers=headers
        )
        self.requests.append(path)
        try:
            with self._opener(request, self.timeout) as response:
                raw = response.read(RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            exc.close()
            raise LightRagError(f"http_{code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise LightRagError("unreachable") from None
        finally:
            del request
        if len(raw) > RESPONSE_BYTES:
            raise LightRagError("response_too_large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise LightRagError("bad_response") from None


# -- the server's own model hosts -----------------------------------------------------------

#: Keys in ``/health`` ``configuration`` naming where the server sends text.
HOST_KEYS: tuple[str, ...] = ("llm_binding_host", "embedding_binding_host")
_HOSTISH = re.compile(r"(?:host|base_url|endpoint|url)$", re.IGNORECASE)


def _host_is_loopback(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    return _loopback(parts.hostname)


def _nested_hosts(value: Any, key: str = "") -> list[Any]:
    """Every value under a host-like key (``*host``, ``*base_url``, ``*endpoint``, ``*url``)."""

    if isinstance(value, Mapping):
        return [found for k, v in value.items() for found in _nested_hosts(v, str(k))]
    if isinstance(value, list):
        return [found for item in value for found in _nested_hosts(item, key)]
    return [value] if _HOSTISH.search(key) and value not in (None, "") else []


def server_gate(health: Any, *, cloud: bool) -> str:
    """``""`` when this ``/health`` answer may receive memories and queries, else a code.

    The server must be the pinned release (``core_version``), must show its
    configuration (that needs the API key), and must send text only to literal
    loopback hosts: ``llm_binding_host`` and ``embedding_binding_host`` (both
    required), a rerank host when one is set, and any host-like value in its
    per-role LLM settings. ``cloud`` lifts only the loopback rule.
    """

    if not isinstance(health, Mapping):
        return "bad_response"
    if health.get("core_version") != EXTERNAL_VERSION:
        return "version_mismatch"
    config = health.get("configuration")
    if not isinstance(config, Mapping):
        return "health_unauthenticated"
    hosts = [config.get(key) for key in HOST_KEYS]
    if any(not isinstance(host, str) or not host.strip() for host in hosts):
        return "health_incomplete"
    if config.get("rerank_binding_host") not in (None, ""):
        hosts.append(config.get("rerank_binding_host"))
    hosts += _nested_hosts(config.get("role_llm_config"))
    if not cloud and not all(_host_is_loopback(host) for host in hosts):
        return "cloud_not_allowed"
    return ""


# -- sources --------------------------------------------------------------------------------


def document_text(memory: MemoryFile, max_chars: int) -> str:
    """The text ``sync`` sends for a memory: its title as a heading, then the cut body."""

    title = " ".join(memory.text("title").split())
    body = memory.body[:max_chars]
    return f"# {title}\n\n{body}" if title else body


def source_name(memory: MemoryFile, max_chars: int) -> str:
    """The ``file_source`` a memory is sent under: id, version (``0`` if none), text hash.

    The hash makes every change a new name, so a memory without a version
    still updates, and a view can tell the sent text is still the vault's.
    """

    digest = hashlib.sha256(document_text(memory, max_chars).encode("utf-8")).hexdigest()
    return f"{memory.memory_id}.v{memory.version() or 0}.{digest[:12]}.md"


def parse_sources(file_path: Any) -> list[tuple[str, str]] | None:
    """``[(memory id, source name), ...]`` from a ``file_path``; ``None`` if any part is foreign."""

    if not isinstance(file_path, str) or not file_path:
        return None
    out: list[tuple[str, str]] = []
    for part in file_path.split(SOURCE_SEPARATOR):
        name = part.strip()
        match = SOURCE_RE.fullmatch(name)
        if match is None:
            return None
        out.append((match.group(1), name))
    return out


def _current(memories: Mapping[str, MemoryFile], max_chars: int) -> dict[str, str]:
    return {memory_id: source_name(memory, max_chars) for memory_id, memory in memories.items()}


def _sources_ok(file_path: Any, current: Mapping[str, str]) -> list[str] | None:
    """The memory ids behind ``file_path`` when every one is requested, visible and current."""

    parsed = parse_sources(file_path)
    if not parsed:
        return None
    ids: list[str] = []
    for memory_id, name in parsed:
        if current.get(memory_id) != name:
            return None
        if memory_id not in ids:
            ids.append(memory_id)
    return ids


def _short(value: Any, limit: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


# -- building views (the server's output is data) --------------------------------------------


def graph_rows(
    data: Mapping[str, Any], current: Mapping[str, str], *, max_rows: int
) -> list[dict[str, Any]]:
    """Entity and relationship rows whose every source is requested, visible and current."""

    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    entities = data.get("entities")
    for item in entities if isinstance(entities, list) else []:
        if not isinstance(item, Mapping):
            continue
        sources = _sources_ok(item.get("file_path"), current)
        name = _short(item.get("entity_name"), MAX_NAME_CHARS)
        if sources is None or not name:
            continue
        row = {
            "type": "entity",
            "name": name,
            "entity_type": _short(item.get("entity_type"), MAX_TYPE_CHARS).lower() or "unknown",
            "description": _short(item.get("description"), MAX_DESCRIPTION_CHARS),
            "sources": sorted(sources),
        }
        rows.setdefault(("entity", name.lower()), row)
    relationships = data.get("relationships")
    for item in relationships if isinstance(relationships, list) else []:
        if not isinstance(item, Mapping):
            continue
        sources = _sources_ok(item.get("file_path"), current)
        source = _short(item.get("src_id"), MAX_NAME_CHARS)
        target = _short(item.get("tgt_id"), MAX_NAME_CHARS)
        if sources is None or not source or not target:
            continue
        row = {
            "type": "relation",
            "source": source,
            "target": target,
            "keywords": _short(item.get("keywords"), MAX_KEYWORDS_CHARS),
            "description": _short(item.get("description"), MAX_DESCRIPTION_CHARS),
            "sources": sorted(sources),
        }
        rows.setdefault(("relation", source.lower(), target.lower()), row)
    return [rows[key] for key in sorted(rows)][:max_rows]


def _content(payload: Mapping[str, Any]) -> str:
    body = {"format": VIEW_FORMAT, "external_version": EXTERNAL_VERSION, **payload}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _citations(ids: Iterable[str], memories: Mapping[str, MemoryFile]) -> tuple[Citation, ...]:
    wanted = set(ids)
    return tuple(
        Citation(memory_id, memory.version())
        for memory_id, memory in memories.items()
        if memory_id in wanted
    )


def candidates(view: DerivedView) -> list[IngestItem]:
    """Staging candidates from a view this adapter built (``causal: none`` on each)."""

    if view.deriver != NAME:
        raise ValueError("not a lightrag view")
    payload = json.loads(view.content)
    if not isinstance(payload, dict) or payload.get("format") != VIEW_FORMAT:
        raise ValueError("not a lightrag view")
    cited = {citation.memory_id for citation in view.citations}
    base: dict[str, str | int | float | bool | None] = {
        "deriver": NAME,
        "kind": view.kind,
        "external_version": EXTERNAL_VERSION,
        "epistemic": "inference",
        "causal": "none",
        "untrusted_model_output": True,
    }
    items: list[IngestItem] = []
    if view.kind == "answer":
        sources = list(payload["sources"])
        if not set(sources) <= cited:
            raise ValueError("view cites less than its rows")
        body = json.dumps(
            {"query": payload["query"], "answer": payload["answer"], "sources": sources},
            ensure_ascii=False,
            sort_keys=True,
        )
        metadata = {**base, "sources": ",".join(sources), "source_count": len(sources)}
        title = f"answer: {payload['query']}"[:120]
        items.append(IngestItem(title, body, f"{NAME}:answer:{payload['query'][:200]}", metadata))
        return items
    for row in payload["rows"]:
        sources = list(row["sources"])
        if not set(sources) <= cited:
            raise ValueError("view cites less than its rows")
        if row["type"] == "entity":
            title = f"entity: {row['name']}"[:120]
            ref = f"{NAME}:entity:{row['name']}"
        else:
            title = f"relation: {row['source']} -> {row['target']}"[:120]
            ref = f"{NAME}:relation:{row['source']}->{row['target']}"
        metadata = {**base, "sources": ",".join(sources), "source_count": len(sources)}
        body = json.dumps(row, ensure_ascii=False, sort_keys=True)
        items.append(IngestItem(title, body, ref[:300], metadata))
    return items


# -- the deriver ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    """Memory ids sent (``inserted``), already on the server (``present``), or held back,
    and the server documents removed as stale (``deleted``, by source name)."""

    inserted: tuple[str, ...]
    present: tuple[str, ...]
    skipped: tuple[str, ...]
    deleted: tuple[str, ...] = ()


#: Most pages of ``/documents/paginated`` one ``sync`` reads (200 documents each).
MAX_PAGES = 1_000


class LightRagDeriver:
    """``Deriver`` ``lightrag`` (kinds ``graph`` and ``answer``). See the module docstring."""

    def __init__(
        self,
        settings: Settings,
        *,
        refused: str = "",
        environ: Mapping[str, str] | None = None,
        opener: Opener | None = None,
    ) -> None:
        self.name = NAME
        self.settings = settings
        self.refused = refused
        env = os.environ if environ is None else environ
        key = (env.get(settings.api_key_env) or "").strip() or None
        if not refused and settings.require_api_key and key is None:
            self.refused = "missing_key"
        self.client = LightRagClient(settings, key=key, opener=opener)

    def __repr__(self) -> str:
        return f"LightRagDeriver(base_url={self.settings.base_url!r}, refused={self.refused!r})"

    def available(self) -> Availability:
        # Cheap and offline: whether the server answers is the next call's business.
        return Availability.missing(self.refused) if self.refused else Availability.ok()

    def _ensure(self) -> None:
        if self.refused:
            raise LightRagError(self.refused)

    def _memories(self, vault: Path, ids: Sequence[str]) -> dict[str, MemoryFile]:
        memories = shareable_memories(vault, ids)[: self.settings.max_memories]
        found = {memory.memory_id: memory for memory in memories}
        if not found:
            raise ValueError("none of the requested memories exists or may leave the vault")
        return found

    def health(self) -> dict[str, Any]:
        """``GET /health`` (status fields only; the server decides what it shows)."""

        self._ensure()
        value = self.client.call("GET", "/health")
        if not isinstance(value, dict):
            raise LightRagError("bad_response")
        return {
            key: value[key] for key in ("status", "core_version", "api_version") if key in value
        }

    def check_server(self) -> None:
        """Refuse a server that is another release or sends text off this machine."""

        self._ensure()
        refused = server_gate(self.client.call("GET", "/health"), cloud=self.settings.cloud)
        if refused:
            raise LightRagError(refused)

    def server_documents(self) -> list[tuple[str, str]]:
        """``(document id, source name)`` of every server document named like this adapter's."""

        found: list[tuple[str, str]] = []
        for page in range(1, MAX_PAGES + 1):
            payload = {"page": page, "page_size": 200, "sort_field": "id", "sort_direction": "asc"}
            reply = self.client.call("POST", "/documents/paginated", payload)
            documents = reply.get("documents") if isinstance(reply, dict) else None
            pagination = reply.get("pagination") if isinstance(reply, dict) else None
            if not isinstance(documents, list) or not isinstance(pagination, dict):
                raise LightRagError("bad_response")
            for document in documents:
                if not isinstance(document, Mapping):
                    continue
                doc_id, name = document.get("id"), document.get("file_path")
                if isinstance(doc_id, str) and isinstance(name, str) and SOURCE_RE.fullmatch(name):
                    found.append((doc_id, name))
            if pagination.get("has_next") is not True:
                return found
        raise LightRagError("too_many_documents")

    def _stale(self, vault: Path, documents: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        """Server documents whose memory is gone, no longer shareable, or changed since."""

        root = Path(vault).expanduser()
        stale: list[tuple[str, str]] = []
        for doc_id, name in documents:
            memory = read_memory(root, name.split(".v", 1)[0])
            if memory is None or not is_shareable(memory):
                stale.append((doc_id, name))
            elif source_name(memory, self.settings.max_chars) != name:
                stale.append((doc_id, name))
        return stale

    def sync(self, vault: Path, memory_ids: Sequence[str]) -> SyncResult:
        """Bring the server in line with the vault for this adapter's documents.

        First every server document named ``<id>.v<version>.<hash>.md`` whose
        memory is gone, no longer shareable, or no longer has that version and
        hash is deleted (``DELETE /documents/delete_document``), for any id,
        requested or not. Then each shareable requested memory not already
        there under its current name is sent (``POST /documents/text``).
        """

        self.check_server()
        memories = self._memories(vault, memory_ids)
        documents = self.server_documents()
        stale = self._stale(vault, documents)
        if stale:
            body = {
                "doc_ids": [doc_id for doc_id, _ in stale],
                "delete_file": False,
                "delete_llm_cache": True,
            }
            reply = self.client.call("DELETE", "/documents/delete_document", body)
            status = reply.get("status") if isinstance(reply, dict) else None
            if status != "deletion_started":
                raise LightRagError(
                    f"delete_{status}" if isinstance(status, str) else "bad_response"
                )
        on_server = {name for _, name in documents} - {name for _, name in stale}
        inserted: list[str] = []
        present: list[str] = []
        for memory_id, memory in memories.items():
            if source_name(memory, self.settings.max_chars) in on_server:
                present.append(memory_id)
                continue
            text = document_text(memory, self.settings.max_chars)
            if not text.strip():
                continue
            payload = {"text": text, "file_source": source_name(memory, self.settings.max_chars)}
            try:
                reply = self.client.call("POST", "/documents/text", payload)
            except LightRagError as exc:
                if exc.code == "http_409":  # that name (id, version, hash) is already there
                    present.append(memory_id)
                    continue
                raise
            status = reply.get("status") if isinstance(reply, dict) else None
            if status not in ("success", "partial_success"):
                raise LightRagError("insert_failed")
            inserted.append(memory_id)
        skipped = tuple(
            memory_id
            for memory_id in dict.fromkeys(memory_ids)
            if memory_id not in inserted and memory_id not in present
        )
        return SyncResult(tuple(inserted), tuple(present), skipped, tuple(n for _, n in stale))

    def _query(self, request: DeriveRequest) -> tuple[str, str]:
        query = request.options.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("options['query'] is required")
        query = " ".join(query.split())
        if len(query) > self.settings.max_query_chars:
            raise ValueError("query is too long")
        if request.kind == "answer":
            # Outside naive mode the answer's context carries merged entity and
            # relation descriptions from any document on the server (an older
            # version, one that turned private, one the operator added), and
            # ``references`` does not list them: only chunk retrieval is traceable.
            mode = request.options.get("mode", ANSWER_MODE)
            if mode != ANSWER_MODE:
                raise ValueError(
                    "answer views need mode 'naive': in other modes the answer draws on "
                    "graph descriptions its references do not show"
                )
        else:
            mode = request.options.get("mode", self.settings.mode)
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        return query, str(mode)

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView:
        if request.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        self._ensure()
        query, mode = self._query(request)
        memories = self._memories(vault, request.memory_ids)
        self.check_server()
        current = _current(memories, self.settings.max_chars)
        payload = {"query": query, "mode": mode, "top_k": self.settings.top_k}
        if request.kind == "graph":
            reply = self.client.call("POST", "/query/data", payload)
            if not isinstance(reply, dict) or reply.get("status") != "success":
                raise LightRagError("query_failed")
            data = reply.get("data")
            rows = graph_rows(
                data if isinstance(data, dict) else {}, current, max_rows=self.settings.max_rows
            )
            if not rows:
                raise ValueError("no graph rows from the requested memories")
            cited = [memory_id for row in rows for memory_id in row["sources"]]
            content = _content({"kind": "graph", "query": query, "mode": mode, "rows": rows})
        else:
            body = {**payload, "include_references": True, "stream": False}
            reply = self.client.call("POST", "/query", body)
            answer = reply.get("response") if isinstance(reply, dict) else None
            if not isinstance(answer, str) or not answer.strip():
                raise LightRagError("bad_response")
            if reply.get("llm_generated") is False:
                raise ValueError("the server found no context for the query")
            references = reply.get("references")
            if not isinstance(references, list) or not references:
                raise ValueError("the answer carries no references")
            cited = []
            for reference in references:
                ids = _sources_ok(
                    reference.get("file_path") if isinstance(reference, Mapping) else None, current
                )
                if ids is None:
                    # Text from outside the request (or a stale or private version)
                    # may be in the answer: refuse it whole.
                    raise ValueError("the answer draws on documents outside the request")
                cited.extend(memory_id for memory_id in ids if memory_id not in cited)
            content = _content(
                {
                    "kind": "answer",
                    "query": query,
                    "mode": mode,
                    "answer": answer.strip()[: self.settings.max_answer_chars],
                    "sources": sorted(cited),
                    "untrusted_model_output": True,
                }
            )
        return DerivedView(
            deriver=self.name,
            kind=request.kind,
            content=content,
            citations=_citations(cited, memories),
            epistemic_cap="inference",
            causal_cap="association",
        )


def factory(settings: Mapping[str, Any] | None = None) -> LightRagDeriver:
    """The deriver for ``adapters: {enabled: [lightrag], lightrag: {...}}``.

    A bad setting still returns a deriver that reports ``available() == False``
    with the code (``bad_option``, ``bad_base_url``, ``key_in_config``,
    ``missing_key``), so the registry shows why.
    """

    try:
        parsed = Settings.from_mapping(settings)
    except (TypeError, ValueError) as exc:
        code = str(exc)
        if code not in {"bad_option", "bad_base_url", "key_in_config"}:
            code = "bad_option"
        return LightRagDeriver(Settings(), refused=code)
    return LightRagDeriver(parsed)


SPEC = AdapterSpec(
    name=NAME,
    kind="deriver",
    factory=factory,
    version=EXTERNAL_VERSION,
    source=SOURCE,
    license=LICENSE,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_KEY_ENV",
    "EXTERNAL_VERSION",
    "KINDS",
    "MODES",
    "NAME",
    "SPEC",
    "VIEW_FORMAT",
    "LightRagClient",
    "LightRagDeriver",
    "LightRagError",
    "Settings",
    "SyncResult",
    "__version__",
    "candidates",
    "check_loopback_url",
    "document_text",
    "factory",
    "graph_rows",
    "server_gate",
    "loopback_opener",
    "parse_sources",
    "source_name",
]
