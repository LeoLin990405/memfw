"""The wikigen ``Deriver``: one ``memfw-wiki-job/1`` package -> one page candidate.

What one :meth:`WikigenDeriver.generate` call does (design 1, 2, 5.3, 6):

1. **Identity.** The job names the generator it was built for
   (``generator``, ``generator_version``, ``model``, ``model_digest``,
   ``prompt_version``; all part of its ``job_digest``). The deriver computes
   its own from its settings and the backend (the model tag, the digest the
   backend reports, and a ``prompt_version`` that spells out the prompt
   template version *and* the sampling settings:
   ``wiki-v1;t=<temperature>;s=<seed>;ctx=<num_ctx>``) and refuses the job
   on any difference (``model_digest_mismatch``, ``generator_mismatch:<field>``):
   it never swaps in another model or prompt.
2. **Privacy.** A job carries released (``public``/``shared``) text only;
   the generator never reads the vault. A backend whose ``local`` is not
   ``True`` needs ``cloud: true`` *and* a job whose sources are all
   ``public`` (``cloud_needs_public``).
3. **Prompt.** Fixed instructions (:mod:`.prompt`) plus the job's data block.
   Before the call, the estimated prompt plus ``max_output_tokens`` must fit
   ``num_ctx`` (``context_too_small``: Ollama would otherwise cut the prompt
   silently) and the run's token budget (``BackendBudgetExhausted``).
4. **Model.** ``backend.run(TASK, {"system", "prompt", "options": {"temperature",
   "seed", "num_ctx"}, "timeout"}, max_tokens=max_output_tokens)``. A reply
   whose reported tokens reach ``num_ctx`` is refused (``context_overflow``).
   An unusable reply (not one JSON object of the page shape) is retried, with
   the same seed, up to ``max_attempts`` in all, inside ``deadline_seconds``
   and the budget; then ``bad_output:<code>``.
5. **Page.** :mod:`.assemble` keeps only sentences that cite the job's own
   refs exactly and obey the section rules, renders the ``open`` conflicts,
   the sources and the privacy level itself, and runs the result through the
   ports parser and ``check_wiki_page(page, job=job)``. No summary sentence
   left: ``summary_empty``. Anything the ports refuse: ``page_invalid:<code>``.
   Nothing is ever half-returned.
6. **Optional pre-check.** ``precheck: "module:function"`` names a callable
   ``(WikiPage, WikiJob) -> WikiPage | None`` (for example a thin wrapper
   around ``memfw-wiki-gate`` once it exists). It runs only when importable;
   its result is checked against the job again. The NAS re-runs every
   deterministic check at intake regardless.

The page records the model tag and digest, the prompt version with the
sampling settings, the checker version and view version of the job, and the
job digest. ``build.nli_model`` is ``none`` and claims carry no NLI score:
entailment scoring is a separate step (``memfw-adapter-nli``).

``derive(vault, DeriveRequest("wiki_page", (), {"job": <JSON>}))`` (or
``{"job_path": <file>}``) wraps :meth:`generate` as a read-only
``DerivedView``; ``vault`` is never read. :func:`candidates` turns the view
into one ``IngestItem`` for memfw staging with ``proposal_id`` = the job
digest: a page is a candidate, never memory.
"""

from __future__ import annotations

import importlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from ._ports import (
    MAX_CITES_PER_CLAIM,
    MAX_CLAIM_CHARS,
    MAX_JOB_BYTES,
    AdapterSpec,
    Availability,
    BackendBudgetExhausted,
    BackendUnavailable,
    Citation,
    DerivedView,
    DeriveRequest,
    IngestItem,
    WikiFormatError,
    WikiJob,
    WikiPage,
    parse_wiki_job,
    parse_wiki_page,
)
from .assemble import BadOutput, PageRefused, assemble, validate
from .prompt import PROMPT_VERSION, build_prompt

__version__ = "0.1.0"

NAME = "wikigen"
#: The ``ModelBackend`` task this adapter sends (see the module docstring, step 4).
TASK = "wiki_page_generate"
KINDS: tuple[str, ...] = ("wiki_page",)
VIEW_FORMAT = "memfw-wikigen-view/1"
LICENSE = "Apache-2.0"
DEFAULT_BACKEND = "memfw_adapter_wikigen.ollama:factory"
#: ``build.nli_model`` until an entailment step scores the page.
NLI_NOT_RUN = "none"
GENERATOR_FIELDS: tuple[str, ...] = (
    "generator",
    "generator_version",
    "model",
    "model_digest",
    "prompt_version",
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SPEC_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")

#: Options that would put a credential in configuration: always refused.
FORBIDDEN_OPTIONS = frozenset({"api_key", "key", "token", "authorization", "password"})


class WikigenError(RuntimeError):
    """This job produced no page. ``code`` is short and never holds job or reply text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _number(value: Any, low: float, high: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and low <= value <= high
    )


def _integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


@dataclass(frozen=True)
class Settings:
    """The adapter's settings (``adapters.wikigen`` in the operator's config)."""

    #: ``module:factory`` of a memfw ``ModelBackend`` that lists :data:`TASK`.
    backend: str = DEFAULT_BACKEND
    #: Handed to the backend factory as its one argument.
    backend_options: Mapping[str, Any] | None = None
    #: Required for a backend whose ``local`` is not ``True``.
    cloud: bool = False
    #: Sampling: part of ``prompt_version`` and so of the job digest.
    temperature: float = 0.0
    seed: int = 0
    num_ctx: int = 16_384
    max_output_tokens: int = 2_048
    #: Token budget for one job, all attempts together.
    max_run_tokens: int = 48_000
    max_attempts: int = 2
    #: Seconds per model call, and for the whole job.
    request_timeout: float = 900.0
    deadline_seconds: float = 1_800.0
    max_claims_per_section: int = 12
    include_previous: bool = True
    #: Optional ``module:function`` pre-check, used only when importable.
    precheck: str = ""

    def __post_init__(self) -> None:
        checks = (
            isinstance(self.backend, str) and len(self.backend) <= 200,
            self.backend_options is None or isinstance(self.backend_options, Mapping),
            isinstance(self.cloud, bool),
            _number(self.temperature, 0.0, 2.0),
            _integer(self.seed, 0, 2**31 - 1),
            _integer(self.num_ctx, 2_048, 262_144),
            _integer(self.max_output_tokens, 256, 32_768),
            _integer(self.max_run_tokens, 1, 10_000_000),
            _integer(self.max_attempts, 1, 3),
            _number(self.request_timeout, 1.0, 86_400.0),
            _number(self.deadline_seconds, 1.0, 86_400.0),
            _integer(self.max_claims_per_section, 1, 50),
            isinstance(self.include_previous, bool),
            isinstance(self.precheck, str)
            and (not self.precheck or bool(_SPEC_RE.fullmatch(self.precheck))),
        )
        if not all(checks) or self.max_output_tokens >= self.num_ctx:
            raise ValueError("bad_option")

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any] | None) -> Settings:
        options = dict(settings or {})
        if FORBIDDEN_OPTIONS & {str(name).lower() for name in options}:
            raise ValueError("key_in_config")
        nested = options.get("backend_options")
        if isinstance(nested, Mapping) and FORBIDDEN_OPTIONS & {
            str(name).lower() for name in nested
        }:
            raise ValueError("key_in_config")
        if set(options) - {item.name for item in fields(cls)}:
            raise ValueError("bad_option")
        return replace(cls(), **options)

    def prompt_version(self) -> str:
        """The prompt template version plus every sampling setting that shapes the output."""

        return f"{PROMPT_VERSION};t={self.temperature:g};s={self.seed};ctx={self.num_ctx}"


def estimate_tokens(text: str) -> int:
    """A conservative token estimate (about 3 characters per token, CJK about 1)."""

    wide = sum(1 for char in text if ord(char) > 0x2E80)
    return 1 + (len(text) - wide) // 3 + wide


class RunBudget:
    """One job's token budget. Thread-safe; ``stopped`` holds the code once spent."""

    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens
        self.used = 0
        self.calls = 0
        self.stopped = ""
        self._lock = threading.Lock()

    def reserve(self, worst: int) -> None:
        with self._lock:
            if self.stopped or self.used + worst > self.max_tokens:
                self.stopped = "backend_tokens"
                raise BackendBudgetExhausted("backend_tokens")

    def charge(self, tokens: int) -> None:
        with self._lock:
            self.used += max(0, tokens)
            self.calls += 1


@dataclass(frozen=True)
class Generation:
    """A generated page and its report (counts and reason codes only, never text)."""

    page: WikiPage
    report: Mapping[str, Any] = field(default_factory=dict)


Precheck = Callable[[WikiPage, WikiJob], "WikiPage | None"]


def _import(spec: str) -> Any:
    module_name, _, attribute = spec.partition(":")
    return getattr(importlib.import_module(module_name), attribute)


def load_backend(settings: Settings) -> tuple[Any, str]:
    """``(backend, "")`` or ``(None, refusal code)``."""

    spec = settings.backend
    if not spec:
        return None, "no_backend"
    if not _SPEC_RE.fullmatch(spec):
        return None, "bad_backend_spec"
    try:
        factory_fn = _import(spec)
    except (ImportError, AttributeError):
        return None, "backend_not_importable"
    try:
        options = settings.backend_options
        backend = factory_fn() if options is None else factory_fn(dict(options))
    except BackendUnavailable as exc:
        return None, f"backend_unavailable:{exc.code}"[:64]
    except Exception:  # noqa: BLE001 - third-party factory
        return None, "backend_error"
    if not all(hasattr(backend, name) for name in ("name", "local", "run", "model")):
        return None, "backend_incomplete"
    if not callable(getattr(backend, "model_digest", None)):
        return None, "backend_incomplete"
    tasks = getattr(backend, "tasks", None)
    if tasks is None or TASK not in tasks:
        return None, "backend_task"
    if backend.local is not True and not settings.cloud:
        return None, "cloud_not_allowed"
    return backend, ""


def load_precheck(spec: str) -> tuple[Precheck | None, str]:
    """``(callable, "on")``, or ``(None, "off" | "not_importable")``."""

    if not spec:
        return None, "off"
    try:
        found = _import(spec)
    except (ImportError, AttributeError):
        return None, "not_importable"
    if not callable(found):
        return None, "not_importable"
    return found, "on"


class WikigenDeriver:
    """``Deriver`` ``wikigen`` (kind ``wiki_page``). See the module docstring."""

    def __init__(
        self,
        settings: Settings,
        *,
        backend: Any = None,
        refused: str = "",
        precheck: Precheck | None = None,
        precheck_status: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = NAME
        self.settings = settings
        self.backend = backend
        self.refused = refused if refused or backend is not None else "no_backend"
        self._precheck = precheck
        self.precheck_status = precheck_status or ("on" if precheck else "off")
        self._clock = clock
        self.last: Generation | None = None
        self.last_budget: RunBudget | None = None

    def __repr__(self) -> str:
        backend = getattr(self.backend, "name", None)
        return f"WikigenDeriver(backend={backend!r}, refused={self.refused!r})"

    def _digest(self) -> str:
        try:
            digest = self.backend.model_digest()
        except BackendUnavailable as exc:
            raise BackendUnavailable(f"backend_unavailable:{exc.code}"[:64]) from None
        except Exception:  # noqa: BLE001 - third-party backend
            raise BackendUnavailable("backend_error") from None
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise BackendUnavailable("bad_model_digest")
        return digest

    def available(self) -> Availability:
        """Usable now: a loaded backend whose model is there (asks it for the digest)."""

        if self.refused:
            return Availability.missing(self.refused)
        try:
            self._digest()
        except BackendUnavailable as exc:
            return Availability.missing(exc.code)
        return Availability.ok()

    def generator_config(self) -> dict[str, str]:
        """The ``generator`` block a job for this deriver must carry (job builders use it)."""

        if self.refused:
            raise BackendUnavailable(self.refused)
        return {
            "generator": NAME,
            "generator_version": __version__,
            "model": str(self.backend.model),
            "model_digest": self._digest(),
            "prompt_version": self.settings.prompt_version(),
        }

    def _check_job(self, job: WikiJob, config: Mapping[str, str]) -> None:
        for name in GENERATOR_FIELDS:
            if job.generator.get(name) != config[name]:
                if name == "model_digest":
                    raise WikigenError("model_digest_mismatch")
                raise WikigenError(f"generator_mismatch:{name}")
        if self.backend.local is not True:
            if not self.settings.cloud:  # load_backend refuses this already
                raise WikigenError("cloud_not_allowed")
            if any(item.source.privacy != "public" for item in job.sources):
                raise WikigenError("cloud_needs_public")

    def generate(self, job: WikiJob) -> Generation:
        """The page for ``job``, or an exception; never a partial page."""

        if self.refused:
            raise BackendUnavailable(self.refused)
        if type(job) is not WikiJob:
            raise WikigenError("bad_job")
        settings = self.settings
        config = self.generator_config()
        self._check_job(job, config)
        system, user = build_prompt(
            job,
            include_previous=settings.include_previous,
            max_claims=settings.max_claims_per_section,
            max_chars=MAX_CLAIM_CHARS,
            max_cites=MAX_CITES_PER_CLAIM,
        )
        estimate = estimate_tokens(system) + estimate_tokens(user)
        if estimate + settings.max_output_tokens > settings.num_ctx:
            raise WikigenError("context_too_small")
        build = {
            **config,
            "checker_version": job.checker_version,
            "nli_model": NLI_NOT_RUN,
            "view_version": job.view_version,
            "job_digest": job.job_digest,
        }
        budget = RunBudget(settings.max_run_tokens)
        self.last_budget = budget
        deadline = self._clock() + settings.deadline_seconds
        failures: list[str] = []
        page: WikiPage | None = None
        assembly: dict[str, Any] = {}
        for _ in range(settings.max_attempts):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise WikigenError("deadline")
            budget.reserve(estimate + settings.max_output_tokens)
            reply = self.backend.run(
                TASK,
                {
                    "system": system,
                    "prompt": user,
                    "options": {
                        "temperature": settings.temperature,
                        "seed": settings.seed,
                        "num_ctx": settings.num_ctx,
                    },
                    "timeout": min(settings.request_timeout, remaining),
                },
                max_tokens=settings.max_output_tokens,
            )
            text = getattr(reply, "text", None)
            tokens_in = _count(getattr(reply, "tokens_in", 0))
            tokens_out = _count(getattr(reply, "tokens_out", 0))
            if not isinstance(text, str):
                budget.charge(estimate + settings.max_output_tokens)
                failures.append("no_text")
                continue
            budget.charge((tokens_in or estimate) + (tokens_out or estimate_tokens(text)))
            if tokens_in and tokens_in + tokens_out >= settings.num_ctx:
                raise WikigenError("context_overflow")
            try:
                page, report = assemble(
                    text, job, build=build, max_claims=settings.max_claims_per_section
                )
            except BadOutput as exc:
                failures.append(exc.code)
                continue
            except PageRefused as exc:
                raise WikigenError(exc.code) from None
            assembly = report.to_json()
            break
        if page is None:
            raise WikigenError(f"bad_output:{failures[-1] if failures else 'none'}"[:64])
        page, precheck = self._run_precheck(page, job)
        result = Generation(
            page,
            {
                "attempts": len(failures) + 1,
                "failed_attempts": failures,
                "tokens_used": budget.used,
                "calls": budget.calls,
                "assembly": assembly,
                "precheck": precheck,
            },
        )
        self.last = result
        return result

    def _run_precheck(self, page: WikiPage, job: WikiJob) -> tuple[WikiPage, str]:
        if self._precheck is None:
            return page, self.precheck_status
        try:
            checked = self._precheck(page, job)
        except Exception:  # noqa: BLE001 - third-party check: fail closed, no partial page
            raise WikigenError("precheck_error") from None
        if checked is None:
            raise WikigenError("precheck_empty")
        try:
            return validate(checked, job), "ran"
        except PageRefused as exc:
            raise WikigenError(f"precheck_{exc.code}"[:64]) from None

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView:
        """``generate`` for the job in ``request.options`` (``job`` or ``job_path``)."""

        if request.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        availability = self.available()
        if not availability.available:
            raise BackendUnavailable(availability.reason)
        job = read_job(request.options)
        result = self.generate(job)
        page = result.page
        causal = max(
            (claim.causal for claim in page.claims),
            key=("none", "association", "hypothesis").index,
        )
        return DerivedView(
            deriver=self.name,
            kind=request.kind,
            content=view_content(result),
            citations=tuple(Citation.from_ref(source.ref) for source in page.sources),
            epistemic_cap="inference",
            causal_cap=causal,
        )


def _count(raw: Any) -> int:
    return raw if type(raw) is int and raw >= 0 else 0


def read_job(options: Mapping[str, Any]) -> WikiJob:
    """The job named by ``options``: ``job`` (JSON text) or ``job_path`` (a file), not both.

    Other options are ignored (no option widens what the job holds).
    """

    inline, path = options.get("job"), options.get("job_path")
    if (inline is None) == (path is None):
        raise WikigenError("job_option")
    if inline is not None:
        if not isinstance(inline, str):
            raise WikigenError("job_option")
        data: str | bytes = inline
    else:
        if not isinstance(path, str) or not path:
            raise WikigenError("job_option")
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_JOB_BYTES + 1)
        except OSError:
            raise WikigenError("job_unreadable") from None
    try:
        return parse_wiki_job(data)
    except WikiFormatError as exc:
        raise WikigenError(f"bad_job:{exc.code}") from None


def view_content(result: Generation) -> str:
    """The canonical JSON a view carries: the page and the run's report."""

    payload = {"format": VIEW_FORMAT, "page": result.page.to_json(), "report": result.report}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def candidates(view: DerivedView) -> list[IngestItem]:
    """The one staging candidate for a view this adapter built.

    Stage it with ``memfw.ports.staging.stage_candidates(..., proposal_id=<job
    digest>, source="wikigen")``: the same job gives the same proposal, and a
    page is reviewed (per section) before anything is approved. The metadata
    holds ids, counts and the build identity, never page text.
    """

    if view.deriver != NAME:
        raise ValueError("not a wikigen view")
    payload = json.loads(view.content)
    if not isinstance(payload, dict) or payload.get("format") != VIEW_FORMAT:
        raise ValueError("not a wikigen view")
    page = parse_wiki_page(payload["page"])
    report = payload.get("report") or {}
    assembly = report.get("assembly") or {}
    build = page.build
    metadata: dict[str, str | int | float | bool | None] = {
        "deriver": NAME,
        "kind": view.kind,
        "format": "memfw-wiki-page/1",
        "page_key": page.page_key,
        "concept": page.concept,
        "tenant": page.tenant,
        "privacy": page.privacy,
        "job_digest": build.job_digest,
        "generator_version": build.generator_version,
        "model": build.model,
        "model_digest": build.model_digest,
        "prompt_version": build.prompt_version,
        "claims": len(page.claims),
        "sources": len(page.sources),
        "needs_attention": bool(assembly.get("needs_attention")),
        "epistemic": "inference",
        "causal": view.causal_cap,
    }
    return [
        IngestItem(
            title=f"wiki page {page.page_key}",
            body=page.canonical_bytes().decode("utf-8"),
            external_ref=f"{NAME}:{page.page_key}:{build.job_digest}",
            metadata=metadata,
        )
    ]


def factory(settings: Mapping[str, Any] | None = None) -> WikigenDeriver:
    """The deriver for ``adapters: {enabled: [wikigen], wikigen: {...}}``.

    A bad setting or a refused backend still returns a deriver, one that
    reports ``available() == False`` with the code, so the registry shows why.
    """

    try:
        parsed = Settings.from_mapping(settings)
    except (TypeError, ValueError) as exc:
        code = str(exc) if str(exc) in {"bad_option", "key_in_config"} else "bad_option"
        return WikigenDeriver(Settings(), refused=code)
    backend, refused = load_backend(parsed)
    precheck, status = load_precheck(parsed.precheck)
    return WikigenDeriver(
        parsed, backend=backend, refused=refused, precheck=precheck, precheck_status=status
    )


SPEC = AdapterSpec(
    name=NAME,
    kind="deriver",
    factory=factory,
    requires=(),
    version=__version__,
    license=LICENSE,
)

__all__ = [
    "DEFAULT_BACKEND",
    "GENERATOR_FIELDS",
    "KINDS",
    "NAME",
    "NLI_NOT_RUN",
    "SPEC",
    "TASK",
    "VIEW_FORMAT",
    "Generation",
    "RunBudget",
    "Settings",
    "WikigenDeriver",
    "WikigenError",
    "__version__",
    "candidates",
    "estimate_tokens",
    "factory",
    "load_backend",
    "load_precheck",
    "read_job",
    "view_content",
]
