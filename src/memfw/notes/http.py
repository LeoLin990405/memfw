"""Outbound HTTP that refuses redirects and never proxies loopback requests.

A 3xx must not be followed. urllib's redirect handler copies headers such as
``Authorization`` onto the next request, including a redirect to another port.

A request to a host on this machine goes direct: urllib's default
``ProxyHandler`` would send it through ``HTTP(S)_PROXY`` when ``no_proxy`` does
not list the host, so text meant for a local service (embeddings, a local
model, a local judge) would reach the proxy. Other hosts keep the
environment's proxy settings.

Two rules, both on the literal host (never DNS):

* :func:`is_loopback_host`, the strict one for privacy decisions ("this text
  stays on the machine"): ``localhost``, canonical ``127.0.0.0/8``, ``::1``,
  ``::ffff:127.x``.
* :func:`bypasses_proxy`, the one ``open_http`` uses: the above, plus IPv4
  forms the socket layer also reads as 127/8 (``127.1``, ``2130706433``,
  ``0x7f.0.0.1``) and the unspecified addresses ``0.0.0.0`` and ``::`` (they
  connect to this host). Going direct for these is always safe; it is not a
  claim that the endpoint is loopback.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from typing import Any, NoReturn
from urllib.parse import urlsplit

_LOOPBACK_NAMES = frozenset({"localhost"})
#: What ``inet_aton`` may be shown: digits, hex digits, ``x`` and dots only.
_NUMERIC_IPV4 = re.compile(r"^[0-9a-fx.]{1,32}$")


def _host_name(host: str | None) -> str:
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    return name.removesuffix(".")


def is_loopback_host(host: str | None) -> bool:
    """Whether ``host`` (a URL hostname, brackets stripped) is a loopback literal."""

    name = _host_name(host)
    if not name:
        return False
    if name in _LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def is_loopback_url(url: str) -> bool:
    """Whether ``url``'s host is a loopback literal (an unparseable URL is not)."""

    try:
        return is_loopback_host(urlsplit(url).hostname)
    except ValueError:
        return False


def embedder_sends_off_host(embedder: object) -> bool:
    """Whether ``embedder`` sends document text to a service that may be off this machine.

    ``False`` for a provider that says ``local is True``, for an in-process
    provider (no ``endpoint`` attribute, such as ``HashEmbeddingProvider``)
    and for an ``endpoint`` URL whose host is a loopback literal
    (:func:`is_loopback_url`, the strict privacy rule, never DNS). ``True``
    for any other ``endpoint`` (another host, an empty or unparseable URL,
    a value that is not a string): the memory embedding gate then embeds
    only ``public``/``shared`` memories unless the operator opts in
    (``MEMFW_EMBEDDING_ALLOW_REMOTE`` / ``runtime.embedding.allow_remote``).
    """

    if getattr(embedder, "local", None) is True:
        return False
    if not hasattr(embedder, "endpoint"):
        return False
    endpoint = getattr(embedder, "endpoint", None)
    return not (isinstance(endpoint, str) and endpoint and is_loopback_url(endpoint))


def bypasses_proxy(host: str | None) -> bool:
    """Whether a request to ``host`` must go direct (see the module docstring)."""

    if is_loopback_host(host):
        return True
    name = _host_name(host)
    if not name:
        return False
    try:
        if ipaddress.ip_address(name.split("%", 1)[0]).is_unspecified:
            return True
    except ValueError:
        pass
    if _NUMERIC_IPV4.match(name) and name[0].isdigit():
        try:
            packed = socket.inet_aton(name)
        except OSError:
            return False
        return packed[0] == 127 or packed == b"\0\0\0\0"
    return False


def _direct_url(url: str) -> bool:
    try:
        return bypasses_proxy(urlsplit(url).hostname)
    except ValueError:
        return False


class RefusingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise on every 3xx instead of building a follow-up request."""

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

    def _refuse(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> NoReturn:
        del msg
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


for _status in range(300, 400):
    setattr(RefusingRedirectHandler, f"http_error_{_status}", RefusingRedirectHandler._refuse)


def open_http(request: urllib.request.Request, timeout: float) -> Any:
    """Open ``request`` with redirects disabled; a request to this host bypasses any proxy."""

    handlers: list[Any] = [RefusingRedirectHandler]
    if _direct_url(request.full_url):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)
