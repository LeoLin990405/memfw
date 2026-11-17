"""``QdrantProvider``: semantic recall from a Qdrant service (HTTP, loopback by default).

A ``RecallProvider`` (``memfw.ports``): it embeds the query with memfw's own
embedder (``embed``), asks Qdrant for the nearest points of this vault, this
embedder and the caller's tenant, and returns ``memory_id`` refs with
Qdrant's scores and ``why: semantic``. The request asks for the reference
fields only (``with_payload: ["memory_id", "tenant", "vault", "embedder"]``)
and no vectors (``with_vector: false``); the response is data, and a point
that does not carry a valid id of the caller's tenant, this vault and this
embedder is dropped.

What Qdrant holds comes from ``sync`` (``memfw-adapter-qdrant sync``): only
memories whose privacy level the ``clearance`` admits, and ``clearance`` can
never include ``local-private`` (refused at construction), so a
``local-private`` memory is never sent to the service. Lifecycle, validity,
``as_of`` and source are the coordinator's policy.

Settings (``vault.yaml`` ``runtime.recall.adapters[].options``; the runtime
adds ``vault`` and ``embedder``):

``url``
    The Qdrant REST endpoint (default ``http://127.0.0.1:6333``). A host that
    is not loopback is refused unless ``allow_remote: true``.
``collection``
    Collection name (default ``memfw``); one collection may serve several
    vaults, each point carries its vault key.
``api_key_env``
    Name of the environment variable holding the API key (none by default);
    the key itself is never a setting.
``clearance``
    ``public``, ``shared`` or both (default ``public,shared``).
``timeout_seconds``
    Per-request timeout (default 2.0).
``name``
    Provider name (default ``qdrant``).
``vault_id``
    What the points of this vault are keyed by. Default: ``vault_id`` from
    ``vault.yaml`` when it has one, else the vault's resolved path on this
    host. **Required with** ``allow_remote``: a remote collection may be shared
    by several hosts, and a path is only unique on one.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from ._ports import (
    MEMORY_ID_RE,
    REMOTE_CLEARANCE,
    AdapterSpec,
    Availability,
    Candidate,
    ProviderUnavailable,
    RecallBudget,
    check_name,
    is_vault,
)
from .embed import EmbedderError, dimension_of, embedder_from_env, fingerprint_of
from .http import QdrantClient, QdrantError, check_base_url, collection_path
from .vault import check_vault_id, configured_vault_id, points_key

__version__ = "0.1.0"
#: The Qdrant server line this adapter is pinned to (REST API, recorded fixtures).
SERVER_SERIES: Final = "1.20"
#: The exact server release the fixtures were taken from.
SERVER_VERSION: Final = "1.20.2"
#: The Python client's licence was checked (Apache-2.0); the adapter speaks REST itself.
NAME: Final = "qdrant"
DEFAULT_URL: Final = "http://127.0.0.1:6333"
DEFAULT_COLLECTION: Final = "memfw"
DEFAULT_CLEARANCE: Final = "public,shared"
MAX_QUERY_CHARS: Final = 512
#: The payload fields a search asks for: references only.
SEARCH_PAYLOAD: Final = ("memory_id", "tenant", "vault", "embedder")
#: How long a successful availability check is reused, in seconds.
AVAILABILITY_TTL: Final = 30.0
KNOWN: Final = frozenset(
    {
        "vault",
        "embedder",
        "url",
        "collection",
        "api_key_env",
        "allow_remote",
        "clearance",
        "timeout_seconds",
        "name",
        "vault_id",
    }
)
_COLLECTION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class Settings:
    """Checked settings (see the module docstring)."""

    vault: Path
    url: str = DEFAULT_URL
    collection: str = DEFAULT_COLLECTION
    api_key_env: str = ""
    allow_remote: bool = False
    clearance: frozenset[str] = REMOTE_CLEARANCE
    timeout_seconds: float = 2.0
    name: str = NAME
    #: Operator-chosen vault id; required with ``allow_remote`` (see ``points_key``).
    vault_id: str = ""

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any]) -> Settings:
        unknown = sorted(set(settings) - KNOWN)
        if unknown:
            raise ValueError("unknown qdrant settings: " + ", ".join(unknown))
        if "api_key" in settings:  # pragma: no cover - caught as unknown above
            raise ValueError("the API key comes from api_key_env only")
        vault = settings.get("vault")
        if not isinstance(vault, (str, Path)) or not str(vault).strip():
            raise ValueError("qdrant needs a vault path")
        allow_remote = settings.get("allow_remote", False)
        if not isinstance(allow_remote, bool):
            raise ValueError("allow_remote must be true or false")
        url = check_base_url(str(settings.get("url", DEFAULT_URL)), allow_remote=allow_remote)
        collection = settings.get("collection", DEFAULT_COLLECTION)
        if not isinstance(collection, str) or not _COLLECTION.fullmatch(collection):
            raise ValueError("collection must be 1-64 letters, digits, _ or -")
        api_key_env = settings.get("api_key_env", "")
        if not isinstance(api_key_env, str) or (
            api_key_env and not _ENV_NAME.fullmatch(api_key_env)
        ):
            raise ValueError("api_key_env must name an environment variable")
        raw = settings.get("clearance", DEFAULT_CLEARANCE)
        if not isinstance(raw, str):
            raise ValueError("clearance must be a comma separated string")
        levels = frozenset(token.strip().lower() for token in raw.split(",") if token.strip())
        if "local-private" in levels:
            raise ValueError("local-private memories are never sent to Qdrant")
        if not levels or not levels <= REMOTE_CLEARANCE:
            raise ValueError("clearance must name public, shared or both")
        timeout = settings.get("timeout_seconds", 2.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be a number")
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("timeout_seconds must be in (0, 60]")
        name = settings.get("name", NAME)
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        vault_id = settings.get("vault_id")
        if vault_id is None:
            vault_id = configured_vault_id(Path(vault).expanduser())
        elif vault_id != "":
            vault_id = check_vault_id(vault_id)
        if allow_remote and not vault_id:
            raise ValueError(
                "a remote Qdrant needs vault_id (several hosts may share the collection)"
            )
        return cls(
            vault=Path(vault).expanduser(),
            url=url,
            collection=collection,
            api_key_env=api_key_env,
            allow_remote=allow_remote,
            clearance=levels,
            timeout_seconds=float(timeout),
            name=check_name(name, what="provider"),
            vault_id=str(vault_id),
        )


def tenant_filter(vault: str, embedder: str, tenant: str) -> dict[str, Any]:
    """Points of this vault, this embedder and exactly this tenant."""

    return {
        "must": [
            {"key": "vault", "match": {"value": vault}},
            {"key": "embedder", "match": {"value": embedder}},
            {"key": "tenant", "match": {"value": tenant}},
        ]
    }


def server_supported(version: Any) -> bool:
    return isinstance(version, str) and (
        version == SERVER_SERIES or version.startswith(SERVER_SERIES + ".")
    )


class QdrantProvider:
    """Semantic recall from Qdrant over the memories ``sync`` wrote there."""

    lexical = False

    def __init__(self, settings: Settings, *, embedder: Any = None) -> None:
        self.settings = settings
        self.name = settings.name
        self.root = settings.vault
        self.embedder = embedder
        self.vault_key = points_key(self.root, settings.vault_id)
        self.projection_version = ""
        key = os.environ.get(settings.api_key_env) if settings.api_key_env else None
        self.client = QdrantClient(
            settings.url, api_key=key or None, timeout=settings.timeout_seconds
        )
        self._checked_at: float | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"QdrantProvider({self.settings.url!r}, collection={self.settings.collection!r})"

    def embedder_fingerprint(self) -> str:
        return fingerprint_of(self.embedder) if self.embedder is not None else ""

    # --- RecallProvider -------------------------------------------------------

    def available(self) -> Availability:
        """Cheap checks first; then the server's version and the collection (cached 30 s)."""

        if not is_vault(self.root):
            return Availability.missing("no_vault")
        if self.embedder is None:
            return Availability.missing("no_embedder")
        with self._lock:
            checked = self._checked_at
            if checked is not None and time.monotonic() - checked < AVAILABILITY_TTL:
                return Availability.ok()
        timeout = min(self.settings.timeout_seconds, 1.0)
        try:
            info = self.client.call("GET", "/", timeout=timeout, envelope=False)
            if not server_supported(info.get("version")):
                return Availability.missing("unsupported_version")
            exists = self.client.call(
                "GET", collection_path(self.settings.collection, "/exists"), timeout=timeout
            )
        except QdrantError as exc:
            return Availability.missing(exc.code)
        if not isinstance(exists, dict) or exists.get("exists") is not True:
            return Availability.missing("no_collection")
        with self._lock:
            self._checked_at = time.monotonic()
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        """At most ``limit`` (and ``budget.max_candidates``) ids of ``tenant``, nearest first."""

        del as_of
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be a string of at most 512 characters")
        cap = min(int(limit), budget.max_candidates)
        if cap < 1 or not query.strip():
            return []
        if self.embedder is None:
            raise ProviderUnavailable("no_embedder")
        try:
            (vector,) = self.embedder.embed([query], role="query")
            if len(vector) != dimension_of(self.embedder):
                raise ProviderUnavailable("embedding_bad_response")
        except EmbedderError as exc:
            raise ProviderUnavailable(exc.code) from None
        fingerprint = self.embedder_fingerprint()
        body = {
            "query": [float(value) for value in vector],
            "limit": cap,
            "filter": tenant_filter(self.vault_key, fingerprint, tenant),
            "with_payload": list(SEARCH_PAYLOAD),
            "with_vector": False,
        }
        timeout = min(self.settings.timeout_seconds, max(0.1, budget.timeout_seconds))
        try:
            result = self.client.call(
                "POST",
                collection_path(self.settings.collection, "/points/query"),
                body,
                timeout=timeout,
            )
        except QdrantError as exc:
            raise ProviderUnavailable(exc.code) from None
        self.projection_version = f"qdrant-{fingerprint}"
        points = result.get("points") if isinstance(result, dict) else None
        out: list[Candidate] = []
        seen: set[str] = set()
        for point in points if isinstance(points, list) else []:
            if not isinstance(point, dict):
                continue
            payload = point.get("payload")
            score = point.get("score")
            if not isinstance(payload, dict) or isinstance(score, bool):
                continue
            memory_id = payload.get("memory_id")
            if not isinstance(memory_id, str) or not MEMORY_ID_RE.fullmatch(memory_id):
                continue
            if (
                payload.get("tenant") != tenant
                or payload.get("vault") != self.vault_key
                or payload.get("embedder") != fingerprint
                or memory_id in seen
            ):
                continue  # never trust the service with isolation
            if not isinstance(score, (int, float)) or not math.isfinite(score):
                continue
            seen.add(memory_id)
            out.append(
                Candidate(
                    ref=memory_id,
                    ref_kind="memory_id",
                    score=round(float(score), 6),
                    provider=self.name,
                    rank=len(out) + 1,
                    extra={"why": "semantic"},
                )
            )
            if len(out) >= cap:
                break
        return out


def factory(settings: Mapping[str, Any] | None = None) -> QdrantProvider:
    """``module:factory`` entry: ``memfw_adapter_qdrant:factory``.

    The runtime passes ``embedder``; without one (``None`` or absent) the
    ``MEMFW_EMBEDDING_*`` environment builds the same endpoint's client.
    """

    values = dict(settings or {})
    embedder = values.get("embedder")
    checked = Settings.from_mapping(values)
    if embedder is None:
        embedder = embedder_from_env()
    return QdrantProvider(checked, embedder=embedder)


SPEC = AdapterSpec(
    name=NAME,
    kind="recall",
    factory=factory,
    requires=(),
    version=SERVER_VERSION,
    source="https://github.com/qdrant/qdrant",
    license="Apache-2.0",
)


__all__ = [
    "DEFAULT_CLEARANCE",
    "DEFAULT_COLLECTION",
    "DEFAULT_URL",
    "NAME",
    "SEARCH_PAYLOAD",
    "SERVER_SERIES",
    "SERVER_VERSION",
    "SPEC",
    "QdrantProvider",
    "Settings",
    "__version__",
    "factory",
    "server_supported",
    "tenant_filter",
]
