"""Outbound HTTP that refuses redirects and never proxies loopback requests.

A 3xx must not be followed. urllib's redirect handler copies headers such as
``Authorization`` onto the next request, including a redirect to another port.

A request to a loopback host (``localhost``, ``127.0.0.0/8``, ``::1``,
``::ffff:127.x``) goes direct: urllib's default ``ProxyHandler`` would send it
through ``HTTP(S)_PROXY`` when ``no_proxy`` does not list the host, so text
meant for a local service (embeddings, a local model) would reach the proxy.
Other hosts keep the environment's proxy settings.
"""

from __future__ import annotations

import ipaddress
import urllib.error
import urllib.request
from typing import Any, NoReturn
from urllib.parse import urlsplit

_LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback_host(host: str | None) -> bool:
    """Whether ``host`` (a URL hostname, brackets stripped) is a loopback literal."""

    name = (host or "").strip().lower().strip("[]").rstrip(".")
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
    """Open ``request`` with redirects disabled; a loopback request bypasses any proxy."""

    handlers: list[Any] = [RefusingRedirectHandler]
    if is_loopback_url(request.full_url):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(request, timeout=timeout)
