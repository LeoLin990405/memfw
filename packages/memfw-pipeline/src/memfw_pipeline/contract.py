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
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
#: An inputs digest: lowercase hex.
DIGEST_RE = re.compile(r"^[0-9a-f]{16,128}$")
#: A stage reports at most this many count keys (per build or post-publish step).
MAX_COUNT_KEYS = 32
#: The count the shrink guard compares: every BuildResult carries it.
ITEMS = "items"


def code(value: object, default: str = "other") -> str:
    """``value`` when it is a valid reason code, else ``default``."""
    return value if isinstance(value, str) and CODE_RE.fullmatch(value) else default


class StageError(Exception):
    """A stage failure with a fixed reason code.

    Only ``code`` reaches the status file (an invalid code becomes ``other``); the
    exception message is never written or printed, so it may name things. The
    exit code follows the call that raised it (``inputs_digest`` 1, ``build`` /
    ``verify`` / ``publish`` 3), except that a ``SetupError`` from the factory or
    ``inputs_digest`` exits 2.
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
    (lock and status; a stage does not write there), ``options`` the stage's
    options from the config file, read-only.
    """

    stage: str
    target: Path
    state_dir: Path
    options: Mapping[str, Any]
    clock: Clock

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

    def __post_init__(self) -> None:
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

    def __post_init__(self) -> None:
        if not CODE_RE.fullmatch(self.name or ""):
            raise ValueError(f"PostPublishStep.name must match {CODE_RE.pattern}")
        if not isinstance(self.attempts, int) or not 1 <= self.attempts <= 5:
            raise ValueError("PostPublishStep.attempts must be 1..5")


@dataclass(frozen=True)
class Freshness:
    """The source revision a build reads and when that revision was made.

    ``revision`` is an id (``REVISION_RE``), never a name; ``as_of`` is
    timezone-aware or ``None`` when the source cannot say.
    """

    revision: str
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not REVISION_RE.fullmatch(self.revision):
            raise ValueError(f"Freshness.revision must match {REVISION_RE.pattern}")
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
    status file is missing or corrupt, and a check that the live artifact still
    holds what the status says). :class:`DirectoryStage` supplies both.
    """

    name: str

    def inputs_digest(self, ctx: StageContext) -> str: ...

    def build(self, ctx: StageContext, workdir: Path) -> BuildResult: ...

    def verify(self, ctx: StageContext, workdir: Path, result: BuildResult) -> None: ...

    def publish(self, ctx: StageContext, workdir: Path) -> str: ...

    def freshness(self, ctx: StageContext) -> Freshness: ...


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
            return sum(1 for p in root.glob(self.count_glob) if p.is_file() and not p.is_symlink())
        except OSError:
            return 0


__all__ = [
    "CODE_RE",
    "DIGEST_RE",
    "ITEMS",
    "MAX_COUNT_KEYS",
    "REVISION_RE",
    "BuildResult",
    "DirectoryStage",
    "Freshness",
    "InputUnavailable",
    "PostPublishStep",
    "SetupError",
    "Stage",
    "StageContext",
    "StageError",
    "StepResult",
    "VerifyFailed",
    "code",
    "digest_of",
]
