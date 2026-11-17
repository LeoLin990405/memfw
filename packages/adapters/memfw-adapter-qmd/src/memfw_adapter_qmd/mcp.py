"""A small MCP JSON-RPC client for QMD's Streamable HTTP endpoint. Stdlib only.

QMD (``qmd mcp --http``) serves MCP at ``/mcp``. A client sends
``initialize``, keeps the ``Mcp-Session-Id`` response header, sends
``notifications/initialized`` and then ``tools/call``. QMD 2.5.3 answers with
plain JSON (``enableJsonResponse``); an ``text/event-stream`` answer is read too.
QMD restarts its server for each new index generation, so a call that gets
``404`` for its session opens a new session once and retries.

Network rules (memfw compat framework section 5):

* The endpoint comes only from adapter settings, never from a query.
* By default it must be a loopback literal (``localhost``, ``127.0.0.0/8``,
  ``::1``); ``allow_remote`` lifts that. A loopback request never goes through
  ``HTTP(S)_PROXY`` (the proxy handler is replaced by an empty one), so query
  text meant for this machine never reaches a proxy.
* Redirects are refused; a response body larger than ``MAX_RESPONSE_BYTES`` is
  an error; every request carries the time left of the caller's deadline as its
  socket timeout, and the body is read in chunks with that timeout shrunk to
  the time left before each one; a watchdog timer also shuts the request's
  socket down at the deadline, so the whole exchange (connect, headers, body;
  not each ``recv``) ends by the deadline.

Errors: anything that means "QMD cannot answer now" (refused connection,
timeout, HTTP 5xx, a busy client) raises ``ProviderUnavailable``; an answer
that breaks the protocol raises :class:`QmdResponseError`. Neither message
carries query or response text.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, NoReturn
from urllib.parse import urlsplit

from ._ports import ProviderUnavailable

__all__ = [
    "MAX_RESPONSE_BYTES",
    "PROTOCOL_VERSION",
    "McpHttpClient",
    "QmdResponseError",
    "check_endpoint",
    "is_loopback_host",
]

#: The MCP revision this client asks for (QMD 2.5.3 ships MCP SDK 1.29.0).
PROTOCOL_VERSION = "2025-06-18"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ACCEPT = "application/json, text/event-stream"


class QmdResponseError(RuntimeError):
    """QMD answered, but not with what the protocol promises. No payload text."""


def is_loopback_host(host: str | None) -> bool:
    """Whether ``host`` (a URL hostname) is a loopback literal. Never resolves DNS."""

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


def check_endpoint(endpoint: str, *, allow_remote: bool) -> str:
    """``""`` when ``endpoint`` may be used, else a reason code."""

    if not isinstance(endpoint, str) or not endpoint:
        return "no_endpoint"
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        parts.port  # noqa: B018  (raises on a bad port)
    except ValueError:
        return "bad_endpoint"
    if parts.scheme not in ("http", "https") or not host:
        return "bad_endpoint"
    if parts.username is not None or parts.password is not None or parts.fragment:
        return "bad_endpoint"
    if any(char.isspace() for char in endpoint):
        return "bad_endpoint"
    if not allow_remote and not is_loopback_host(host):
        return "endpoint_not_loopback"
    return ""


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> NoReturn:
        del msg, newurl
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_CHUNK = 64 * 1024


class _Sockets:
    """The sockets the current request opened, so a watchdog can cut them."""

    def __init__(self) -> None:
        self.open: list[socket.socket] = []

    def cut(self) -> None:
        for sock in list(self.open):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _TrackedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, sockets: _Sockets, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sockets = sockets

    def connect(self) -> None:
        super().connect()
        self._sockets.open.append(self.sock)


class _TrackedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, sockets: _Sockets, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sockets = sockets

    def connect(self) -> None:
        super().connect()
        self._sockets.open.append(self.sock)


class _TrackedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, sockets: _Sockets) -> None:
        super().__init__()
        self._sockets = sockets

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_TrackedHTTPConnection, req, sockets=self._sockets)


class _TrackedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, sockets: _Sockets) -> None:
        super().__init__()
        self._sockets = sockets

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(
            _TrackedHTTPSConnection,
            req,
            sockets=self._sockets,
            context=self._context,  # type: ignore[attr-defined]
        )


def _socket_of(response: Any) -> Any:
    """The socket under an ``http.client`` response, or ``None`` (best effort)."""

    raw = getattr(getattr(response, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


def _read_bounded(response: Any, *, deadline: float) -> bytes:
    """Read the body in chunks; the total, not each ``recv``, is bound by ``deadline``.

    Before each chunk the socket timeout shrinks to the time left, so a server
    that drips a byte at a time cannot hold the call (and the client lock)
    past the caller's budget. More than ``MAX_RESPONSE_BYTES`` is an error.
    """

    sock = _socket_of(response)
    reader = getattr(response, "read1", None) or response.read
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderUnavailable("timeout")
        if sock is not None:
            try:
                sock.settimeout(remaining)
            except OSError:
                pass
        try:
            chunk = reader(_CHUNK)
        except TimeoutError:
            raise ProviderUnavailable("timeout") from None
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise QmdResponseError("response too large")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


def _decode_message(body: bytes, content_type: str, request_id: int) -> dict[str, Any]:
    """The JSON-RPC response with ``request_id`` from a JSON or SSE body."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QmdResponseError("response is not UTF-8") from exc
    messages: list[Any] = []
    if content_type.startswith("text/event-stream"):
        data: list[str] = []
        for line in [*text.splitlines(), ""]:
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif not line and data:
                messages.append(_loads("\n".join(data)))
                data = []
    else:
        loaded = _loads(text)
        messages.extend(loaded if isinstance(loaded, list) else [loaded])
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            if message.get("jsonrpc") != "2.0":
                raise QmdResponseError("not a JSON-RPC 2.0 response")
            return message
    raise QmdResponseError("no response for the request id")


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise QmdResponseError("response is not JSON") from exc


class McpHttpClient:
    """One MCP session with one QMD endpoint; calls are serialised by a lock."""

    def __init__(
        self,
        endpoint: str,
        *,
        allow_remote: bool = False,
        client_name: str = "memfw-adapter-qmd",
        client_version: str = "0",
    ) -> None:
        reason = check_endpoint(endpoint, allow_remote=allow_remote)
        if reason:
            raise ValueError(f"endpoint refused: {reason}")
        self.endpoint = endpoint
        self.client_name = client_name
        self.client_version = client_version
        self._sockets = _Sockets()
        handlers: list[Any] = [
            _RefuseRedirects,
            _TrackedHTTPHandler(self._sockets),
            _TrackedHTTPSHandler(self._sockets),
        ]
        if is_loopback_host(urlsplit(endpoint).hostname):
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)
        self._lock = threading.Lock()
        self._session: str | None = None
        self._initialized = False
        self._protocol = PROTOCOL_VERSION
        self._next_id = 0
        #: Server name/version from the last ``initialize`` (``serverInfo``).
        self.server_info: dict[str, str] = {}

    # -- transport -------------------------------------------------------

    def _post(
        self, payload: Mapping[str, Any], *, deadline: float
    ) -> tuple[int, Mapping[str, str], bytes, str]:
        """One POST, cut at ``deadline`` whatever phase it is in.

        Socket timeouts bound each ``recv``, not the call: a server that sends
        its status line and headers a line at a time could hold the call (and
        the client lock) long after the deadline. A watchdog timer shuts the
        request's socket down at the deadline (connect, headers and body
        alike); whatever the cut produces, the call reports ``timeout`` and
        never a truncated answer.
        """

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderUnavailable("timeout")
        fired = threading.Event()

        def cut() -> None:
            fired.set()
            self._sockets.cut()

        self._sockets.open.clear()
        watchdog = threading.Timer(remaining, cut)
        watchdog.daemon = True
        watchdog.start()
        try:
            answer = self._post_once(payload, deadline=deadline)
        except (ProviderUnavailable, QmdResponseError):
            if fired.is_set():
                raise ProviderUnavailable("timeout") from None
            raise
        finally:
            watchdog.cancel()
            self._sockets.open.clear()
        if fired.is_set():
            raise ProviderUnavailable("timeout")
        return answer

    def _post_once(
        self, payload: Mapping[str, Any], *, deadline: float
    ) -> tuple[int, Mapping[str, str], bytes, str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderUnavailable("timeout")
        headers = {"Content-Type": "application/json", "Accept": _ACCEPT}
        if self._session is not None:
            headers["Mcp-Session-Id"] = self._session
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=remaining) as response:
                body = _read_bounded(response, deadline=deadline)
                status = int(response.status)
                content_type = str(response.headers.get("Content-Type") or "")
                found = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise QmdResponseError("redirect refused") from None
            if exc.code >= 500:
                raise ProviderUnavailable("server_error") from None
            try:
                body = _read_bounded(exc, deadline=deadline)
            except (OSError, QmdResponseError, ProviderUnavailable):
                body = b""
            return exc.code, {}, body, ""
        except TimeoutError:
            raise ProviderUnavailable("timeout") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ProviderUnavailable("timeout") from None
            raise ProviderUnavailable("unreachable") from None
        except (OSError, http.client.HTTPException):
            raise ProviderUnavailable("unreachable") from None
        return status, found, body, content_type

    def _request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _rpc(self, method: str, params: Mapping[str, Any], *, deadline: float) -> dict[str, Any]:
        request_id = self._request_id()
        status, headers, body, content_type = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
            deadline=deadline,
        )
        if status == 404 and self._session is not None:
            raise _SessionGone()
        if status != 200:
            raise QmdResponseError(f"HTTP {status}")
        message = _decode_message(body, content_type, request_id)
        if "error" in message:
            raise QmdResponseError("JSON-RPC error")
        result = message.get("result")
        if not isinstance(result, dict):
            raise QmdResponseError("JSON-RPC result is not an object")
        if method == "initialize":
            session = headers.get("mcp-session-id")
            self._session = session if isinstance(session, str) and session else None
        return result

    def _open_session(self, *, deadline: float) -> None:
        self._session = None
        self._initialized = False
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
            deadline=deadline,
        )
        protocol = result.get("protocolVersion")
        if not isinstance(protocol, str) or not protocol:
            raise QmdResponseError("initialize without protocolVersion")
        self._protocol = protocol
        info = result.get("serverInfo")
        self.server_info = (
            {k: str(info.get(k, "")) for k in ("name", "version")} if isinstance(info, dict) else {}
        )
        self._initialized = True
        status, _headers, _body, _type = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, deadline=deadline
        )
        if status not in (200, 202, 204):
            self._initialized = False
            raise QmdResponseError(f"HTTP {status} for notifications/initialized")

    # -- public ----------------------------------------------------------

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, deadline: float
    ) -> dict[str, Any]:
        """``tools/call`` → the tool result (``isError`` raises). Opens a session if needed."""

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise ProviderUnavailable("busy")
        try:
            for attempt in (0, 1):
                if not self._initialized:
                    self._open_session(deadline=deadline)
                try:
                    result = self._rpc(
                        "tools/call",
                        {"name": name, "arguments": dict(arguments)},
                        deadline=deadline,
                    )
                except _SessionGone:
                    self._initialized = False
                    self._session = None
                    if attempt:
                        raise QmdResponseError("session lost twice") from None
                    continue
                if result.get("isError") is True:
                    raise QmdResponseError("tool reported an error")
                return result
            raise QmdResponseError("unreachable")  # pragma: no cover
        except BaseException:
            if not self._initialized:
                self._session = None
            raise
        finally:
            self._lock.release()

    def reset(self) -> None:
        """Forget the session (the next call opens a new one)."""

        with self._lock:
            self._session = None
            self._initialized = False


class _SessionGone(Exception):
    """The server no longer knows our session (it restarted)."""
