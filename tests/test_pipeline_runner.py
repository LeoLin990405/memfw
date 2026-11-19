"""memfw-pipeline: the runner under the Stage contract (packages/memfw-pipeline).

Everything runs offline in temporary directories. The example stage
(``memfw_pipeline.example``) copies a flat directory of ``*.md`` files; test
stages below subclass it to fail, shrink, crash or leak on purpose.
"""

from __future__ import annotations

import errno
import json
import os
import random
import signal
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_SRC = ROOT / "packages" / "memfw-pipeline" / "src"
if str(PKG_SRC) not in sys.path:  # a workspace member; CI installs only memfw[dev]
    sys.path.insert(0, str(PKG_SRC))

from memfw_pipeline import (  # noqa: E402
    EXIT_BUILD,
    EXIT_BUSY,
    EXIT_INPUTS,
    EXIT_OK,
    EXIT_POST_PUBLISH,
    EXIT_SETUP,
    EXIT_SHRINK,
    BuildResult,
    Freshness,
    PostPublishStep,
    RunOptions,
    StageContext,
    StageError,
    StageSpec,
    StepResult,
    VerifyFailed,
    fsops,  # noqa: E402
    load_config,
    plan_stage,
    run_stage,
    stage_status,
)
from memfw_pipeline.example import CopyStage  # noqa: E402
from memfw_pipeline.runner import clean_status, freshness_fields  # noqa: E402

from memfw.ports.clock import FixedClock  # noqa: E402

T0 = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


# --- helpers -------------------------------------------------------------------------------
class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "source"
        self.source.mkdir()
        self.state = root / "state"
        self.out = root / "out"
        self.target = self.out / "artifact"
        self.marker = root / "marker.json"
        self.config = root / "pipeline.toml"
        self.write_config()

    def write_config(self, **extra: Any) -> None:
        lines = [
            "[pipeline]",
            f'state_dir = "{self.state}"',
            "[stages.example]",
            'factory = "memfw_pipeline.example:factory"',
            f'target = "{self.target}"',
        ]
        lines += [f"{key} = {json.dumps(value)}" for key, value in extra.items()]
        lines.append(f'options = {{ source = "{self.source}", marker = "{self.marker}" }}')
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @property
    def spec(self) -> StageSpec:
        return load_config(self.config).stage("example")

    def items(self, count: int, *, prefix: str = "item") -> None:
        for path in self.source.glob("*.md"):
            path.unlink()
        for index in range(count):
            (self.source / f"{prefix}-{index}.md").write_text(f"body {index}\n", "utf-8")

    def live(self) -> list[str]:
        return sorted(p.name for p in (self.target / "items").glob("*.md"))

    def status(self) -> dict[str, Any]:
        return json.loads((self.state / "example" / "status.json").read_text("utf-8"))

    def stage(self) -> CopyStage:
        return CopyStage({"source": str(self.source), "marker": str(self.marker)})

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.out.glob(".artifact.*"))


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path.resolve())


def run(env: Env, stage: Any = None, **options: bool) -> tuple[int, dict[str, Any]]:
    return run_stage(env.spec, RunOptions(**options), clock=FixedClock(T0), stage=stage)


# --- the happy path ------------------------------------------------------------------------
def test_first_build_publishes_and_records_counts(env: Env) -> None:
    env.items(3)
    code, status = run(env)
    assert code == EXIT_OK
    assert env.live() == ["item-0.md", "item-1.md", "item-2.md"]
    assert status["result"] == "built" and status["ok"] is True
    assert status["counts"] == {"items": 3, "skipped": 0}
    assert status["last_items"] == 3 and status["swap_method"] == "rename"
    assert status["shrink"] == {"baseline": None, "baseline_source": "none", "would_publish": 3}
    assert env.status() == status
    assert json.loads(env.marker.read_text("utf-8")) == {"items": 3}
    mode = (env.state / "example" / "status.json").stat().st_mode & 0o777
    assert mode == 0o600
    assert (env.state / "example").stat().st_mode & 0o777 == 0o700


def test_second_build_exchanges_real_directories(env: Env) -> None:
    env.items(2)
    assert run(env)[0] == EXIT_OK
    inode = env.target.stat().st_ino
    env.items(3)
    code, status = run(env)
    assert code == EXIT_OK and status["result"] == "built"
    assert status["swap_method"] in {"exchange", "rename_pair"}
    if sys.platform in ("darwin", "linux"):
        # both platforms ship an exchange call; a filesystem without it falls back
        assert status["swap_method"] == "exchange" or fsops._load_libc() is None
    assert env.target.is_dir() and not env.target.is_symlink()
    assert env.target.stat().st_ino != inode
    assert len(env.live()) == 3 and env.leftovers() == []


# --- unchanged inputs ----------------------------------------------------------------------
def test_unchanged_inputs_skip_the_build_but_run_post_publish(env: Env) -> None:
    env.items(2)
    assert run(env)[0] == EXIT_OK
    inode = env.target.stat().st_ino
    env.marker.unlink()  # a wiped downstream index
    code, status = run(env)
    assert code == EXIT_OK and status["result"] == "unchanged"
    assert env.target.stat().st_ino == inode  # not rebuilt
    assert json.loads(env.marker.read_text("utf-8")) == {"items": 2}  # healed
    assert status["post_publish"]["write_marker"]["ok"] is True
    assert status["last_items"] == 2


def test_unchanged_digest_rebuilds_when_the_live_artifact_lost_items(env: Env) -> None:
    env.items(4)
    assert run(env)[0] == EXIT_OK
    (env.target / "items" / "item-0.md").unlink()  # the status alone is not trusted
    code, status = run(env)
    assert code == EXIT_OK and status["result"] == "built"
    assert len(env.live()) == 4


def test_force_rebuilds_unchanged_inputs(env: Env) -> None:
    env.items(2)
    run(env)
    code, status = run(env, force=True)
    assert code == EXIT_OK and status["result"] == "built"


# --- lock ----------------------------------------------------------------------------------
def test_lock_held_exits_75_and_touches_nothing(env: Env) -> None:
    env.items(2)
    run(env)
    before = (env.state / "example" / "status.json").read_bytes()
    env.items(5)
    with fsops.single_writer(env.spec.lock_path) as held:
        assert held
        code, status = run(env)
    assert code == EXIT_BUSY and status["result"] == "busy"
    assert (env.state / "example" / "status.json").read_bytes() == before
    assert len(env.live()) == 2


def test_lock_held_by_another_process_exits_75_from_the_cli(env: Env) -> None:
    env.items(1)
    spec = env.spec
    fsops.private_mkdir(spec.state_dir)
    with fsops.single_writer(spec.lock_path) as held:
        assert held
        proc = _cli(["run", "example", "--config", str(env.config)])
    assert proc.returncode == 75
    assert json.loads(proc.stdout.splitlines()[-1])["result"] == "busy"
    assert not spec.status_path.exists() and not env.target.exists()


def test_planted_lock_symlink_is_refused(env: Env) -> None:
    spec = env.spec
    fsops.private_mkdir(spec.state_dir)
    elsewhere = env.root / "elsewhere"
    spec.lock_path.symlink_to(elsewhere)
    code, status = run(env)
    assert code == EXIT_SETUP and status["errors"] == ["lock_unusable"]
    assert not elsewhere.exists()


def test_unwritable_status_is_a_setup_error(env: Env) -> None:
    env.items(1)
    (env.state / "example" / "status.json").mkdir(parents=True)
    code, status = run(env)
    assert code == EXIT_SETUP and status["errors"] == ["status_unwritable"]
    assert not env.target.exists()


def _cli(args: Sequence[str], extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    path = [str(PKG_SRC), str(ROOT / "src")]
    if extra_path is not None:
        path.insert(0, str(extra_path))
    environment = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    environment["PYTHONPATH"] = os.pathsep.join(path)
    return subprocess.run(
        [sys.executable, "-m", "memfw_pipeline", *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=False,
    )


# --- a killed run --------------------------------------------------------------------------
KILL_STAGE = textwrap.dedent(
    """
    import os, signal
    from memfw_pipeline.example import CopyStage

    class KillStage(CopyStage):
        name = "kill-copy"

        def build(self, ctx, workdir):
            (workdir / "partial.md").write_text("half written", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)

    def factory(options):
        return KillStage(options)
    """
)


def test_killed_run_leaves_running_with_the_last_good_counts(env: Env) -> None:
    env.items(3)
    assert run(env)[0] == EXIT_OK
    good = env.status()
    modules = env.root / "modules"
    modules.mkdir()
    (modules / "killstage.py").write_text(KILL_STAGE, "utf-8")
    text = env.config.read_text("utf-8").replace(
        "memfw_pipeline.example:factory", "killstage:factory"
    )
    env.config.write_text(text, "utf-8")
    env.items(4)
    proc = _cli(["run", "example", "--config", str(env.config)], extra_path=modules)
    assert proc.returncode == -signal.SIGKILL
    status = env.status()
    assert status["result"] == "running" and status["ok"] is False
    for key in ("last_items", "last_success_at", "published_digest", "published_revision"):
        assert status[key] == good[key]
    assert len(env.leftovers()) == 1  # the half-built work directory
    assert len(env.live()) == 3  # the live artifact is untouched
    env.write_config()
    code, status = run(env)
    assert code == EXIT_OK and status["result"] == "built"
    assert status["leftovers_removed"] == 1 and env.leftovers() == []
    assert status["shrink"]["baseline"] == 3 and status["shrink"]["baseline_source"] == "status"


def test_orphaned_old_artifact_is_restored_before_cleanup(env: Env) -> None:
    env.items(3)
    run(env)
    os.rename(env.target, fsops.new_name(env.target, "old"))  # crash between the renames
    code, status = run(env)
    assert "previous_artifact_restored" in status.get("warnings", [])
    assert code == EXIT_OK and status["result"] == "unchanged"
    assert len(env.live()) == 3 and env.leftovers() == []


# --- failures keep the live artifact -------------------------------------------------------
class FailingStage(CopyStage):
    def __init__(self, options: dict[str, Any], phase: str) -> None:
        super().__init__(options)
        self.phase = phase

    def inputs_digest(self, ctx: StageContext) -> str:
        if self.phase == "inputs":
            raise OSError("cannot read /secret/place")
        return super().inputs_digest(ctx)

    def build(self, ctx: StageContext, workdir: Path) -> BuildResult:
        result = super().build(ctx, workdir)
        if self.phase == "build":
            raise RuntimeError("boom")
        return result

    def verify(self, ctx: StageContext, workdir: Path, result: BuildResult) -> None:
        if self.phase == "verify":
            raise VerifyFailed("reader_mismatch")
        super().verify(ctx, workdir, result)


@pytest.mark.parametrize(
    ("phase", "exit_code", "result", "errors"),
    [
        ("inputs", EXIT_INPUTS, "inputs_unavailable", ["inputs"]),
        ("build", EXIT_BUILD, "build_failed", ["build"]),
        ("verify", EXIT_BUILD, "verify_failed", ["verify:reader_mismatch"]),
    ],
)
def test_failures_keep_the_live_artifact(
    env: Env, phase: str, exit_code: int, result: str, errors: list[str]
) -> None:
    env.items(2)
    run(env)
    env.items(3)
    options = {"source": str(env.source)}
    code, status = run(env, stage=FailingStage(options, phase))
    assert code == exit_code and status["result"] == result
    assert status["errors"] == errors
    assert env.live() == ["item-0.md", "item-1.md"] and env.leftovers() == []
    assert status["last_items"] == 2 and status["artifact_present"] is True


def test_verify_rejects_a_count_mismatch(env: Env) -> None:
    class Liar(CopyStage):
        def build(self, ctx: StageContext, workdir: Path) -> BuildResult:
            super().build(ctx, workdir)
            return BuildResult({"items": 7}, ("items",))

    env.items(2)
    code, status = run(env, stage=Liar({"source": str(env.source)}))
    assert code == EXIT_BUILD and status["errors"] == ["verify:count_mismatch"]
    assert not env.target.exists()


def test_post_publish_failure_exits_4_after_retries(env: Env) -> None:
    calls: list[int] = []

    class Flaky(CopyStage):
        def post_publish(self, ctx: StageContext) -> Sequence[PostPublishStep]:
            def sync(c: StageContext) -> StepResult:
                calls.append(1)
                raise StageError("unreachable", "http://127.0.0.1:1 refused")

            return (PostPublishStep("sync", sync, attempts=2),)

    env.items(2)
    code, status = run(env, stage=Flaky({"source": str(env.source)}))
    assert code == EXIT_POST_PUBLISH and status["result"] == "post_publish_failed"
    assert len(calls) == 2
    assert status["post_publish"]["sync"] == {"ok": False, "attempts": 2, "error": "unreachable"}
    assert status["errors"] == ["sync:unreachable"]
    assert len(env.live()) == 2  # published anyway; only the step failed


# --- swap paths through the runner ---------------------------------------------------------
def test_exchange_error_keeps_the_old_artifact(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.items(2)
    run(env)

    def broken(first: Path, second: Path) -> bool:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(fsops, "exchange", broken)
    env.items(3)
    code, status = run(env)
    assert code == EXIT_BUILD and status["errors"] == ["publish"]
    assert status["exception"] == "OSError"
    assert len(env.live()) == 2 and env.leftovers() == []


def test_rename_pair_rollback_keeps_the_old_artifact(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.items(2)
    run(env)
    monkeypatch.setattr(fsops, "exchange", lambda a, b: False)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if ".artifact.new-" in str(src):
            raise OSError(errno.EIO, "I/O error")
        real_rename(src, dst)

    monkeypatch.setattr(fsops.os, "rename", rename)
    env.items(3)
    code, status = run(env)
    monkeypatch.setattr(fsops.os, "rename", real_rename)
    assert code == EXIT_BUILD and status["result"] == "publish_failed"
    assert len(env.live()) == 2 and env.leftovers() == []


def test_rename_pair_fallback_publishes(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.items(2)
    run(env)
    monkeypatch.setattr(fsops, "exchange", lambda a, b: False)
    env.items(3)
    code, status = run(env)
    assert code == EXIT_OK and status["swap_method"] == "rename_pair"
    assert len(env.live()) == 3 and env.leftovers() == []


# --- shrink guard --------------------------------------------------------------------------
def test_empty_first_build_is_refused(env: Env) -> None:
    code, status = run(env)
    assert code == EXIT_SHRINK and status["errors"] == ["empty_build"]
    assert status["shrink"]["baseline_source"] == "none"
    assert not env.target.exists() and env.leftovers() == []
    code, status = run(env, allow_shrink=True)
    assert code == EXIT_OK and env.live() == []
    assert status["shrink"]["allowed"] is True


@pytest.mark.parametrize(
    ("before", "after", "refused"), [(4, 2, False), (4, 1, True), (5, 2, True)]
)
def test_shrink_floor_is_half_of_the_last_published_count(
    env: Env, before: int, after: int, refused: bool
) -> None:
    env.items(before)
    run(env)
    env.items(after)
    code, status = run(env)
    assert (code == EXIT_SHRINK) is refused
    assert len(env.live()) == (before if refused else after)
    assert status["shrink"]["baseline"] == before
    if refused:
        assert status["errors"] == ["shrink_refused"] and status["last_items"] == before


def test_an_empty_build_is_refused_even_after_a_small_one(env: Env) -> None:
    env.items(1)
    run(env)
    env.items(0)
    code, status = run(env)
    assert code == EXIT_SHRINK and status["errors"] == ["empty_build"]
    assert run(env, allow_shrink=True)[0] == EXIT_OK and env.live() == []


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_schema"])
def test_shrink_baseline_falls_back_to_the_live_count(env: Env, damage: str) -> None:
    env.items(4)
    run(env)
    path = env.state / "example" / "status.json"
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("{not json", "utf-8")
    else:
        path.write_text(json.dumps({"schema": 99, "last_items": 1}), "utf-8")
    env.items(1)
    code, status = run(env)
    assert code == EXIT_SHRINK
    assert status["shrink"] == {"baseline": 4, "baseline_source": "live", "would_publish": 1}
    if damage != "missing":
        assert "status_unreadable" in status["warnings"]
    assert len(env.live()) == 4


def test_allow_shrink_publishes_a_small_build(env: Env) -> None:
    env.items(4)
    run(env)
    env.items(1)
    code, status = run(env, allow_shrink=True)
    assert code == EXIT_OK and len(env.live()) == 1 and status["last_items"] == 1


# --- status hygiene: never names or text ---------------------------------------------------
_CANARY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz0123456789-./é中"


def _canary(rng: random.Random) -> str:
    # an uppercase letter, a space and a non-ASCII char: never a code, digest or revision
    body = "".join(rng.choice(_CANARY_ALPHABET) for _ in range(rng.randint(4, 12)))
    return f"Canary {body}é"


class LeakyStage(CopyStage):
    """Puts the canary everywhere a careless stage might."""

    def __init__(self, options: dict[str, Any], canary: str, mode: str) -> None:
        super().__init__(options)
        self.canary = canary
        self.mode = mode

    def build(self, ctx: StageContext, workdir: Path) -> BuildResult:
        result = super().build(ctx, workdir)
        if self.mode == "build_error":
            raise StageError(self.canary, f"failed on {self.canary}")
        if self.mode == "bad_counts":
            return BuildResult({"items": 1, self.canary: 1})
        if self.mode == "verify_error":
            raise ValueError(self.canary)
        return result

    def freshness(self, ctx: StageContext) -> Freshness:
        if self.mode == "freshness_error":
            raise RuntimeError(self.canary)
        return super().freshness(ctx)

    def publish(self, ctx: StageContext, workdir: Path) -> str:
        method = super().publish(ctx, workdir)
        return self.canary if self.mode == "publish_method" else method

    def post_publish(self, ctx: StageContext) -> Sequence[PostPublishStep]:
        canary = self.canary

        def step(c: StageContext) -> StepResult:
            raise StageError(canary, canary)

        return (PostPublishStep("sync", step),) if self.mode == "post_error" else ()


MODES = (
    "ok",
    "build_error",
    "bad_counts",
    "verify_error",
    "freshness_error",
    "publish_method",
    "post_error",
)


@pytest.mark.parametrize("seed", range(6))
def test_status_never_holds_item_names_or_text(env: Env, seed: int) -> None:
    rng = random.Random(seed)
    for mode in MODES:
        canary = _canary(rng)
        for path in env.source.glob("*"):
            path.unlink()
        for index in range(3):
            name = f"{canary}-{index}.md".replace("/", "_")
            (env.source / name).write_text(f"{canary} text {index}\n", "utf-8")
        stage = LeakyStage({"source": str(env.source)}, canary, mode)
        code, status = run(env, stage=stage, force=True, allow_shrink=True)
        written = (env.state / "example" / "status.json").read_text("utf-8")
        for text in (written, json.dumps(status, ensure_ascii=False)):
            assert canary not in text, mode
            assert "Canary" not in text, mode
        assert stage_status(env.spec, clock=FixedClock(T0), stage=stage)
        assert "Canary" not in json.dumps(plan_stage(env.spec, clock=FixedClock(T0), stage=stage))


@pytest.mark.parametrize("seed", range(20))
def test_clean_status_redacts_anything_that_is_not_a_known_shape(seed: int) -> None:
    rng = random.Random(seed)
    canary = _canary(rng)
    keys = ["result", "errors", "counts", canary, "stage", "kind", "exception", "finished_at"]
    payload: dict[str, Any] = {}
    for key in keys:
        payload[key] = rng.choice([canary, [canary, "ok_code"], {canary: canary, "n": 1}])
    cleaned = clean_status(payload)
    assert canary not in json.dumps(cleaned, ensure_ascii=False)
    assert clean_status({"result": "built", "errors": ["build:x"]}) == {
        "result": "built",
        "errors": ["build:x"],
    }


# --- freshness -----------------------------------------------------------------------------
def test_freshness_lag_and_stale_rules() -> None:
    now = T0
    current = Freshness("rev-2", T0 - timedelta(hours=3))
    same = freshness_fields(
        current=current,
        published_revision="rev-2",
        last_success_at=T0 - timedelta(minutes=5),
        max_lag_seconds=3600,
        now=now,
    )
    assert same["lag_seconds"] == 0 and same["behind"] is False and same["stale"] is False
    behind = freshness_fields(
        current=current,
        published_revision="rev-1",
        last_success_at=T0 - timedelta(minutes=5),
        max_lag_seconds=3600,
        now=now,
    )
    assert behind["behind"] is True and behind["lag_seconds"] == 3 * 3600 and behind["stale"]
    timer_stopped = freshness_fields(
        current=current,
        published_revision="rev-2",
        last_success_at=T0 - timedelta(days=15),
        max_lag_seconds=86400,
        now=now,
    )
    assert timer_stopped["lag_seconds"] == 0 and timer_stopped["stale"] is True
    never = freshness_fields(
        current=None, published_revision=None, last_success_at=None, max_lag_seconds=60, now=now
    )
    assert never["stale"] is True and never["lag_seconds"] is None


def test_status_reports_a_stage_that_fell_behind(env: Env) -> None:
    revision = env.root / "REVISION"
    revision.write_text("abc123\n", "utf-8")
    text = env.config.read_text("utf-8").replace(
        f'marker = "{env.marker}"', f'marker = "{env.marker}", revision_file = "{revision}"'
    )
    env.config.write_text(text, "utf-8")
    env.items(2)
    clock = FixedClock(T0)
    code, status = run_stage(env.spec, clock=clock)
    assert code == EXIT_OK and status["published_revision"] == "abc123"
    revision.write_text("def456\n", "utf-8")
    later = datetime.fromtimestamp(revision.stat().st_mtime, UTC) + timedelta(days=2)
    clock.set(max(later, T0))
    report = stage_status(env.spec, clock=clock)
    fresh = report["freshness"]
    assert fresh["behind"] is True and fresh["source_revision"] == "def456"
    assert fresh["published_revision"] == "abc123" and fresh["stale"] is True
    assert run_stage(env.spec, clock=clock)[1]["freshness"]["behind"] is False


def test_status_cli_exit_code_follows_ok_and_stale(env: Env) -> None:
    revision = env.root / "REVISION"
    revision.write_text("abc123\n", "utf-8")
    text = env.config.read_text("utf-8").replace(
        f'marker = "{env.marker}"', f'marker = "{env.marker}", revision_file = "{revision}"'
    )
    env.config.write_text(text, "utf-8")
    env.items(2)
    assert _cli(["status", "--config", str(env.config)]).returncode == 1  # never ran
    assert run_stage(env.spec)[0] == EXIT_OK  # the real clock, as the CLI uses
    assert _cli(["status", "--config", str(env.config)]).returncode == 0
    revision.write_text("def456\n", "utf-8")
    proc = _cli(["status", "--config", str(env.config)])
    report = json.loads(proc.stdout)["stages"][0]
    assert report["freshness"]["behind"] is True  # behind, but well within max_lag_seconds
    assert proc.returncode == 0
    path = env.state / "example" / "status.json"
    stopped = json.loads(path.read_text("utf-8"))
    stopped["last_success_at"] = "2020-01-01T00:00:00Z"  # the timer stopped long ago
    path.write_text(json.dumps(stopped), "utf-8")
    assert _cli(["status", "--config", str(env.config)]).returncode == 1


def test_status_of_a_stage_that_never_ran(env: Env) -> None:
    report = stage_status(env.spec, probe=False)
    assert report["result"] == "never_run" and report["freshness"]["stale"] is True


# --- plan reads only -----------------------------------------------------------------------
def test_plan_writes_nothing(env: Env) -> None:
    env.items(2)
    report = plan_stage(env.spec, clock=FixedClock(T0))
    assert report["action"] == "build" and report["live_count"] is None
    assert not env.state.exists() and not env.target.exists()
    run(env)
    report = plan_stage(env.spec, clock=FixedClock(T0))
    assert report["action"] == "skip_unchanged" and report["post_publish_steps"] == 1
    env.items(3)
    assert plan_stage(env.spec, clock=FixedClock(T0))["action"] == "build"


# --- the CLI end to end --------------------------------------------------------------------
def test_cli_run_prints_one_status_line(env: Env) -> None:
    env.items(2)
    proc = _cli(["run", "example", "--config", str(env.config)])
    assert proc.returncode == 0, proc.stderr
    status = json.loads(proc.stdout.strip().splitlines()[-1])
    assert status["result"] == "built" and status["last_items"] == 2
    proc = _cli(["run", "missing", "--config", str(env.config)])
    assert proc.returncode == 2 and "unknown_stage" in proc.stdout
