"""The runner: one stage run under the contract, plus ``plan`` and ``status``.

Exit codes of :func:`run_stage`:

==  =====================================================================
0   built, or inputs unchanged (post-publish steps ran)
1   inputs unavailable; the live artifact is kept
2   setup error (config, untrusted path, stage factory)
3   build, verify or publish failed; the live artifact is kept
4   published (or unchanged) but a post-publish step failed
5   shrink refused: an empty build, or under half of the baseline (the last
    published count from the status, else the live artifact's own count)
75  another run holds the lock: nothing was done; a unit counts it as success
==  =====================================================================

The status file (``<state_dir>/<stage>/status.json``) holds counts, fixed
reason codes, digests, revisions, timestamps and booleans only. Every string
is checked against those shapes before it is written (:func:`clean_status`);
anything else becomes ``redacted``.
"""

from __future__ import annotations

import contextlib
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
    CODE_RE,
    DIGEST_RE,
    REVISION_RE,
    BuildResult,
    Freshness,
    InputUnavailable,
    PostPublishStep,
    SetupError,
    Stage,
    StageContext,
    StageError,
    StepResult,
    code,
)

RUNNER_VERSION = "1"
STATUS_SCHEMA = 1
#: A build under this share of the live count is refused (exactly half passes).
SHRINK_FLOOR = 0.5

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
_KEY_RULES: dict[str, re.Pattern[str]] = {
    "stage": _NAME_RE,
    "kind": _NAME_RE,
    "inputs_digest": DIGEST_RE,
    "published_digest": DIGEST_RE,
    "source_revision": REVISION_RE,
    "published_revision": REVISION_RE,
    "exception": _TYPE_RE,
    "errors": _ERROR_RE,
    "warnings": _ERROR_RE,
    "runner_version": re.compile(r"^[0-9]{1,8}$"),
}


def _clean_value(value: Any, key: str) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        if key.endswith("_at") or key == "source_as_of":
            rule = _TIME_RE
        else:
            rule = _KEY_RULES.get(key, CODE_RE)
        return value if rule.fullmatch(value) else "redacted"
    if isinstance(value, Mapping):
        return {
            code(k, "redacted") if isinstance(k, str) else "redacted": _clean_value(v, str(k))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_clean_value(v, key) for v in value]
    return "redacted"


def clean_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``payload`` with every key a code and every string of the shape its key allows.

    Timestamps (``*_at``), digests, revisions, the stage and kind names, error
    codes (``prefix:code``) and an exception's type name have their own shapes;
    every other string must be a reason code. The runner only puts such values
    there itself; this is the second fence, so a stage bug can never write a
    file name or text into the status file: it becomes ``redacted``.
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
    fsops.write_json_atomic(spec.status_path, clean_status(payload))


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
    if spec.trust_paths:
        failures = trust_failures(spec)
        if failures:
            # nothing is written into a tree another user could have prepared
            return EXIT_SETUP, clean_status(
                {
                    "schema": STATUS_SCHEMA,
                    "stage": spec.name,
                    "ok": False,
                    "result": "setup_error",
                    "errors": failures,
                }
            )
    try:
        fsops.private_mkdir(spec.state_dir)
        if not spec.target.parent.exists():
            fsops.private_mkdir(spec.target.parent)
    except OSError:
        return EXIT_SETUP, clean_status(
            {
                "schema": STATUS_SCHEMA,
                "stage": spec.name,
                "ok": False,
                "result": "setup_error",
                "errors": ["state_dir_unusable"],
            }
        )
    with contextlib.ExitStack() as stack:
        try:
            held = stack.enter_context(fsops.single_writer(spec.lock_path))
        except OSError:
            return EXIT_SETUP, clean_status(
                {
                    "schema": STATUS_SCHEMA,
                    "stage": spec.name,
                    "ok": False,
                    "result": "setup_error",
                    "errors": ["lock_unusable"],
                }
            )
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
            return EXIT_SETUP, clean_status(
                {
                    "schema": STATUS_SCHEMA,
                    "stage": spec.name,
                    "ok": False,
                    "result": "setup_error",
                    "errors": ["status_unwritable"],
                }
            )


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
    if fsops.restore_orphaned_old(spec.target):
        status.setdefault("warnings", []).append("previous_artifact_restored")
    leftovers = fsops.cleanup_leftovers(spec.target)
    if leftovers:
        status["leftovers_removed"] = leftovers
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
        digest = stage.inputs_digest(ctx)
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise InputUnavailable("bad_digest")
    except InputUnavailable as exc:
        return finish(EXIT_INPUTS, "inputs_unavailable", **_error_fields(exc, "inputs"))
    except SetupError as exc:
        return finish(EXIT_SETUP, "setup_error", errors=[exc.code])
    except Exception as exc:  # noqa: BLE001
        return finish(EXIT_INPUTS, "inputs_unavailable", **_error_fields(exc, "inputs"))
    status["inputs_digest"] = digest

    live_count = _live_count(stage, ctx)
    last_items = _int(prev.get("last_items"))
    unchanged = (
        not opts.force
        and prev.get("published_digest") == digest
        and spec.target.is_dir()
        and not spec.target.is_symlink()
        # the status alone is not trusted: the live artifact must still hold what it said
        and (live_count is None or live_count == last_items)
    )
    if unchanged:
        if current is not None:
            status["published_revision"] = current.revision
        return _post_publish(stage, ctx, status, finish, result="unchanged", now=clock.now())

    # Shrink baseline: the status, else the live artifact itself.
    if last_items is not None:
        baseline, source = last_items, "status"
    elif live_count is not None:
        baseline, source = live_count, "live"
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

    items = result.items
    shrink = {"baseline": baseline, "baseline_source": source, "would_publish": items}
    refused = None
    if not opts.allow_shrink:
        if items == 0:
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
        fsops.fsync_tree(workdir)
        method = stage.publish(ctx, workdir)
        fsops.fsync_dir(spec.target.parent)
    except Exception as exc:  # noqa: BLE001
        _remove(workdir)
        return finish(EXIT_BUILD, "publish_failed", **_error_fields(exc, "publish"))
    _remove(workdir)  # gone after a rename; holds the old artifact only if publish left it
    built_at = _iso(clock.now())
    status.update(
        last_items=items,
        last_built_at=built_at,
        published_digest=digest,
        swap_method=method if isinstance(method, str) and method in _SWAP_METHODS else "custom",
    )
    if current is not None:
        status["published_revision"] = current.revision
    else:
        status.pop("published_revision", None)
        carried.pop("published_revision", None)
    return _post_publish(stage, ctx, status, finish, result="built", now=clock.now())


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
    """What ``run`` would do now. Reads only: no lock taken, nothing written."""
    clock = clock or SystemClock()
    report: dict[str, Any] = {"schema": STATUS_SCHEMA, "stage": spec.name}
    if spec.trust_paths:
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
    report["locked"] = fsops.lock_is_held(spec.lock_path) is True
    report["last_result"] = previous.get("result")
    report["last_items"] = _int(previous.get("last_items"))
    current = _probe_freshness(stage, ctx)
    try:
        digest = stage.inputs_digest(ctx)
        if not DIGEST_RE.fullmatch(digest):
            raise InputUnavailable("bad_digest")
    except Exception as exc:  # noqa: BLE001
        report.update(action="inputs_unavailable", **_error_fields(exc, "inputs"))
        return clean_status(report)
    live = _live_count(stage, ctx)
    report["inputs_digest"] = digest
    report["live_count"] = live
    same = (
        previous.get("published_digest") == digest
        and spec.target.is_dir()
        and not spec.target.is_symlink()
    )
    report["action"] = (
        "skip_unchanged" if same and (live is None or live == report["last_items"]) else "build"
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
    if probe and not (spec.trust_paths and trust_failures(spec)):
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
        if isinstance(revision, str) and REVISION_RE.fullmatch(revision):
            current = Freshness(revision, as_of)
    report["locked"] = fsops.lock_is_held(spec.lock_path) is True
    report["freshness"] = freshness_fields(
        current=current,
        published_revision=report.get("published_revision"),
        last_success_at=_parse_iso(report.get("last_success_at")),
        max_lag_seconds=spec.max_lag_seconds,
        now=clock.now(),
    )
    return clean_status(report)


__all__ = [
    "EXIT_BUILD",
    "EXIT_BUSY",
    "EXIT_INPUTS",
    "EXIT_OK",
    "EXIT_POST_PUBLISH",
    "EXIT_SETUP",
    "EXIT_SHRINK",
    "RUNNER_VERSION",
    "SHRINK_FLOOR",
    "RunOptions",
    "clean_status",
    "freshness_fields",
    "plan_stage",
    "read_status",
    "run_stage",
    "stage_status",
]
