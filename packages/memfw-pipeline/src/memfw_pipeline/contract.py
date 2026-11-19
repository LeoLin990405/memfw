"""The Stage contract: what one pipeline step implements.

A stage turns some inputs (an authority vault, a meta-index, a previous stage's
artifact) into one derived artifact directory. The runner
(:mod:`memfw_pipeline.runner`) calls it in a fixed order::

    freshness(ctx)                      # which source revision, how old
    inputs_digest(ctx)                  # unchanged digest -> skip to post_publish
    build(ctx, workdir) -> BuildResult  # into a fresh directory beside the target
    verify(ctx, workdir, result)        # raise VerifyFailed to keep the live artifact
    publish(ctx, workdir) -> method     # atomic; DirectoryStage swaps real directories
    post_publish(ctx) -> steps          # idempotent, also when inputs are unchanged

Everything a stage hands back to the runner is either a count, a fixed reason
code (``CODE_RE``), a digest or a revision id. The runner writes nothing else
into its status file, so item names and text never leave the artifact. A stage
must therefore never build a count key or a reason code from its data: they are
a fixed vocabulary of the stage, like an enum.

Standard library only (plus the memfw ports' ``Clock`` and name rule).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from . import fsops
from ._ports import Clock, check_name

#: Count keys, reason codes and step names: a fixed, lowercase vocabulary.
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
#: A source revision: a commit id, a generation number, a digest prefix. Never a name.
#: Revisions are git-like: a hash, a tag, a generation number, ``sha256:<hex>``.
#: On top of the shape, :func:`revision_ok` refuses anything that could be an address:
#: a bare dotted quad (``1.2.3.4``; write a four-part version as ``v1.2.3.4``) and a
#: dotted host name of two or more labels (``files.example``: a dot between two
#: labels that start with a letter). ``v1.2.3``, ``release-2026.11`` and
#: ``sha256:0f1e`` pass.
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
_IPV4_RE = re.compile(r"(?<![0-9A-Za-z])\d{1,3}(?:\.\d{1,3}){3}(?![0-9])")
_HOSTNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*\.[A-Za-z]")
#: An inputs digest: lowercase hex.
DIGEST_RE = re.compile(r"^[0-9a-f]{16,128}$")
#: A stage reports at most this many count keys (per build or post-publish step).
MAX_COUNT_KEYS = 32
#: The count the shrink guard compares: every BuildResult carries it.
ITEMS = "items"
#: The record the runner writes into every artifact it publishes (reserved name).
ARTIFACT_RECORD = ".memfw-pipeline.json"


def revision_ok(value: object) -> bool:
    """``value`` is revision-shaped and holds no IPv4 address or dotted host name."""
    return (
        isinstance(value, str)
        and REVISION_RE.fullmatch(value) is not None
        and _IPV4_RE.search(value) is None
        and _HOSTNAME_RE.search(value) is None
    )


#: Formats a stage may declare for its extra status fields (``Stage.status_formats``).
#: Free text is never one of them.
EXTRA_FORMATS: Mapping[str, re.Pattern[str]] = MappingProxyType(
    {
        "code": re.compile(r"^[a-z][a-z0-9_]{0,63}$"),
        "sha256": re.compile(r"^[0-9a-f]{64}$"),
        "uuid": re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
        # ``name``, ``org/name``, either with ``:tag``; lowercase, at most one ``/``.
        "model": re.compile(
            r"^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?(:[a-z0-9._-]{1,32})?$"
        ),
        "int": re.compile(r"^(0|[1-9][0-9]{0,17})$"),
    }
)
MAX_EXTRA_KEYS = 16


def extra_value_ok(fmt: object, value: object) -> bool:
    """``value`` matches the declared format ``fmt`` (a name in ``EXTRA_FORMATS``).

    ``model`` also refuses a dotted host name or file name (``files.example``,
    ``notes.md``): a dot between two labels that start with a letter.
    """
    pattern = EXTRA_FORMATS.get(fmt) if isinstance(fmt, str) else None
    if pattern is None:
        return False
    if fmt == "int" and isinstance(value, int) and not isinstance(value, bool):
        return 0 <= value < 10**18
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        return False
    return not (fmt == "model" and _HOSTNAME_RE.search(value))


def code(value: object, default: str = "other") -> str:
    """``value`` when it is a valid reason code, else ``default``."""
    return value if isinstance(value, str) and CODE_RE.fullmatch(value) else default


class StageError(Exception):
    """A stage failure with a fixed reason code.

    Only ``code`` reaches the status file (an invalid code becomes ``other``); the
    exception message is never written or printed, so it may name things. The
    exit code follows the call that raised it (``inputs_digest`` / ``prepare`` 1,
    ``build`` / ``verify`` / ``publish`` 3), except that a ``SetupError`` from the
    factory, ``inputs_digest`` or ``prepare`` exits 2.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.code = code(reason)


class InputUnavailable(StageError):
    """The inputs cannot be read; the live artifact is kept."""


class VerifyFailed(StageError):
    """The new artifact failed verification; the live artifact is kept."""


class SetupError(StageError):
    """Configuration or environment problem."""


def _counts(value: Mapping[str, int], *, what: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{what} must be a mapping")
    if len(value) > MAX_COUNT_KEYS:
        raise ValueError(f"{what} holds more than {MAX_COUNT_KEYS} keys")
    out: dict[str, int] = {}
    for key, number in value.items():
        if not isinstance(key, str) or not CODE_RE.fullmatch(key):
            raise ValueError(f"{what} keys must match {CODE_RE.pattern}")
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ValueError(f"{what} values must be non-negative ints")
        out[key] = number
    return MappingProxyType(dict(sorted(out.items())))


@dataclass(frozen=True)
class StageContext:
    """What the runner hands every stage call.

    ``stage`` is the configured name (the config key), ``target`` the live
    artifact directory, ``state_dir`` the runner's own directory for this stage
    (its status; a stage does not write there; the lock is ``.<target>.lock``
    beside the target), ``options`` the stage's
    options from the config file, read-only.
    """

    stage: str
    target: Path
    state_dir: Path
    options: Mapping[str, Any]
    clock: Clock
    #: What ``Stage.prepare`` returned this run (``None`` without ``prepare``): the
    #: channel from the digest step to ``build``, so inputs are read once.
    prepared: Any = None

    def __post_init__(self) -> None:
        check_name(self.stage, what="stage name")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True)
class BuildResult:
    """Counts and the artifacts a build produced.

    ``counts`` must carry ``items`` (what the shrink guard compares); other keys
    are fixed codes such as ``withheld_meta_missing``. ``artifacts`` are paths
    relative to the work directory that must exist after the build (``verify``
    of :class:`DirectoryStage` checks them); only their number is recorded.
    """

    counts: Mapping[str, int]
    artifacts: tuple[str, ...] = ()
    #: Extra status fields (a model name, a vault id): only keys the stage declares
    #: in ``status_formats``, each value matching its declared format, are kept.
    extra: Mapping[str, str | int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.extra, Mapping) or len(self.extra) > MAX_EXTRA_KEYS:
            raise ValueError(f"BuildResult.extra must be a mapping of <= {MAX_EXTRA_KEYS} keys")
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))
        counts = _counts(self.counts, what="BuildResult.counts")
        if ITEMS not in counts:
            raise ValueError("BuildResult.counts must carry 'items'")
        object.__setattr__(self, "counts", counts)
        artifacts = tuple(self.artifacts)
        for rel in artifacts:
            parts = Path(rel).parts if isinstance(rel, str) else ()
            if not parts or Path(rel).is_absolute() or ".." in parts:
                raise ValueError("BuildResult.artifacts must be relative paths inside workdir")
        object.__setattr__(self, "artifacts", artifacts)

    @property
    def items(self) -> int:
        return self.counts[ITEMS]


@dataclass(frozen=True)
class StepResult:
    """What one post-publish step reports: counts only."""

    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", _counts(self.counts, what="StepResult.counts"))


@dataclass(frozen=True)
class PostPublishStep:
    """An idempotent step after publishing (an adapter sync, an index refresh).

    It runs after every successful build AND when the inputs are unchanged, so a
    wiped downstream index heals on the next run. ``run`` raises
    :class:`StageError` (or anything else) to fail; ``attempts`` > 1 retries.
    """

    name: str
    run: Callable[[StageContext], StepResult]
    attempts: int = 1
    #: The step's own deadline. The runner cannot stop in-process code, so a step
    #: enforces it (``subprocess.run(timeout=...)``, an HTTP timeout); the runner
    #: records ``duration_ms`` and flags ``over_budget`` when it was exceeded.
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not CODE_RE.fullmatch(self.name or ""):
            raise ValueError(f"PostPublishStep.name must match {CODE_RE.pattern}")
        if not isinstance(self.attempts, int) or not 1 <= self.attempts <= 5:
            raise ValueError("PostPublishStep.attempts must be 1..5")
        if self.timeout_seconds is not None and not 0 < self.timeout_seconds <= 86400:
            raise ValueError("PostPublishStep.timeout_seconds must be in (0, 86400]")


@dataclass(frozen=True)
class Freshness:
    """The source revision a build reads and when that revision was made.

    ``revision`` is an id (``REVISION_RE``), never a name; ``as_of`` is
    timezone-aware or ``None`` when the source cannot say.
    """

    revision: str
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if not revision_ok(self.revision):
            raise ValueError("Freshness.revision must be an id (REVISION_RE, no host or IP)")
        if self.as_of is not None and (
            not isinstance(self.as_of, datetime) or self.as_of.utcoffset() is None
        ):
            raise ValueError("Freshness.as_of must be timezone-aware")


@runtime_checkable
class Stage(Protocol):
    """One pipeline step. See the module docstring for the call order.

    Required: ``name`` (the stage kind, a name like ``admitted-view``),
    ``inputs_digest``, ``build``, ``verify``, ``publish``, ``freshness``.
    Optional (the runner looks them up): ``post_publish(ctx) -> Sequence[
    PostPublishStep]`` and ``live_count(ctx) -> int | None`` (items in the live
    artifact, ``None`` when there is none; the shrink guard's fallback when the
    artifact has no record and the status is missing, and a check that the live
    artifact still holds what its record says). :class:`DirectoryStage` supplies
    both.

    Also optional: ``prepare(ctx) -> Prepared`` replaces ``inputs_digest`` and
    hands its ``data`` to ``build`` as ``ctx.prepared`` (inputs read once), and
    ``status_formats: Mapping[str, str]`` declares extra status keys with a format
    name from ``EXTRA_FORMATS`` (``BuildResult.extra``).
    """

    name: str

    def inputs_digest(self, ctx: StageContext) -> str: ...

    def build(self, ctx: StageContext, workdir: Path) -> BuildResult: ...

    def verify(self, ctx: StageContext, workdir: Path, result: BuildResult) -> None: ...

    def publish(self, ctx: StageContext, workdir: Path) -> str: ...

    def freshness(self, ctx: StageContext) -> Freshness: ...


@dataclass(frozen=True)
class Prepared:
    """The result of ``Stage.prepare``: the inputs digest and whatever the stage
    read to compute it (handed to ``build`` as ``ctx.prepared``; never recorded)."""

    digest: str
    data: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or not DIGEST_RE.fullmatch(self.digest):
            raise ValueError("Prepared.digest must be lowercase hex (DIGEST_RE)")


def digest_of(*parts: bytes | str) -> str:
    """sha256 over length-prefixed parts (unambiguous concatenation)."""
    hasher = hashlib.sha256()
    for part in parts:
        data = part.encode("utf-8") if isinstance(part, str) else bytes(part)
        hasher.update(f"{len(data)}:".encode())
        hasher.update(data)
    return hasher.hexdigest()


class DirectoryStage:
    """A base for stages whose artifact is one directory.

    Subclasses implement ``inputs_digest``, ``build`` and ``freshness``.
    ``publish`` exchanges the work directory with the live one atomically;
    ``live_count`` counts entries matching ``count_glob`` (relative to the
    artifact; ``None`` counts nothing); ``verify`` checks that every declared
    artifact exists and is not a symlink and, with ``count_glob``, that the work
    directory holds exactly ``items`` matching entries.
    """

    name = "directory"
    count_glob: str | None = None

    def inputs_digest(self, ctx: StageContext) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def build(self, ctx: StageContext, workdir: Path) -> BuildResult:  # pragma: no cover
        raise NotImplementedError

    def freshness(self, ctx: StageContext) -> Freshness:  # pragma: no cover - abstract
        raise NotImplementedError

    def verify(self, ctx: StageContext, workdir: Path, result: BuildResult) -> None:
        for rel in result.artifacts:
            path = workdir / rel
            if path.is_symlink() or not path.exists():
                raise VerifyFailed("artifact_missing")
        if self.count_glob is not None and self._count(workdir) != result.items:
            raise VerifyFailed("count_mismatch")

    def publish(self, ctx: StageContext, workdir: Path) -> str:
        return fsops.swap_in(workdir, ctx.target)

    def post_publish(self, ctx: StageContext) -> Sequence[PostPublishStep]:
        return ()

    def live_count(self, ctx: StageContext) -> int | None:
        target = ctx.target
        if target.is_symlink() or not target.is_dir():
            return None
        if self.count_glob is None:
            return None
        return self._count(target)

    def _count(self, root: Path) -> int:
        assert self.count_glob is not None
        try:
            return sum(
                1
                for p in root.glob(self.count_glob)
                if p.is_file() and not p.is_symlink() and p.name != ARTIFACT_RECORD
            )
        except OSError:
            return 0


__all__ = [
    "ARTIFACT_RECORD",
    "CODE_RE",
    "DIGEST_RE",
    "ITEMS",
    "MAX_COUNT_KEYS",
    "REVISION_RE",
    "EXTRA_FORMATS",
    "BuildResult",
    "DirectoryStage",
    "Freshness",
    "InputUnavailable",
    "PostPublishStep",
    "Prepared",
    "SetupError",
    "Stage",
    "StageContext",
    "StageError",
    "StepResult",
    "VerifyFailed",
    "code",
    "digest_of",
    "extra_value_ok",
    "revision_ok",
]
