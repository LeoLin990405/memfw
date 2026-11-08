"""Outbound HTTP that refuses redirects.

A 3xx must not be followed. urllib's redirect handler copies headers such as
``Authorization`` onto the next request, including a redirect to another port.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any, NoReturn


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
    """Open ``request`` with redirects disabled."""

    opener = urllib.request.build_opener(RefusingRedirectHandler)
    return opener.open(request, timeout=timeout)
