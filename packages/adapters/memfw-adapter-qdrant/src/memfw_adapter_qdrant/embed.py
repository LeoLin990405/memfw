"""The embedder: memfw's own, never a new model.

Inside memfw the runtime hands the factory its embedding provider (the one
``MEMFW_EMBEDDING_*`` configures, which the hybrid provider uses too) as the
``embedder`` setting; that object is used as given. Outside the runtime (the
``memfw-adapter-qdrant sync`` command, the standalone wheel) the same
environment variables build :class:`EnvEmbedder`, a standard-library client
of the same OpenAI-compatible ``/v1/embeddings`` endpoint with the same
``fingerprint`` as memfw's ``HttpEmbeddingProvider`` (model, dimension and
the empty instructions), so vectors written by ``sync`` and queries embedded
in the server agree. Points carry the fingerprint and a search only matches
points of the current one.

The endpoint is the operator's (environment only). A loopback endpoint never
goes through a proxy; redirects are refused.

Request timeout and batch size (texts per request) default to 30 s and 16.
A slow CPU-only host may need a smaller batch and a longer timeout: the
adapter options ``embedding_timeout_seconds`` / ``embedding_batch_size`` set
them, else ``MEMFW_EMBEDDING_TIMEOUT`` / ``MEMFW_EMBEDDING_BATCH_SIZE``
(options win). Timeout is in (0, 600] seconds, batch size in [1, 256]. Neither
changes a vector, so neither is part of the fingerprint. They apply to this
client only: an embedder the runtime injects keeps its own settings.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit

from .http import ResponseTooLarge, open_url, read_capped

DEFAULT_TIMEOUT = 30.0
DEFAULT_BATCH_SIZE = 16
MAX_TIMEOUT = 600.0
MAX_BATCH_SIZE = 256
TIMEOUT_ENV = "MEMFW_EMBEDDING_TIMEOUT"
BATCH_SIZE_ENV = "MEMFW_EMBEDDING_BATCH_SIZE"


def check_timeout(value: Any, *, what: str = "embedding_timeout_seconds") -> float:
    """A request timeout in (0, 600] seconds."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number")
    if not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT:
        raise ValueError(f"{what} must be in (0, {MAX_TIMEOUT:g}]")
    return float(value)


def check_batch_size(value: Any, *, what: str = "embedding_batch_size") -> int:
    """Texts per embedding request, in [1, 256]."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer")
    if not 1 <= value <= MAX_BATCH_SIZE:
        raise ValueError(f"{what} must be in [1, {MAX_BATCH_SIZE}]")
    return value


class Embedder(Protocol):
    dimension: int

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]: ...


class EmbedderError(Exception):
    """The embedding endpoint failed; ``code`` is a fixed reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def fingerprint_of(embedder: Any) -> str:
    """The embedder's ``fingerprint`` (or model and dimension); 16 hex chars."""

    marker = str(getattr(embedder, "fingerprint", "") or "")
    if not marker:
        marker = f"{getattr(embedder, 'model', type(embedder).__name__)}|{dimension_of(embedder)}"
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]


def dimension_of(embedder: Any) -> int:
    value = getattr(embedder, "dimension", None)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65536:
        raise EmbedderError("bad_dimension")
    return value


class EnvEmbedder:
    """OpenAI-compatible embeddings from ``MEMFW_EMBEDDING_URL`` / ``_MODEL`` / ``_DIMENSION``."""

    def __init__(
        self,
        endpoint: str,
        *,
        model: str,
        dimension: int,
        timeout: float = DEFAULT_TIMEOUT,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        scheme = urlsplit(endpoint).scheme if isinstance(endpoint, str) else ""
        if scheme not in {"http", "https"}:
            raise ValueError("MEMFW_EMBEDDING_URL must be an http(s) URL")
        self.endpoint = endpoint
        self.model = model
        self.dimension = dimension
        self.timeout = check_timeout(timeout, what="timeout")
        self.batch_size = check_batch_size(batch_size, what="batch_size")

    @property
    def fingerprint(self) -> str:
        """As ``memfw.notes.HttpEmbeddingProvider`` without instructions."""

        marker = f"{self.model}|{self.dimension}||"
        return hashlib.sha256(marker.encode("utf-8")).hexdigest()[:16]

    def embed(self, texts: Sequence[str], *, role: str = "document") -> list[list[float]]:
        del role  # no instructions, like the provider memfw builds from the environment
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            body = json.dumps({"model": self.model, "input": batch}).encode("utf-8")
            request = urllib.request.Request(
                self.endpoint, data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with open_url(request, self.timeout) as response:
                    raw = read_capped(response)
            except ResponseTooLarge:
                # Checked before any OSError mapping: a cap breach is never "unreachable".
                raise EmbedderError("embedding_response_too_large") from None
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                OSError,
                ValueError,
            ):
                raise EmbedderError("embedding_unreachable") from None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise EmbedderError("embedding_bad_response") from None
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise EmbedderError("embedding_bad_response")
            for row in sorted(rows, key=lambda item: int(item.get("index", 0))):
                vector = row.get("embedding") if isinstance(row, dict) else None
                if not isinstance(vector, list) or len(vector) != self.dimension:
                    raise EmbedderError("embedding_bad_response")
                values = [float(value) for value in vector]
                if not all(math.isfinite(value) for value in values):
                    raise EmbedderError("embedding_bad_response")
                vectors.append(values)
        return vectors


def _env_number(env: Mapping[str, str], name: str, parse: type[float] | type[int]) -> Any:
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    try:
        return parse(raw)
    except ValueError:
        kind = "a number" if parse is float else "an integer"
        raise ValueError(f"{name} must be {kind}") from None


def embedder_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    timeout: float | None = None,
    batch_size: int | None = None,
) -> EnvEmbedder | None:
    """memfw's ``MEMFW_EMBEDDING_*`` settings (same defaults), or None without a URL.

    ``timeout`` / ``batch_size`` (the adapter options) win over
    ``MEMFW_EMBEDDING_TIMEOUT`` / ``MEMFW_EMBEDDING_BATCH_SIZE``; both fall
    back to 30 s and 16. A bad value raises ``ValueError``.
    """

    env = os.environ if environ is None else environ
    endpoint = env.get("MEMFW_EMBEDDING_URL")
    if not endpoint:
        return None
    timeout_name = "embedding_timeout_seconds"
    if timeout is None:
        timeout, timeout_name = _env_number(env, TIMEOUT_ENV, float), TIMEOUT_ENV
    batch_name = "embedding_batch_size"
    if batch_size is None:
        batch_size, batch_name = _env_number(env, BATCH_SIZE_ENV, int), BATCH_SIZE_ENV
    return EnvEmbedder(
        endpoint,
        model=env.get("MEMFW_EMBEDDING_MODEL", "embedding-model"),
        dimension=int(env.get("MEMFW_EMBEDDING_DIMENSION", "1024")),
        timeout=DEFAULT_TIMEOUT if timeout is None else check_timeout(timeout, what=timeout_name),
        batch_size=(
            DEFAULT_BATCH_SIZE
            if batch_size is None
            else check_batch_size(batch_size, what=batch_name)
        ),
    )


__all__ = [
    "BATCH_SIZE_ENV",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_TIMEOUT",
    "MAX_BATCH_SIZE",
    "MAX_TIMEOUT",
    "TIMEOUT_ENV",
    "Embedder",
    "EmbedderError",
    "EnvEmbedder",
    "check_batch_size",
    "check_timeout",
    "dimension_of",
    "embedder_from_env",
    "fingerprint_of",
]
