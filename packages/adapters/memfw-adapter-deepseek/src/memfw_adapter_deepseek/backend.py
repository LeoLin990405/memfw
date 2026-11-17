"""DeepSeek chat-completions backend for the memfw conversation worker (``conv-v2``).

What it sends: one ``POST {base_url}/chat/completions`` per task, with a
system message (the ``conv-v2`` instructions from the ports) and a user
message (the task data, which the worker has already redacted), JSON response
mode, ``temperature`` 0 and a capped ``max_tokens``. Nothing else: no ids, no
paths, no app names beyond what the redacted data holds.

Key handling: the API key is read only from the ``DEEPSEEK_API_KEY``
environment variable, once, by :func:`factory`. It is never read from a file
or from ``vault.yaml`` (an ``api_key`` option is refused), never logged, and
never part of a ``repr``, an exception message or a report. Errors carry
short codes only.

Budget: each backend instance (one worker run) has a token budget and a cost
budget. Before a request the worst case (estimated input plus ``max_tokens``)
must fit both; afterwards the reported usage is charged. When a request would
not fit, :class:`BackendBudgetExhausted` stops the run and the worker leaves
the job queued for the next one.

A request that timed out or broke off may still have been billed, so each such
attempt is charged its worst case (estimated input plus ``max_tokens``), and
every retry must fit the budget again.

Retries: HTTP 429 and 5xx (and network errors or timeouts) are retried with
exponential backoff (``Retry-After`` is honoured up to the cap). When the
retries are used up the run stops with :class:`BackendUnavailable`
(``transient``): the worker re-queues the job without spending one of its
attempts. Other 4xx are not retried (:class:`DeepSeekError`). 401/403 raise
:class:`BackendUnavailable` (``auth``). The worker may call
:meth:`DeepSeekBackend.limit_time` with the seconds left in its run; a request
or backoff that would overrun it stops the run (``deadline``).

Host: only ``https://api.deepseek.com`` (:data:`ALLOWED_BASE_URLS`), or plain
http to a loopback address for local tests. Redirects are refused, so the key
is never sent to another host.

Prices: the per-million-token defaults are placeholders, not the provider's
current prices; the operator must set ``price_in_per_mtok`` and
``price_out_per_mtok`` for the cost budget to mean anything.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any
from urllib.parse import urlsplit

from ._ports import (
    TAG_PROMPT_VERSION,
    TAG_TASKS,
    BackendBudgetExhausted,
    BackendUnavailable,
    ModelReply,
    render_tag_prompt,
)

__version__ = "0.1.1"

KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_BASE_URL = "https://api.deepseek.com"
#: The only remote endpoints the key may be sent to (anything else is refused, so
#: editing vault.yaml cannot redirect the key to another host).
ALLOWED_BASE_URLS = frozenset({DEFAULT_BASE_URL})
DEFAULT_MODEL = "deepseek-chat"
USER_AGENT = f"memfw-adapter-deepseek/{__version__}"
RESPONSE_BYTES = 1 << 20
#: Options that would put a credential in configuration: always refused.
FORBIDDEN_OPTIONS = frozenset({"api_key", "key", "token", "authorization", "password"})


class DeepSeekError(RuntimeError):
    """A request failed after its retries. ``code`` (e.g. ``http_503``) is all it carries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Settings:
    """Everything but the key. Prices are USD per million tokens. The defaults are
    PLACEHOLDERS, not verified current prices: set both from the provider's price
    list (cache-hit discounts are ignored, so cost is over-estimated)."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 60.0
    max_retries: int = 3
    backoff: float = 1.0
    max_backoff: float = 30.0
    max_output_tokens: int = 512
    max_run_tokens: int = 400_000
    max_run_cost_usd: float = 0.50
    price_in_per_mtok: float = 0.28
    price_out_per_mtok: float = 0.42

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model or len(self.model) > 64:
            raise BackendUnavailable("bad_option")
        _check_base_url(self.base_url)
        for name in ("timeout", "backoff", "max_backoff", "max_run_cost_usd"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise BackendUnavailable("bad_option")
        if not math.isfinite(self.max_run_cost_usd) or self.timeout <= 0:
            raise BackendUnavailable("bad_option")
        for name in ("price_in_per_mtok", "price_out_per_mtok"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise BackendUnavailable("bad_option")
        for name in ("max_retries", "max_output_tokens", "max_run_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BackendUnavailable("bad_option")
        if self.max_output_tokens < 1 or self.max_retries > 10:
            raise BackendUnavailable("bad_option")

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> Settings:
        options = dict(options or {})
        if FORBIDDEN_OPTIONS & {str(name).lower() for name in options}:
            # Keys come from the environment only, never from vault.yaml.
            raise BackendUnavailable("key_in_config")
        known = {item.name for item in fields(cls)}
        if set(options) - known:
            raise BackendUnavailable("bad_option")
        return replace(cls(), **options)


def _check_base_url(url: Any) -> None:
    """One of :data:`ALLOWED_BASE_URLS`, or plain http to a loopback host (local tests)."""

    if not isinstance(url, str) or len(url) > 200:
        raise BackendUnavailable("bad_base_url")
    if url.rstrip("/") in ALLOWED_BASE_URLS:
        return
    parts = urlsplit(url)
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise BackendUnavailable("bad_base_url")
    if parts.scheme == "http" and _loopback(parts.hostname):
        return
    raise BackendUnavailable("bad_base_url")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Key:
    """Holds the key; prints as ``***`` and cannot be pickled or compared."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def header(self) -> str:
        return f"Bearer {self._value}"

    def __repr__(self) -> str:
        return "***"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("the API key cannot be pickled")

    def __eq__(self, other: object) -> bool:
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: the Authorization header must not follow it elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def estimate_tokens(text: str) -> int:
    """A conservative token estimate (about 3 characters per token, CJK about 1)."""

    wide = sum(1 for char in text if ord(char) > 0x2E80)
    return 1 + (len(text) - wide) // 3 + wide


Opener = Callable[[urllib.request.Request, float], Any]


class DeepSeekBackend:
    """A ``conv-v2`` :class:`~memfw.ports.models.ModelBackend` over DeepSeek's API."""

    local = False
    prompt_version = TAG_PROMPT_VERSION
    tasks = TAG_TASKS

    def __init__(
        self,
        settings: Settings,
        *,
        key: str,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(key, str) or not key.strip():
            raise BackendUnavailable("missing_key")
        self.settings = settings
        self.name = f"deepseek:{settings.model}"
        self._key = _Key(key.strip())
        self._sleep = sleep
        self._clock = clock
        self._deadline: float | None = None
        self._opener = opener or self._default_opener(settings.base_url)
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.stopped = ""

    def __repr__(self) -> str:
        settings = self.settings
        return f"DeepSeekBackend(model={settings.model!r}, base_url={settings.base_url!r})"

    @staticmethod
    def _default_opener(base_url: str) -> Opener:
        handlers: list[Any] = [_NoRedirect()]
        if _loopback(urlsplit(base_url).hostname or ""):
            handlers.append(urllib.request.ProxyHandler({}))  # never proxy a local test server
        built = urllib.request.build_opener(*handlers)
        return lambda request, timeout: built.open(request, timeout=timeout)

    def limit_time(self, seconds: float) -> None:
        """Bound the next :meth:`run` (attempts, backoff) to ``seconds`` from now."""

        self._deadline = self._clock() + max(0.0, float(seconds))

    def _time_left(self) -> float | None:
        return None if self._deadline is None else self._deadline - self._clock()

    # -- budget ---------------------------------------------------------------------

    def _cost(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in * self.settings.price_in_per_mtok
            + tokens_out * self.settings.price_out_per_mtok
        ) / 1_000_000

    def _reserve(self, estimate_in: int, max_out: int) -> None:
        tokens = self.tokens_in + self.tokens_out + estimate_in + max_out
        if tokens > self.settings.max_run_tokens:
            self.stopped = "backend_tokens"
            raise BackendBudgetExhausted("backend_tokens")
        if self.cost_usd + self._cost(estimate_in, max_out) > self.settings.max_run_cost_usd:
            self.stopped = "backend_cost"
            raise BackendBudgetExhausted("backend_cost")

    def usage(self) -> dict[str, Any]:
        """This run's counts (no text)."""

        return {
            "requests": self.requests,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "stopped": self.stopped,
        }

    # -- the call ---------------------------------------------------------------------

    def request_body(self, task: str, data: dict[str, Any], *, max_tokens: int) -> dict[str, Any]:
        """The exact JSON body sent for ``task`` (public so its shape can be checked)."""

        if task not in TAG_TASKS:
            raise ValueError(f"deepseek backend does not take task {task}")
        system, user = render_tag_prompt(task, data)
        return {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": max(1, min(int(max_tokens), self.settings.max_output_tokens)),
            "temperature": 0,
            "stream": False,
        }

    def run(self, task: str, data: dict[str, Any], *, max_tokens: int) -> ModelReply:
        body = self.request_body(task, data, max_tokens=max_tokens)
        prompt_text = "".join(str(message["content"]) for message in body["messages"])
        worst = (estimate_tokens(prompt_text), int(body["max_tokens"]))
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        reply = self._post(payload, worst)
        try:
            content = reply["choices"][0]["message"]["content"]
            usage = reply.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError):
            raise DeepSeekError("bad_response") from None
        if not isinstance(content, str):
            raise DeepSeekError("bad_response")
        tokens_in = max(0, tokens_in) or estimate_tokens(prompt_text)
        tokens_out = max(0, tokens_out) or estimate_tokens(content)
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += self._cost(tokens_in, tokens_out)
        return ModelReply(text=content, tokens_in=tokens_in, tokens_out=tokens_out)

    def _charge(self, tokens_in: int, tokens_out: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += self._cost(tokens_in, tokens_out)

    def _post(self, payload: bytes, worst: tuple[int, int]) -> dict[str, Any]:
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        attempt = 0
        while True:
            # Every attempt (retries too) must fit the budget in its worst case.
            self._reserve(*worst)
            left = self._time_left()
            if left is not None and left <= 0:
                raise BackendUnavailable("deadline")
            request = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "Authorization": self._key.header(),
                },
            )
            timeout = self.settings.timeout if left is None else min(self.settings.timeout, left)
            code, delay = self._attempt(request, timeout)
            del request
            if isinstance(code, dict):
                return code
            if code == "network":
                # It may have reached the model and been billed: charge the worst case.
                self._charge(*worst)
            if delay is None:
                raise DeepSeekError(code)
            if attempt >= self.settings.max_retries:
                # Retryable, but the retries are used up: stop the run, keep the job.
                raise BackendUnavailable("transient")
            wait = min(self.settings.max_backoff, self.settings.backoff * (2**attempt))
            wait = max(wait, min(delay, self.settings.max_backoff))
            left = self._time_left()
            if left is not None and wait >= left:
                raise BackendUnavailable("deadline")
            self._sleep(wait)
            attempt += 1

    def _attempt(self, request: urllib.request.Request, timeout: float) -> tuple[Any, float | None]:
        """``(reply, None)``, or ``(code, retry delay or None when not retryable)``."""

        self.requests += 1
        try:
            with self._opener(request, timeout) as response:
                raw = response.read(RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            retry_after = _retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            exc.close()
            if status in (401, 403):
                raise BackendUnavailable("auth") from None
            if status == 429 or 500 <= status < 600:
                return f"http_{status}", retry_after
            return f"http_{status}", None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            return "network", 0.0
        if len(raw) > RESPONSE_BYTES:
            return "response_too_large", None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "bad_response", None
        if not isinstance(value, dict):
            return "bad_response", None
        return value, None


def _retry_after(raw: Any) -> float:
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def factory(config: Mapping[str, Any] | None = None) -> DeepSeekBackend:
    """The backend for ``vault.yaml`` ``conversations: {backend:
    "memfw_adapter_deepseek:factory", cloud: true, options: {...}}``.

    Raises :class:`BackendUnavailable` (``missing_key``) when ``DEEPSEEK_API_KEY``
    is unset or empty; options are :class:`Settings` fields.
    """

    settings = Settings.from_options(config)
    key = os.environ.get(KEY_ENV, "")
    if not key.strip():
        raise BackendUnavailable("missing_key")
    return DeepSeekBackend(settings, key=key)
