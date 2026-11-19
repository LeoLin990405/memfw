"""A local ``ModelBackend`` over an Ollama server on loopback, for :data:`~.deriver.TASK`.

``memfw_adapter_wikigen.ollama:factory`` is the default backend. It is meant
to run on the worker machine that holds the model (design 6.2): the model
never leaves that machine and no tunnel is needed.

* ``run`` sends one ``POST {base_url}/api/generate`` (``stream: false``,
  ``format: "json"``, the deriver's ``system`` and ``prompt``, and
  ``options`` = the deriver's ``temperature`` / ``seed`` / ``num_ctx`` plus
  ``num_predict`` = the call's ``max_tokens``) and returns the model text with
  Ollama's token counts.
* ``model_digest`` asks ``GET {base_url}/api/tags`` for the model's digest
  (``sha256:<hex>``); a model the server does not list is ``model_missing``,
  and a digest other than a pinned ``model_digest`` option is
  ``model_digest_mismatch``. The deriver records the digest in every page and
  compares it with the job's, so a worker never silently swaps models.

The base URL must be plain ``http`` to a *literal* loopback address
(``127.0.0.1``, any ``127.x``, ``::1``); a host name (even ``localhost``),
credentials, a query or a fragment is refused (``bad_base_url``). Proxy
variables are ignored for it (an empty ``ProxyHandler``) and redirects are
refused. No key, no telemetry. Errors carry short codes only.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
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
USER_AGENT = f"memfw-adapter-wikigen/{__version__}"
_HEX64 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")


def check_loopback_url(url: Any) -> str:
    """``url`` without a trailing slash when it is plain http to a literal loopback IP."""

    if not isinstance(url, str) or len(url) > 200:
        raise BackendUnavailable("bad_base_url")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - a bad port raises here
    except ValueError:
        raise BackendUnavailable("bad_base_url") from None
    if (
        parts.scheme != "http"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or not host
    ):
        raise BackendUnavailable("bad_base_url")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False  # a name, even "localhost", could resolve anywhere
    if not loopback:
        raise BackendUnavailable("bad_base_url")
    return url.rstrip("/")


@dataclass(frozen=True)
class OllamaSettings:
    model: str = "qwen2.5:7b-instruct"
    base_url: str = DEFAULT_BASE_URL
    #: Seconds per generate request (the deriver may pass a shorter one).
    timeout: float = 900.0
    #: Seconds for the ``/api/tags`` probe.
    probe_timeout: float = 5.0
    #: Optional pin ``sha256:<hex>``: any other digest on the server is refused.
    model_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not _MODEL_RE.fullmatch(self.model):
            raise BackendUnavailable("bad_option")
        check_loopback_url(self.base_url)
        for value in (self.timeout, self.probe_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise BackendUnavailable("bad_option")
        if not isinstance(self.model_digest, str) or (
            self.model_digest and not self.model_digest.startswith("sha256:")
        ):
            raise BackendUnavailable("bad_option")
        if self.model_digest and not _HEX64.fullmatch(self.model_digest):
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
        self.model = settings.model
        self.name = f"ollama:{settings.model}"
        self._base = check_loopback_url(settings.base_url)
        self._opener = opener or loopback_opener()
        self.requests = 0

    def __repr__(self) -> str:
        return f"OllamaBackend(model={self.settings.model!r}, base_url={self.settings.base_url!r})"

    def _call(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests += 1
        try:
            with self._opener(request, timeout) as response:
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
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise BackendUnavailable("bad_response") from None

    def model_digest(self) -> str:
        """``sha256:<hex>`` of :attr:`model` as the server lists it."""

        request = urllib.request.Request(
            self._base + "/api/tags", method="GET", headers={"User-Agent": USER_AGENT}
        )
        value = self._call(request, self.settings.probe_timeout)
        models = value.get("models") if isinstance(value, dict) else None
        if not isinstance(models, list):
            raise BackendUnavailable("bad_response")
        wanted = {self.model} if ":" in self.model else {self.model, self.model + ":latest"}
        for entry in models:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") in wanted or entry.get("model") in wanted:
                match = _HEX64.fullmatch(str(entry.get("digest", "")))
                if match is None:
                    raise BackendUnavailable("bad_response")
                digest = "sha256:" + match.group(1)
                pinned = self.settings.model_digest
                if pinned and pinned != digest:
                    raise BackendUnavailable("model_digest_mismatch")
                return digest
        raise BackendUnavailable("model_missing")

    def request_body(
        self, task: str, data: Mapping[str, Any], *, max_tokens: int
    ) -> dict[str, Any]:
        if task != TASK:
            raise ValueError(f"ollama backend does not take task {task}")
        prompt, system = data.get("prompt"), data.get("system", "")
        if not isinstance(prompt, str) or not isinstance(system, str):
            raise ValueError("data must hold 'prompt' (and optional 'system') strings")
        options = data.get("options", {})
        if not isinstance(options, Mapping) or set(options) - {"temperature", "seed", "num_ctx"}:
            raise ValueError("options may hold temperature, seed and num_ctx only")
        body: dict[str, Any] = {
            "model": self.settings.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": options.get("temperature", 0),
                "seed": options.get("seed", 0),
                **({"num_ctx": options["num_ctx"]} if "num_ctx" in options else {}),
                "num_predict": max(1, int(max_tokens)),
            },
        }
        if system:
            body["system"] = system
        return body

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        body = json.dumps(self.request_body(task, data, max_tokens=max_tokens)).encode("utf-8")
        timeout = self.settings.timeout
        asked = data.get("timeout")
        if isinstance(asked, (int, float)) and not isinstance(asked, bool) and asked > 0:
            timeout = min(timeout, float(asked))
        request = urllib.request.Request(
            self._base + "/api/generate",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        value = self._call(request, timeout)
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
    """The backend for ``backend: "memfw_adapter_wikigen.ollama:factory"``."""

    return OllamaBackend(OllamaSettings.from_options(options))


__all__ = [
    "DEFAULT_BASE_URL",
    "OllamaBackend",
    "OllamaSettings",
    "check_loopback_url",
    "factory",
    "loopback_opener",
]
