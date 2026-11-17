"""HTTP to the Qdrant REST API: loopback by default, no redirects, no proxy for loopback.

Two rules, both on the literal host in the configured URL (never DNS), the
same as memfw's ``notes/http.open_http``:

* the endpoint must be a loopback host (``localhost``, ``127.0.0.0/8``,
  ``::1``, ``::ffff:127.x``) unless the operator sets ``allow_remote: true``;
* a request to a loopback host never goes through ``HTTP(S)_PROXY`` (urllib's
  default ``ProxyHandler`` would send it there when ``no_proxy`` does not list
  the host); a remote host keeps the environment's proxy settings.

Redirects are refused: urllib would copy the ``api-key`` header onto the next
request. A response is data: anything but a JSON object with ``status: ok``
and a ``result`` is an error with a fixed code; no body text is kept.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from typing import Any, NoReturn
from urllib.parse import quote, urlsplit

#: Largest response body read, in bytes.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
#: Bytes read per call while a body is read under the cap.
READ_CHUNK = 64 * 1024


class ResponseTooLarge(Exception):
    """A response body is larger than the cap (never an ``OSError``: not "unreachable")."""


def read_capped(response: Any, cap: int = MAX_RESPONSE_BYTES) -> bytes:
    """The whole body, refused as soon as it is known to exceed ``cap`` bytes.

    A ``Content-Length`` above the cap is refused before anything is read; then
    the body is read in chunks and refused the moment the running total passes
    the cap (a chunked or close-delimited body, or a ``Content-Length`` that
    lies), so the decision never depends on how the peer closes the socket.
    """

    headers = getattr(response, "headers", None)
    declared = headers.get("Content-Length") if headers is not None else None
    if declared is not None:
        try:
            if int(declared) > cap:
                raise ResponseTooLarge
        except ValueError:
            pass  # a malformed length: the running total still decides
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(READ_CHUNK, cap + 1 - total))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > cap:
            raise ResponseTooLarge
        chunks.append(chunk)


class QdrantError(Exception):
    """A failed Qdrant call; ``code`` is a fixed reason (never response text)."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def is_loopback_host(host: str | None) -> bool:
    """Whether a URL hostname (brackets stripped) is a loopback literal."""

    name = (host or "").strip().lower().removesuffix(".")
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not name:
        return False
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def check_base_url(url: str, *, allow_remote: bool) -> str:
    """The base URL without a trailing slash; ``ValueError`` for anything unsafe.

    ``http``/``https`` only, a host, no user info, query or fragment, no path
    beyond ``/``; a non-loopback host only with ``allow_remote``.
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be an http(s) URL")
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("url must be an http(s) URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("url must not carry credentials, a query or a fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("url must not have a path")
    try:
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError as exc:
        raise ValueError("url has a bad port") from exc
    if not is_loopback_host(parts.hostname) and not allow_remote:
        raise ValueError("url is not loopback; set allow_remote: true to use another host")
    return f"{parts.scheme}://{parts.netloc}"


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> NoReturn:
        del msg, newurl
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def open_url(request: urllib.request.Request, timeout: float) -> Any:
    """Open ``request``: redirects refused; a loopback host bypasses every proxy."""

    handlers: list[Any] = [_RefuseRedirects]
    if is_loopback_host(urlsplit(request.full_url).hostname):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


class QdrantClient:
    """The few REST calls the adapter makes (Qdrant v1.20 API), standard library only."""

    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 2.0) -> None:
        self.base_url = base_url
        self._api_key = api_key
        self.timeout = timeout

    def __repr__(self) -> str:  # never the key
        return f"QdrantClient({self.base_url!r})"

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        timeout: float | None = None,
        allow_missing: bool = False,
        envelope: bool = True,
    ) -> Any:
        """``result`` of one call. ``allow_missing``: a 404 gives ``None``.

        ``envelope=False`` is for ``GET /`` (server title and version), the
        one answer without the ``{"status": "ok", "result": ...}`` envelope.
        """

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, allow_nan=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["api-key"] = self._api_key
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with open_url(request, self.timeout if timeout is None else timeout) as response:
                raw = read_capped(response)
        except ResponseTooLarge:
            raise QdrantError("response_too_large") from None
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_missing:
                return None
            if exc.code in {401, 403}:
                raise QdrantError("unauthorized", status=exc.code) from None
            if 300 <= exc.code < 400:
                raise QdrantError("redirect_refused", status=exc.code) from None
            raise QdrantError(f"http_{exc.code}", status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise QdrantError("unreachable") from None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise QdrantError("bad_response") from None
        if not isinstance(payload, dict):
            raise QdrantError("bad_response")
        if not envelope:
            return payload
        if payload.get("status") != "ok" or "result" not in payload:
            raise QdrantError("bad_response")
        return payload["result"]


def collection_path(collection: str, suffix: str = "") -> str:
    return f"/collections/{quote(collection, safe='')}{suffix}"


__all__ = [
    "MAX_RESPONSE_BYTES",
    "READ_CHUNK",
    "ResponseTooLarge",
    "QdrantClient",
    "QdrantError",
    "check_base_url",
    "collection_path",
    "is_loopback_host",
    "open_url",
    "read_capped",
]
