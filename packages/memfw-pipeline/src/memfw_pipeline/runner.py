"""The runner: one stage run under the contract, plus ``plan`` and ``status``.

Exit codes of :func:`run_stage`:

==  =====================================================================
0   built, or inputs unchanged (post-publish steps ran)
1   inputs unavailable; the live artifact is kept
2   setup error (config, untrusted path, stage factory, a crash leftover the
    runner will not resolve on its own); nothing is published
3   build, verify or publish failed; the live artifact is kept
4   published (or unchanged) but a post-publish step failed
5   shrink refused: an empty build, or under half of the baseline (the live
    artifact's own record, else the last status, else its live count)
75  another run holds the lock: nothing was done; a unit counts it as success
==  =====================================================================

The live artifact carries its own record, ``.memfw-pipeline.json`` (the inputs
digest it was built from, its counts and extra fields), written into the work
directory before the swap. Whether a rebuild is needed is decided from that
record, never from the status file alone: a run killed after the swap but
before its final status write cannot make a later run skip a rebuild of an
artifact that no longer matches its inputs.

The status file (``<state_dir>/<stage>/status.json``) holds counts, fixed
reason codes, digests, revisions, timestamps and booleans only. Every string
is checked against those shapes before it is written (:func:`clean_status`);
anything else becomes ``redacted``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import fsops
from ._ports import Clock, SystemClock
from .config import StageSpec, load_stage, trust_failures
from .contract import (
    ARTIFACT_RECORD,
    CODE_RE,
    DIGEST_RE,
    EXTRA_FORMATS,
    ITEMS,
    MAX_EXTRA_KEYS,
    BuildResult,
    Freshness,
    InputUnavailable,
    PostPublishStep,
    Prepared,
    SetupError,
    Stage,
    StageContext,
    StageError,
    StepResult,
    VerifyFailed,
    code,
    revision_ok,
)

RUNNER_VERSION = "2"
STATUS_SCHEMA = 1
#: A build under this share of the baseline is refused (exactly half passes).
SHRINK_FLOOR = 0.5
ARTIFACT_SCHEMA = 1

EXIT_OK = 0
EXIT_INPUTS = 1
EXIT_SETUP = 2
EXIT_BUILD = 3
EXIT_POST_PUBLISH = 4
EXIT_SHRINK = 5
EXIT_BUSY = 75

_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TYPE_RE = re.compile(r"^[A-Z][A-Za-z0-9_]{0,63}$")
_SWAP_METHODS = frozenset({"rename", "exchange", "rename_pair", "custom"})


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


# --- status hygiene ------------------------------------------------------------------------
#: ``stage`` / ``kind``: the name rule of the ports (``check_name``).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
#: ``errors`` / ``warnings`` entries: ``code`` or ``prefix:code``.
_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}(?::[a-z][a-z0-9_]{0,63})?$")


def _matches(pattern: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda value: pattern.fullmatch(value) is not None


_KEY_RULES: dict[str, Callable[[str], bool]] = {
    "stage": _matches(_NAME_RE),
    "kind": _matches(_NAME_RE),
    "inputs_digest": _matches(DIGEST_RE),
    "published_digest": _matches(DIGEST_RE),
    "publishing_digest": _matches(DIGEST_RE),
    "source_revision": revision_ok,
    "published_revision": revision_ok,
    "exception": _matches(_TYPE_RE),
    "errors": _matches(_ERROR_RE),
    "warnings": _matches(_ERROR_RE),
    "runner_version": _matches(re.compile(r"^[0-9]{1,8}$")),
}
_CODE_RULE = _matches(CODE_RE)
_TIME_RULE = _matches(_TIME_RE)


def _extra_rule(value: str) -> bool:
    return any(pattern.fullmatch(value) for pattern in EXTRA_FORMATS.values())


def _clean_value(value: Any, key: str, extra: bool = False) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        rule: Callable[[str], bool]
        if extra:
            rule = _extra_rule
        elif key.endswith("_at") or key == "source_as_of":
            rule = _TIME_RULE
        else:
            rule = _KEY_RULES.get(key, _CODE_RULE)
        return value if rule(value) else "redacted"
    if isinstance(value, Mapping):
        return {
            code(k, "redacted") if isinstance(k, str) else "redacted": _clean_value(
                v, str(k), extra or key == "extra"
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_clean_value(v, key, extra) for v in value]
    return "redacted"


def clean_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``payload`` with every key a code and every string of the shape its key allows.

    Timestamps (``*_at``), digests, revisions (no IPv4 or dotted host names),
    the stage and kind names, error codes (``prefix:code``), an exception's type
    name and the declared formats of ``extra`` have their own shapes; every
    other string must be a reason code. The runner only puts such values there
    itself; this is the second fence, so a stage bug cannot write a file name,
    a path or text into the status file: it becomes ``redacted``.

    Limit: a lowercase slug (``alice_smith``) IS a valid reason code, so a stage
    that builds a code from its data would get it through. The first fence is
    the contract: codes and count keys are a fixed vocabulary of the stage.
    """
    cleaned = _clean_value(payload, "")
    assert isinstance(cleaned, dict)
    return cleaned


def read_status(spec: StageSpec) -> dict[str, Any] | None:
    """The last status, or ``None`` when missing or corrupt."""
    data = fsops.read_json(spec.status_path)
    if data is None or data.get("schema") != STATUS_SCHEMA:
        return None
    return data


def _write_status(spec: StageSpec, payload: Mapping[str, Any]) -> None:
    fsops.write_json_atomic(spec.status_path, clean_status(payload), mode=spec.status_mode)


# --- the artifact's own record -------------------------------------------------------------
@dataclass(frozen=True)
class ArtifactRecord:
    digest: str
    counts: Mapping[str, int]
    extra: Mapping[str, str | int]

    @property
    def items(self) -> int:
        return self.counts[ITEMS]


def read_record(target: Path) -> ArtifactRecord | None:
    """The record of the live artifact, or ``None`` (none, symlinked, corrupt)."""
    if target.is_symlink() or not target.is_dir():
        return None
    data = fsops.read_json(target / ARTIFACT_RECORD)
    if data is None or data.get("schema") != ARTIFACT_SCHEMA:
        return None
    digest, counts, extra = data.get("inputs_digest"), data.get("counts"), data.get("extra", {})
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        return None
    if not isinstance(counts, dict) or _int(counts.get(ITEMS)) is None:
        return None
    if not all(
        isinstance(k, str) and CODE_RE.fullmatch(k) and _int(v) is not None
        for k, v in counts.items()
    ):
        return None
    if not isinstance(extra, dict):
        return None
    return ArtifactRecord(digest, dict(counts), dict(extra))


def _write_record(
    workdir: Path, digest: str, result: BuildResult, extra: Mapping[str, Any], now: datetime
) -> None:
    path = workdir / ARTIFACT_RECORD
    if path.exists() or path.is_symlink():
        raise VerifyFailed("reserved_name")
    payload = {
        "schema": ARTIFACT_SCHEMA,
        "inputs_digest": digest,
        "counts": dict(result.counts),
        "extra": dict(extra),
        "built_at": _iso(now),
    }
    fsops.write_json_atomic(path, clean_status(payload), mode=0o600)


def _accepted_extra(stage: Stage, result: BuildResult) -> tuple[dict[str, Any], int]:
    """``result.extra`` filtered by the stage's ``status_formats``; (kept, rejected)."""
    formats = getattr(stage, "status_formats", None) or {}
    kept: dict[str, Any] = {}
    rejected = 0
    for key, value in list(result.extra.items())[:MAX_EXTRA_KEYS]:
        fmt = formats.get(key) if isinstance(formats, Mapping) else None
        pattern = EXTRA_FORMATS.get(fmt) if isinstance(fmt, str) else None
        ok = (
            isinstance(key, str)
            and CODE_RE.fullmatch(key) is not None
            and pattern is not None
            and (
                (isinstance(value, str) and pattern.fullmatch(value) is not None)
                or (fmt == "int" and _int(value) is not None)
            )
        )
        if ok:
            kept[key] = value
        else:
            rejected += 1
    return kept, rejected


# --- freshness -----------------------------------------------------------------------------
def freshness_fields(
    *,
    current: Freshness | None,
    published_revision: str | None,
    last_success_at: datetime | None,
    max_lag_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    """Freshness of the live artifact.

    ``lag_seconds`` is 0 when the live artifact was built from the current
    source revision, else the time since the current revision was made (a lower
    bound of how far behind it is; ``None`` when the source gives no time).
    ``stale`` is true when nothing was ever published, the lag exceeds
    ``max_lag_seconds``, or the last successful run is older than that (the
    timer stopped).
    """
    fields: dict[str, Any] = {"max_lag_seconds": max_lag_seconds}
    lag: int | None = None
    if current is not None:
        fields["source_revision"] = current.revision
        if current.as_of is not None:
            fields["source_as_of"] = _iso(current.as_of)
        behind = published_revision is not None and published_revision != current.revision
        fields["behind"] = behind
        if not behind and published_revision is not None:
            lag = 0
        elif current.as_of is not None:
            lag = max(0, int((now - current.as_of).total_seconds()))
    else:
        fields["source_revision"] = None
    if published_revision is not None:
        fields["published_revision"] = published_revision
    fields["lag_seconds"] = lag
    age: int | None = None
    if last_success_at is not None:
        age = max(0, int((now - last_success_at).total_seconds()))
    fields["last_success_age_seconds"] = age
    fields["stale"] = (
        published_revision is None
        or age is None
        or age > max_lag_seconds
        or (lag is not None and lag > max_lag_seconds)
    )
    return fields


# --- the run -------------------------------------------------------------------------------
@dataclass(frozen=True)
class RunOptions:
    force: bool = False
    allow_shrink: bool = False
    #: How long to retry a held lock before exiting 75 (a probe holds it for an instant).
    lock_wait_seconds: float = 5.0


def _context(spec: StageSpec, clock: Clock) -> StageContext:
    return StageContext(
        stage=spec.name,
        target=spec.target,
        state_dir=spec.state_dir,
        options=spec.options,
        clock=clock,
    )


def _error_fields(exc: BaseException, prefix: str) -> dict[str, Any]:
    if isinstance(exc, StageError):
        return {"errors": [f"{prefix}:{exc.code}"]}
    name = type(exc).__name__
    return {"errors": [prefix], "exception": name if _TYPE_RE.fullmatch(name) else "Exception"}


def _setup_failure(spec: StageSpec, errors: list[str]) -> tuple[int, dict[str, Any]]:
    return EXIT_SETUP, clean_status(
        {
            "schema": STATUS_SCHEMA,
            "stage": spec.name,
            "ok": False,
            "result": "setup_error",
            "errors": errors,
        }
    )


def _run_steps(stage: Stage, ctx: StageContext) -> tuple[bool, dict[str, Any], list[str]]:
    get_steps: Callable[[StageContext], Sequence[PostPublishStep]] | None = getattr(
        stage, "post_publish", None
    )
    report: dict[str, Any] = {}
    errors: list[str] = []
    if get_steps is None:
        return True, report, errors
    try:
        steps = list(get_steps(ctx))
    except Exception as exc:  # noqa: BLE001 - report the code only
        return False, report, [f"post_publish:{code(getattr(exc, 'code', None), 'failed')}"]
    for step in steps:
        if not isinstance(step, PostPublishStep):
            errors.append("post_publish:not_a_step")
            continue
        entry: dict[str, Any] = {"ok": False}
        started = time.monotonic()
        for attempt in range(1, step.attempts + 1):
            entry["attempts"] = attempt
            try:
                result = step.run(ctx)
                if not isinstance(result, StepResult):
                    raise StageError("bad_result")
            except Exception as exc:  # noqa: BLE001
                entry["error"] = exc.code if isinstance(exc, StageError) else "failed"
                if not isinstance(exc, StageError):
                    name = type(exc).__name__
                    entry["exception"] = name if _TYPE_RE.fullmatch(name) else "Exception"
                continue
            entry = {"ok": True, "attempts": attempt, "counts": dict(result.counts)}
            break
        elapsed = time.monotonic() - started
        entry["duration_ms"] = int(elapsed * 1000)
        if step.timeout_seconds is not None and elapsed > step.timeout_seconds * step.attempts:
            entry["over_budget"] = True
        report[step.name] = entry
        if not entry["ok"]:
            errors.append(f"{step.name}:{entry.get('error', 'failed')}")
    return not errors, report, errors


def run_stage(
    spec: StageSpec,
    options: RunOptions | None = None,
    *,
    clock: Clock | None = None,
    stage: Stage | None = None,
) -> tuple[int, dict[str, Any]]:
    """One run of ``spec``; returns ``(exit code, status)``. See the module docstring.

    ``stage`` skips the factory (tests); ``clock`` defaults to the system clock.
    """
    opts = options or RunOptions()
    clock = clock or SystemClock()
    trust = spec.trust_enabled
    if trust:
        failures = trust_failures(spec)
        if failures:
            # nothing is written into a tree another user could have prepared
            return _setup_failure(spec, failures)
    try:
        fsops.private_mkdir(spec.state_dir, spec.state_mode)
        if not spec.target.parent.exists():
            fsops.private_mkdir(spec.target.parent)
    except OSError:
        return _setup_failure(spec, ["state_dir_unusable"])
    if trust:
        failures = trust_failures(spec)  # again: the directories exist now
        if failures:
            return _setup_failure(spec, failures)
    with contextlib.ExitStack() as stack:
        try:
            held = stack.enter_context(
                fsops.single_writer(spec.lock_path, wait_seconds=opts.lock_wait_seconds)
            )
        except OSError:
            return _setup_failure(spec, ["lock_unusable"])
        if not held:
            # not a failure: the other run does the work (SuccessExitStatus=75)
            return EXIT_BUSY, {
                "schema": STATUS_SCHEMA,
                "stage": spec.name,
                "ok": True,
                "result": "busy",
            }
        try:
            return _run_locked(spec, opts, clock, stage)
        except OSError:
            # the status file itself cannot be written (a directory or a full disk there)
            return _setup_failure(spec, ["status_unwritable"])


def _run_locked(
    spec: StageSpec, opts: RunOptions, clock: Clock, stage: Stage | None
) -> tuple[int, dict[str, Any]]:
    started = time.monotonic()
    now = clock.now()
    previous = read_status(spec)
    status: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "runner_version": RUNNER_VERSION,
        "stage": spec.name,
        "started_at": _iso(now),
        "ok": False,
    }
    if previous is None and spec.status_path.exists():
        status["warnings"] = ["status_unreadable"]
    prev = previous or {}
    # What the live artifact is, carried through every run (a killed run included).
    carried: dict[str, Any] = {}
    for key in (
        "last_items",
        "last_success_at",
        "last_built_at",
        "published_digest",
        "published_revision",
        "swap_method",
    ):
        if prev.get(key) is not None:
            carried[key] = prev[key]
    # Visible at once: a run killed by the service manager leaves "running".
    _write_status(spec, {**status, **carried, "result": "running"})
    ctx = _context(spec, clock)
    current: Freshness | None = None

    def finish(exit_code: int, result: str, **fields: Any) -> tuple[int, dict[str, Any]]:
        end = clock.now()
        status.update(fields)
        for key, value in carried.items():
            status.setdefault(key, value)
        status["result"] = result
        status["exit_code"] = exit_code
        status["ok"] = exit_code == EXIT_OK
        status["finished_at"] = _iso(end)
        status["duration_ms"] = int((time.monotonic() - started) * 1000)
        status["artifact_present"] = spec.target.is_dir() and not spec.target.is_symlink()
        status["freshness"] = freshness_fields(
            current=current,
            published_revision=status.get("published_revision"),
            last_success_at=_parse_iso(status.get("last_success_at")),
            max_lag_seconds=spec.max_lag_seconds,
            now=end,
        )
        cleaned = clean_status(status)
        _write_status(spec, cleaned)
        return exit_code, cleaned

    restored = fsops.restore_orphaned_old(spec.target)
    if restored in ("ambiguous", "failed"):
        # One of the .old-* copies may be the only good artifact: keep every one of them.
        return finish(EXIT_SETUP, "setup_error", errors=[f"restore_{restored}"])
    if restored == "restored":
        status.setdefault("warnings", []).append("previous_artifact_restored")
    leftovers = fsops.cleanup_leftovers(spec.target)
    if leftovers:
        status["leftovers_removed"] = leftovers

    try:
        if stage is None:
            stage = load_stage(spec)
    except SetupError as exc:
        return finish(EXIT_SETUP, "setup_error", errors=[exc.code])
    status["kind"] = stage.name
    current = _probe_freshness(stage, ctx)
    if current is None:  # informative only: the digest decides what to build
        status.setdefault("warnings", []).append("freshness_unavailable")
    try:
        digest, ctx = _digest(stage, ctx)
    except InputUnavailable as exc:
        return finish(EXIT_INPUTS, "inputs_unavailable", **_error_fields(exc, "inputs"))
    except SetupError as exc:
        return finish(EXIT_SETUP, "setup_error", errors=[exc.code])
    except Exception as exc:  # noqa: BLE001
        return finish(EXIT_INPUTS, "inputs_unavailable", **_error_fields(exc, "inputs"))
    status["inputs_digest"] = digest

    live_count = _live_count(stage, ctx)
    record = read_record(spec.target)
    unchanged = (
        not opts.force
        and record is not None
        # the artifact's own record decides, never the status file alone
        and record.digest == digest
        and (live_count is None or live_count == record.items)
    )
    if unchanged:
        assert record is not None
        status.update(
            counts=dict(record.counts),
            last_items=record.items,
            published_digest=record.digest,
        )
        if record.extra:
            status["extra"] = dict(record.extra)
        if current is not None:
            status["published_revision"] = current.revision
        return _post_publish(stage, ctx, status, finish, result="unchanged", now=clock.now())

    # Shrink baseline: what the live artifact says it holds, else the status, else a count.
    last_items = _int(prev.get("last_items"))
    if record is not None:
        baseline, source = record.items, "artifact"
    elif last_items is not None and spec.target.is_dir():
        baseline, source = last_items, "status"
    elif live_count is not None:
        baseline, source = live_count, "live"
    elif last_items is not None:
        baseline, source = last_items, "status"
    else:
        baseline, source = None, "none"

    workdir = fsops.new_name(spec.target, "new")
    try:
        os.mkdir(workdir, 0o700)
        result = stage.build(ctx, workdir)
        if not isinstance(result, BuildResult):
            raise StageError("bad_result")
        status["counts"] = dict(result.counts)
        status["artifacts"] = len(result.artifacts)
    except Exception as exc:  # noqa: BLE001 - keep the live artifact, codes only
        _remove(workdir)
        return finish(EXIT_BUILD, "build_failed", **_error_fields(exc, "build"))
    try:
        stage.verify(ctx, workdir, result)
    except Exception as exc:  # noqa: BLE001
        _remove(workdir)
        return finish(EXIT_BUILD, "verify_failed", **_error_fields(exc, "verify"))
    extra, rejected = _accepted_extra(stage, result)
    if rejected:
        status.setdefault("warnings", []).append("extra_rejected")
    if extra:
        status["extra"] = extra

    items = result.items
    shrink: dict[str, Any] = {
        "baseline": baseline,
        "baseline_source": source,
        "would_publish": items,
    }
    refused = None
    if not opts.allow_shrink:
        if items == 0 and (spec.empty_build == "refuse" or (baseline is not None and baseline > 0)):
            refused = "empty_build"
        elif baseline is not None and baseline > 0 and items < baseline * SHRINK_FLOOR:
            refused = "shrink_refused"
    if refused is not None:
        _remove(workdir)
        return finish(EXIT_SHRINK, "shrink_refused", errors=[refused], shrink=shrink)
    if opts.allow_shrink:
        shrink["allowed"] = True
    status["shrink"] = shrink

    try:
        _write_record(workdir, digest, result, extra, clock.now())
        fsops.fsync_tree(workdir)
    except VerifyFailed as exc:  # the stage wrote the reserved record name
        _remove(workdir)
        return finish(EXIT_BUILD, "verify_failed", **_error_fields(exc, "verify"))
    except Exception as exc:  # noqa: BLE001 - I/O before the swap
        _remove(workdir)
        return finish(EXIT_BUILD, "publish_failed", **_error_fields(exc, "record"))
    # Visible before the swap: a run killed from here on is known to have been publishing.
    _write_status(spec, {**status, **carried, "result": "publishing", "publishing_digest": digest})
    try:
        method = stage.publish(ctx, workdir)
        fsops.fsync_dir(spec.target.parent)
    except Exception as exc:  # noqa: BLE001
        _remove(workdir)
        return finish(EXIT_BUILD, "publish_failed", **_error_fields(exc, "publish"))
    _remove(workdir)  # gone after a rename; holds the old artifact only if publish left it
    status.update(
        last_items=items,
        last_built_at=_iso(clock.now()),
        published_digest=digest,
        swap_method=method if isinstance(method, str) and method in _SWAP_METHODS else "custom",
    )
    if current is not None:
        status["published_revision"] = current.revision
    else:
        status.pop("published_revision", None)
        carried.pop("published_revision", None)
    return _post_publish(stage, ctx, status, finish, result="built", now=clock.now())


def _digest(stage: Stage, ctx: StageContext) -> tuple[str, StageContext]:
    """The inputs digest, through ``prepare`` when the stage has it."""
    prepare = getattr(stage, "prepare", None)
    if prepare is not None:
        prepared = prepare(ctx)
        if not isinstance(prepared, Prepared):
            raise InputUnavailable("bad_prepared")
        return prepared.digest, dataclasses.replace(ctx, prepared=prepared.data)
    digest = stage.inputs_digest(ctx)
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise InputUnavailable("bad_digest")
    return digest, ctx


def _post_publish(
    stage: Stage,
    ctx: StageContext,
    status: dict[str, Any],
    finish: Callable[..., tuple[int, dict[str, Any]]],
    *,
    result: str,
    now: datetime,
) -> tuple[int, dict[str, Any]]:
    ok, report, errors = _run_steps(stage, ctx)
    if report:
        status["post_publish"] = report
    # The artifact is live and current either way; a failed step is reported apart.
    status["last_success_at"] = _iso(now)
    if not ok:
        return finish(EXIT_POST_PUBLISH, "post_publish_failed", errors=errors)
    return finish(EXIT_OK, result)


def _probe_freshness(stage: Stage, ctx: StageContext) -> Freshness | None:
    try:
        current = stage.freshness(ctx)
    except Exception:  # noqa: BLE001
        return None
    return current if isinstance(current, Freshness) else None


def _live_count(stage: Stage, ctx: StageContext) -> int | None:
    counter = getattr(stage, "live_count", None)
    if counter is None:
        return None
    try:
        return _int(counter(ctx))
    except Exception:  # noqa: BLE001
        return None


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


# --- plan and status -----------------------------------------------------------------------
def plan_stage(
    spec: StageSpec, *, clock: Clock | None = None, stage: Stage | None = None
) -> dict[str, Any]:
    """What ``run`` would do now. Reads only: nothing is written.

    It probes the lock with a non-blocking ``flock`` for an instant; ``run``
    retries a held lock for ``lock_wait_seconds``, so a probe never makes a
    scheduled run give up.
    """
    clock = clock or SystemClock()
    report: dict[str, Any] = {"schema": STATUS_SCHEMA, "stage": spec.name}
    if spec.trust_enabled:
        failures = trust_failures(spec)
        if failures:
            return clean_status({**report, "action": "setup_error", "errors": failures})
    try:
        stage = stage or load_stage(spec)
    except SetupError as exc:
        return clean_status({**report, "action": "setup_error", "errors": [exc.code]})
    report["kind"] = stage.name
    ctx = _context(spec, clock)
    previous = read_status(spec) or {}
    report["locked"] = fsops.lock_is_held(spec.lock_path)
    report["last_result"] = previous.get("result")
    report["last_items"] = _int(previous.get("last_items"))
    current = _probe_freshness(stage, ctx)
    try:
        digest, ctx = _digest(stage, ctx)
    except Exception as exc:  # noqa: BLE001
        report.update(action="inputs_unavailable", **_error_fields(exc, "inputs"))
        return clean_status(report)
    live = _live_count(stage, ctx)
    record = read_record(spec.target)
    report["inputs_digest"] = digest
    report["live_count"] = live
    report["artifact_items"] = record.items if record is not None else None
    same = record is not None and record.digest == digest
    report["action"] = (
        "skip_unchanged"
        if same and record is not None and (live is None or live == record.items)
        else "build"
    )
    try:
        steps = getattr(stage, "post_publish", None)
        report["post_publish_steps"] = len(list(steps(ctx))) if steps is not None else 0
    except Exception:  # noqa: BLE001
        report["post_publish_steps"] = None
    report["freshness"] = freshness_fields(
        current=current,
        published_revision=previous.get("published_revision"),
        last_success_at=_parse_iso(previous.get("last_success_at")),
        max_lag_seconds=spec.max_lag_seconds,
        now=clock.now(),
    )
    return clean_status(report)


def stage_status(
    spec: StageSpec,
    *,
    clock: Clock | None = None,
    probe: bool = True,
    stage: Stage | None = None,
) -> dict[str, Any]:
    """The last status plus freshness recomputed now (``probe`` reads the source)."""
    clock = clock or SystemClock()
    previous = read_status(spec)
    report: dict[str, Any] = dict(
        previous
        or {"schema": STATUS_SCHEMA, "stage": spec.name, "result": "never_run", "ok": False}
    )
    if previous is None and spec.status_path.exists():
        report["result"] = "status_unreadable"
    current: Freshness | None = None
    if probe and not (spec.trust_enabled and trust_failures(spec)):
        try:
            stage = stage or load_stage(spec)
        except SetupError:
            stage = None
        current = _probe_freshness(stage, _context(spec, clock)) if stage is not None else None
        if current is None:
            report.setdefault("warnings", []).append("freshness_unavailable")
    elif previous is not None and isinstance(previous.get("freshness"), dict):
        revision = previous["freshness"].get("source_revision")
        as_of = _parse_iso(previous["freshness"].get("source_as_of"))
        if revision_ok(revision):
            current = Freshness(revision, as_of)
    report["locked"] = fsops.lock_is_held(spec.lock_path)
    report["freshness"] = freshness_fields(
        current=current,
        published_revision=report.get("published_revision"),
        last_success_at=_parse_iso(report.get("last_success_at")),
        max_lag_seconds=spec.max_lag_seconds,
        now=clock.now(),
    )
    return clean_status(report)


__all__ = [
    "ARTIFACT_RECORD",
    "EXIT_BUILD",
    "EXIT_BUSY",
    "EXIT_INPUTS",
    "EXIT_OK",
    "EXIT_POST_PUBLISH",
    "EXIT_SETUP",
    "EXIT_SHRINK",
    "RUNNER_VERSION",
    "SHRINK_FLOOR",
    "ArtifactRecord",
    "RunOptions",
    "clean_status",
    "freshness_fields",
    "plan_stage",
    "read_record",
    "read_status",
    "run_stage",
    "stage_status",
]
