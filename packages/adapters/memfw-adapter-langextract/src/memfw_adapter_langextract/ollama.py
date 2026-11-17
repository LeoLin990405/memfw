"""A local ``ModelBackend`` over an Ollama server on loopback, for :data:`~.deriver.TASK`.

``memfw_adapter_langextract.ollama:factory`` is the default backend to name in
the adapter settings. It sends one ``POST {base_url}/api/generate`` per call
(``stream: false``, ``temperature`` 0, ``num_predict`` = the call's
``max_tokens``) with the prompt LangExtract built, and returns the model text
plus Ollama's token counts.

The base URL must be plain ``http`` to a loopback address (``127.0.0.1``,
``::1`` or ``localhost``): the backend is ``local``, and anything else is
refused (``bad_base_url``), so no setting can point it at another host. Proxy
variables are ignored for it and redirects are refused. No key, no telemetry.
Errors carry short codes only.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any
from urllib.parse import urlsplit

from ._ports import BackendUnavailable, ModelReply
from .deriver import TASK, __version__

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
RESPONSE_BYTES = 1 << 20
USER_AGENT = f"memfw-adapter-langextract/{__version__}"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_loopback_url(url: Any) -> str:
    """``url`` without a trailing slash when it is plain http to a loopback host."""

    if not isinstance(url, str) or len(url) > 200:
        raise BackendUnavailable("bad_base_url")
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not parts.hostname
        or not _loopback(parts.hostname)
    ):
        raise BackendUnavailable("bad_base_url")
    return url.rstrip("/")


@dataclass(frozen=True)
class OllamaSettings:
    model: str = "qwen2.5:7b-instruct"
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 120.0

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model or len(self.model) > 64:
            raise BackendUnavailable("bad_option")
        check_loopback_url(self.base_url)
        timeout = self.timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise BackendUnavailable("bad_option")

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> OllamaSettings:
        options = dict(options or {})
        if set(options) - {item.name for item in fields(cls)}:
            raise BackendUnavailable("bad_option")
        return replace(cls(), **options)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


Opener = Callable[[urllib.request.Request, float], Any]


def loopback_opener() -> Opener:
    """An opener that never uses a proxy and never follows a redirect."""

    built = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return lambda request, timeout: built.open(request, timeout=timeout)


class OllamaBackend:
    """``ModelBackend`` for :data:`TASK` only; ``local`` (loopback Ollama)."""

    local = True
    tasks = (TASK,)

    def __init__(self, settings: OllamaSettings, *, opener: Opener | None = None) -> None:
        self.settings = settings
        self.name = f"ollama:{settings.model}"
        self._url = check_loopback_url(settings.base_url) + "/api/generate"
        self._opener = opener or loopback_opener()
        self.requests = 0

    def __repr__(self) -> str:
        return f"OllamaBackend(model={self.settings.model!r}, base_url={self.settings.base_url!r})"

    def request_body(
        self, task: str, data: Mapping[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        if task != TASK:
            raise ValueError(f"ollama backend does not take task {task}")
        prompt = data.get("prompt")
        if not isinstance(prompt, str):
            raise ValueError("data must hold a 'prompt' string")
        return {
            "model": self.settings.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0, "num_predict": max(1, int(max_tokens))},
        }

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        body = json.dumps(self.request_body(task, data, max_tokens=max_tokens)).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        self.requests += 1
        try:
            with self._opener(request, self.settings.timeout) as response:
                raw = response.read(RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = int(exc.code)
            exc.close()
            raise BackendUnavailable(f"http_{code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise BackendUnavailable("unreachable") from None
        if len(raw) > RESPONSE_BYTES:
            raise BackendUnavailable("response_too_large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BackendUnavailable("bad_response") from None
        text = value.get("response") if isinstance(value, dict) else None
        if not isinstance(text, str):
            raise BackendUnavailable("bad_response")
        return ModelReply(
            text=text,
            tokens_in=_count(value.get("prompt_eval_count")),
            tokens_out=_count(value.get("eval_count")),
        )


def _count(raw: Any) -> int:
    return raw if type(raw) is int and raw >= 0 else 0


def factory(options: Mapping[str, Any] | None = None) -> OllamaBackend:
    """The backend for ``backend: "memfw_adapter_langextract.ollama:factory"``."""

    return OllamaBackend(OllamaSettings.from_options(options))


__all__ = [
    "DEFAULT_BASE_URL",
    "OllamaBackend",
    "OllamaSettings",
    "check_loopback_url",
    "factory",
    "loopback_opener",
]
