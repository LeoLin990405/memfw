"""memfw-pipeline: swap primitives (mocked libc), path trust, config rules, unit templates.

The exchange call is exercised on every code path with a fake C library, the
way the deployment-side builder's tests do, so Linux and macOS paths are both
covered on either platform. Unit templates are compared with snapshots in
``tests/fixtures/pipeline/``.
"""

from __future__ import annotations

import ast
import ctypes
import errno
import json
import os
import plistlib
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_SRC = ROOT / "packages" / "memfw-pipeline" / "src"
if str(PKG_SRC) not in sys.path:  # a workspace member; CI installs only memfw[dev]
    sys.path.insert(0, str(PKG_SRC))

from memfw_pipeline import (  # noqa: E402
    SetupError,
    StageSpec,
    fsops,  # noqa: E402
    load_config,
    run_stage,
)
from memfw_pipeline.config import trust_failures  # noqa: E402
from memfw_pipeline.units import launchd_plist, systemd_units  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "pipeline"


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path.resolve()
    workdir, target = base / ".t.new-1", base / "t"
    workdir.mkdir()
    (workdir / "new.txt").write_text("new", "utf-8")
    target.mkdir()
    (target / "old.txt").write_text("old", "utf-8")
    return workdir, target


class FakeLibc:
    """``renameat2`` / ``renamex_np`` that record their arguments and really swap."""

    def __init__(self, *, rc: int = 0, err: int = 0, with_fn: bool = True) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.rc, self.err = rc, err
        if with_fn:
            self.renameat2 = self._renameat2
            self.renamex_np = self._renamex_np

    def _swap(self, a: bytes, b: bytes) -> int:
        if self.rc != 0:
            ctypes.set_errno(self.err)
            return self.rc
        tmp = a + b".swap"
        os.rename(a, tmp)
        os.rename(b, a)
        os.rename(tmp, b)
        return 0

    def _renameat2(self, fd1: int, a: bytes, fd2: int, b: bytes, flags: int) -> int:
        self.calls.append(("renameat2", fd1, a, fd2, b, flags))
        return self._swap(a, b)

    def _renamex_np(self, a: bytes, b: bytes, flags: int) -> int:
        self.calls.append(("renamex_np", a, b, flags))
        return self._swap(a, b)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(platform: str, libc: Any) -> Any:
        monkeypatch.setattr(fsops, "_platform", lambda: platform)
        monkeypatch.setattr(fsops, "_load_libc", lambda: libc)
        monkeypatch.setattr(ctypes, "get_errno", lambda: getattr(libc, "err", 0))
        return libc

    return install


# --- swap ----------------------------------------------------------------------------------
def test_first_publish_is_a_plain_rename(tmp_path: Path) -> None:
    workdir, target = _dirs(tmp_path)
    (target / "old.txt").unlink()
    target.rmdir()
    assert fsops.swap_in(workdir, target) == "rename"
    assert (target / "new.txt").exists() and not workdir.exists()


def test_linux_renameat2_exchange(tmp_path: Path, fake: Any) -> None:
    libc = fake("linux", FakeLibc())
    workdir, target = _dirs(tmp_path)
    assert fsops.swap_in(workdir, target) == "exchange"
    assert libc.calls == [("renameat2", -100, os.fsencode(workdir), -100, os.fsencode(target), 2)]
    assert (target / "new.txt").exists() and not workdir.exists()  # old one removed


def test_macos_renamex_np_swap(tmp_path: Path, fake: Any) -> None:
    libc = fake("darwin", FakeLibc())
    workdir, target = _dirs(tmp_path)
    assert fsops.swap_in(workdir, target) == "exchange"
    assert libc.calls == [("renamex_np", os.fsencode(workdir), os.fsencode(target), 0x2)]
    assert (target / "new.txt").exists() and not workdir.exists()


@pytest.mark.parametrize("err", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.EXDEV])
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_unsupported_exchange_falls_back_to_rename_pair(
    tmp_path: Path, fake: Any, platform: str, err: int
) -> None:
    libc = fake(platform, FakeLibc(rc=-1, err=err))
    workdir, target = _dirs(tmp_path)
    assert fsops.swap_in(workdir, target) == "rename_pair"
    assert len(libc.calls) == 1
    assert (target / "new.txt").exists() and not workdir.exists()
    assert list(tmp_path.resolve().glob(".t.old-*")) == []


@pytest.mark.parametrize(
    ("platform", "libc"),
    [("linux", None), ("linux", FakeLibc(with_fn=False)), ("darwin", None), ("sunos5", FakeLibc())],
)
def test_no_exchange_call_falls_back(tmp_path: Path, fake: Any, platform: str, libc: Any) -> None:
    fake(platform, libc)
    workdir, target = _dirs(tmp_path)
    assert fsops.swap_in(workdir, target) == "rename_pair"
    assert (target / "new.txt").exists()


def test_exchange_error_raises_and_keeps_the_old_artifact(tmp_path: Path, fake: Any) -> None:
    fake("linux", FakeLibc(rc=-1, err=errno.EIO))
    workdir, target = _dirs(tmp_path)
    with pytest.raises(OSError):
        fsops.swap_in(workdir, target)
    assert (target / "old.txt").exists() and (workdir / "new.txt").exists()


def test_rename_pair_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workdir, target = _dirs(tmp_path)
    real = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(src) == workdir:
            raise OSError(errno.EIO, "I/O error")
        real(src, dst)

    monkeypatch.setattr(fsops.os, "rename", rename)
    with pytest.raises(OSError):
        fsops.swap_in(workdir, target, use_exchange=False)
    assert (target / "old.txt").exists() and (workdir / "new.txt").exists()
    assert list(tmp_path.resolve().glob(".t.old-*")) == []


def test_swap_refuses_a_symlinked_target(tmp_path: Path) -> None:
    workdir, target = _dirs(tmp_path)
    real = tmp_path.resolve() / "real"
    target.rename(real)
    target.symlink_to(real)
    with pytest.raises(OSError):
        fsops.swap_in(workdir, target)
    assert target.is_symlink() and (real / "old.txt").exists()


def test_cleanup_removes_only_this_targets_leftovers(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    target = base / "view[1]"  # glob metacharacters in the name are escaped
    for name in (".view[1].new-1-2", ".view[1].old-3-4", ".view1.new-5", ".other.new-6"):
        (base / name).mkdir()
        (base / name / "f").write_text("x", "utf-8")
    (base / ".view[1].new-7").write_text("file", "utf-8")
    assert fsops.cleanup_leftovers(target) == 3
    assert sorted(p.name for p in base.iterdir()) == [".other.new-6", ".view1.new-5"]


def test_fsync_tree_skips_symlinks(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "f").write_text("x", "utf-8")
    (root / "dangling").symlink_to(root / "nowhere")
    fsops.fsync_tree(root)  # no error on the dangling link


def test_status_write_is_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path.resolve() / "status.json"
    fsops.write_json_atomic(path, {"a": 1})
    fsops.write_json_atomic(path, {"a": 2})
    assert fsops.read_json(path) == {"a": 2}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["status.json"]
    link = tmp_path.resolve() / "link.json"
    link.symlink_to(path)
    assert fsops.read_json(link) is None  # O_NOFOLLOW


def test_private_mkdir_creates_every_missing_parent_0700(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    fsops.private_mkdir(base / "a" / "b" / "c")
    for rel in ("a", "a/b", "a/b/c"):
        assert stat.S_IMODE((base / rel).stat().st_mode) == 0o700


# --- path trust ----------------------------------------------------------------------------
def test_trusted_private_tree(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    fsops.private_mkdir(base / "x")
    assert fsops.path_is_trusted(base / "x")
    assert fsops.path_is_trusted(base / "x" / "not-yet" / "created")


def test_group_writable_component_is_untrusted(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "x" / "y").mkdir(parents=True)
    os.chmod(base / "x", 0o775)
    assert not fsops.path_is_trusted(base / "x" / "y")


def test_symlink_component_is_untrusted(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "real").mkdir()
    (base / "link").symlink_to(base / "real")
    assert not fsops.path_is_trusted(base / "link" / "file")


def test_other_owner_is_untrusted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.geteuid() == 0:
        pytest.skip("as root every component is root-or-self")
    monkeypatch.setattr(fsops, "_geteuid", lambda: os.geteuid() + 4242)
    assert not fsops.path_is_trusted(tmp_path.resolve())


def test_root_sticky_ancestor_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path.resolve()
    leaf = base / "leaf"
    leaf.mkdir(mode=0o700)
    real_lstat = os.lstat

    def lstat(path: Any) -> Any:
        info = real_lstat(path)
        if Path(path) == base:  # pretend base is /tmp: root-owned, 1777
            fields = list(info)
            fields[stat.ST_MODE] = stat.S_IFDIR | 0o1777
            fields[stat.ST_UID] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(fsops.os, "lstat", lstat)
    assert fsops.path_is_trusted(leaf)
    assert not fsops.path_is_trusted(base)  # the leaf itself may not be world-writable


def _config(base: Path, **stage: str) -> Path:
    path = base / "pipeline.toml"
    body = "\n".join(f'{k} = "{v}"' for k, v in stage.items())
    path.write_text(
        f'[pipeline]\nstate_dir = "{base / "state"}"\n[stages.example]\n'
        f'factory = "memfw_pipeline.example:factory"\ntarget = "{base / "out" / "t"}"\n'
        f"trust_paths = true\n{body}\n"
        f'options = {{ source = "{base / "src"}" }}\n',
        "utf-8",
    )
    return path


def test_runner_with_trust_paths_refuses_an_untrusted_tree(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "src").mkdir()
    (base / "src" / "a.md").write_text("a", "utf-8")
    spec = load_config(_config(base)).stage("example")
    if trust_failures(spec):
        pytest.skip("the interpreter or checkout itself is not in a trusted tree here")
    assert run_stage(spec)[0] == 0
    os.chmod(base / "out", 0o777)
    code, status = run_stage(spec)
    assert code == 2 and status["errors"] == ["target_untrusted"]
    assert "running" not in (spec.status_path.read_text("utf-8"))
    os.chmod(base / "out", 0o700)
    os.chmod(base / "pipeline.toml", 0o666)
    assert run_stage(spec)[1]["errors"] == ["config_untrusted"]


def test_trust_check_covers_the_factory_package(tmp_path: Path, monkeypatch: Any) -> None:
    base = tmp_path.resolve()
    modules = base / "mods"
    modules.mkdir()
    (modules / "loose_stage.py").write_text("def factory(o):\n    return None\n", "utf-8")
    os.chmod(modules, 0o777)
    monkeypatch.syspath_prepend(str(modules))
    path = _config(base)
    path.write_text(
        path.read_text("utf-8").replace("memfw_pipeline.example:factory", "loose_stage:factory"),
        "utf-8",
    )
    assert "factory_untrusted" in trust_failures(load_config(path).stage("example"))


# --- config rules --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda t: t.replace('state_dir = "/', 'state_dir = "rel/'), "state_dir_invalid"),
        (lambda t: t + "typo = 1\n", "stage_config_invalid"),
        (lambda t: t.replace("memfw_pipeline.example:factory", "no colon"), "factory_invalid"),
        (lambda t: t.replace("[stages.example]", "[stages.Bad]"), "stage_name_invalid"),
        (lambda t: t.replace("/out/t", "/state/t"), "target_invalid"),
        (lambda t: t.replace("/out/t", ""), "target_invalid"),  # the state dir inside it
        (lambda t: t + "max_lag_seconds = 5\n", "max_lag_invalid"),
    ],
)
def test_config_rejects(tmp_path: Path, mutate: Any, reason: str) -> None:
    base = tmp_path.resolve()
    path = base / "p.toml"
    text = (
        f'[pipeline]\nstate_dir = "{base}/state"\n[stages.example]\n'
        f'factory = "memfw_pipeline.example:factory"\ntarget = "{base}/out/t"\n'
    )
    path.write_text(mutate(text), "utf-8")
    with pytest.raises(SetupError) as info:
        load_config(path)
    assert info.value.code == reason


def test_config_rejects_nested_targets(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    path = base / "p.json"
    config = {
        "pipeline": {"state_dir": f"{base}/state"},
        "stages": {
            "a": {"factory": "m:f", "target": f"{base}/out"},
            "b": {"factory": "m:f", "target": f"{base}/out/inner"},
        },
    }
    path.write_text(json.dumps(config), "utf-8")
    with pytest.raises(SetupError) as info:
        load_config(path)
    assert info.value.code == "target_invalid"


# --- unit templates ------------------------------------------------------------------------
def _spec(trust: bool | None = True, **unit: Any) -> StageSpec:
    return StageSpec(
        name="admitted-view",
        factory="memfw_pipeline.example:factory",
        target=Path("/srv/memfw/derived/view"),
        state_dir=Path("/srv/memfw/pipeline-state/admitted-view"),
        config_path=Path("/srv/memfw/pipeline.toml"),
        trust_paths=trust,
        unit=unit,
    )


FULL_UNIT = {
    "user": "memfw",
    "schedule": "*-*-* 03:30:00",
    "capabilities": ["CAP_DAC_READ_SEARCH", "CAP_DAC_OVERRIDE"],
    "read_only_paths": ["/srv/memfw/authority"],
    "environment": {"MEMFW_EMBEDDING_URL": "http://127.0.0.1:11434/v1/embeddings"},
    "timeout_seconds": 6000,
}


def _snapshot(name: str, text: str) -> None:
    path = FIXTURES / name
    if os.environ.get("MEMFW_UPDATE_SNAPSHOTS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    assert text == path.read_text("utf-8"), f"{name} changed; MEMFW_UPDATE_SNAPSHOTS=1 rewrites it"


def test_systemd_snapshot() -> None:
    text = systemd_units(_spec(**FULL_UNIT), python="/opt/memfw/venv/bin/python")
    _snapshot("admitted-view.systemd.txt", text)
    for line in (
        "ProtectSystem=strict",
        "ReadWritePaths=/srv/memfw/pipeline-state/admitted-view /srv/memfw/derived",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "NoNewPrivileges=true",
        "CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE",
        "SuccessExitStatus=75",
        "InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock "
        "-/run/containerd/containerd.sock -/run/podman/podman.sock -/run/dbus "
        "-/run/systemd/private",
        "AmbientCapabilities=CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE",
        "SystemCallFilter=@system-service",
        "SystemCallErrorNumber=EPERM",
        "RestrictNamespaces=true",
        "ExecStart=/opt/memfw/venv/bin/python -I -m memfw_pipeline run admitted-view "
        "--config /srv/memfw/pipeline.toml",
    ):
        assert line in text.splitlines()
    assert "CAP_DAC_OVERRIDE is needed for READS" in text


def test_systemd_defaults_snapshot() -> None:
    text = systemd_units(_spec(), python="/opt/memfw/venv/bin/python")
    _snapshot("admitted-view.systemd-defaults.txt", text)
    assert "CapabilityBoundingSet=" in text.splitlines()  # none by default
    assert "OnCalendar=daily" in text and "User=" not in text
    assert "AmbientCapabilities" not in text  # root: the bounding set is enough


def test_user_with_capabilities_gets_ambient_capabilities() -> None:
    text = systemd_units(
        _spec(user="memfw", capabilities=["CAP_DAC_READ_SEARCH"]),
        python="/opt/memfw/venv/bin/python",
    )
    assert "AmbientCapabilities=CAP_DAC_READ_SEARCH" in text.splitlines()
    plain = systemd_units(_spec(user="memfw"), python="/opt/memfw/venv/bin/python")
    assert "AmbientCapabilities" not in plain


@pytest.mark.parametrize("trust", [None, False])
def test_root_unit_requires_trust_paths(trust: bool | None) -> None:
    with pytest.raises(SetupError) as info:
        systemd_units(_spec(trust=trust), python="/opt/memfw/venv/bin/python")
    assert info.value.code == "unit_requires_trust_paths"
    with pytest.raises(SetupError):
        launchd_plist(_spec(trust=trust), python="/opt/memfw/venv/bin/python")
    # explicit override, or a non-root unit, is fine
    systemd_units(_spec(trust=trust), python="/opt/memfw/venv/bin/python", allow_untrusted=True)
    systemd_units(_spec(trust=trust, user="memfw"), python="/opt/memfw/venv/bin/python")


def test_trust_paths_defaults_on_for_root(monkeypatch: pytest.MonkeyPatch) -> None:
    import memfw_pipeline.config as config

    spec = _spec(trust=None)
    monkeypatch.setattr(config.os, "geteuid", lambda: 0)
    assert spec.trust_enabled is True
    monkeypatch.setattr(config.os, "geteuid", lambda: 1000)
    assert spec.trust_enabled is False
    assert _spec(trust=False).trust_enabled is False


def test_launchd_snapshot() -> None:
    unit = {"launchd_hour": 4, "launchd_minute": 15, "log_dir": "/srv/memfw/logs"}
    text = launchd_plist(_spec(**unit), python="/opt/memfw/venv/bin/python")
    _snapshot("admitted-view.launchd.plist", text)
    data = plistlib.loads(text.encode("utf-8"))
    assert data["StartCalendarInterval"] == {"Hour": 4, "Minute": 15}
    assert data["ProgramArguments"][:4] == [
        "/opt/memfw/venv/bin/python",
        "-I",
        "-m",
        "memfw_pipeline",
    ]


def test_launchd_interval() -> None:
    text = launchd_plist(_spec(launchd_interval_seconds=900), python="/opt/memfw/venv/bin/python")
    data = plistlib.loads(text.encode("utf-8"))
    assert data["StartInterval"] == 900 and "StartCalendarInterval" not in data


@pytest.mark.parametrize(
    ("render", "unit"),
    [
        (systemd_units, {"capabilities": ["CAP_SYS_ADMIN; rm"]}),
        (systemd_units, {"environment": {"HTTPS_PROXY": "http://127.0.0.1:1"}}),
        (launchd_plist, {"environment": {"A": "two words"}}),
        (systemd_units, {"read_only_paths": ["/srv/with space"]}),
        (systemd_units, {"read_only_paths": ["relative/path"]}),
        (systemd_units, {"schedule": "daily\nExecStartPre=/bin/sh"}),
        (systemd_units, {"user": "Root User"}),
        (systemd_units, {"unknown": 1}),
        (launchd_plist, {"launchd_interval_seconds": 900, "launchd_hour": 3}),
        (launchd_plist, {"launchd_label": "bad label"}),
    ],
)
def test_unit_rejects_unsafe_values(render: Any, unit: dict[str, Any]) -> None:
    with pytest.raises(SetupError):
        render(_spec(**unit), python="/opt/memfw/venv/bin/python")


# --- import boundaries ---------------------------------------------------------------------
def test_package_imports_only_stdlib_and_the_ports() -> None:
    allowed = set(sys.stdlib_module_names) | {"memfw_pipeline", "memfw_ports"}
    for path in sorted((PKG_SRC / "memfw_pipeline").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                ok = name.split(".")[0] in allowed or name.startswith("memfw.ports.")
                assert ok, (path.name, name)
    for path in (ROOT / "src" / "memfw").rglob("*.py"):
        assert "memfw_pipeline" not in path.read_text(encoding="utf-8"), path


def test_only_the_seam_names_the_ports_and_the_pin_matches() -> None:
    import tomllib

    for path in sorted((PKG_SRC / "memfw_pipeline").glob("*.py")):
        if path.name == "_ports.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert not node.module.startswith(("memfw.", "memfw_")), (path.name, node.module)
    package = ROOT / "packages"
    data = tomllib.loads((package / "memfw-pipeline" / "pyproject.toml").read_text("utf-8"))
    ports = tomllib.loads((package / "memfw-ports" / "pyproject.toml").read_text("utf-8"))
    assert data["project"]["dependencies"] == [f"memfw-ports=={ports['project']['version']}"]


# --- crash-leftover restore ----------------------------------------------------------------
def test_restore_refuses_more_than_one_candidate(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    target = base / "t"
    for name in (".t.old-1-2", ".t.old-3-4"):
        (base / name).mkdir()
    assert fsops.restore_orphaned_old(target) == "ambiguous"
    assert not target.exists() and len(list(base.glob(".t.old-*"))) == 2


def test_restore_reports_a_failed_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path.resolve()
    (base / ".t.old-1-2").mkdir()

    def rename(src: Any, dst: Any) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(fsops.os, "rename", rename)
    assert fsops.restore_orphaned_old(base / "t") == "failed"
    assert (base / ".t.old-1-2").is_dir()


def test_state_mode_is_configurable(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "src").mkdir()
    (base / "src" / "a.md").write_text("a", "utf-8")
    path = base / "p.json"
    path.write_text(
        json.dumps(
            {
                "pipeline": {"state_dir": f"{base}/state", "state_mode": "0750"},
                "stages": {
                    "example": {
                        "factory": "memfw_pipeline.example:factory",
                        "target": f"{base}/out/t",
                        "trust_paths": False,
                        "options": {"source": f"{base}/src"},
                    }
                },
            }
        ),
        "utf-8",
    )
    spec = load_config(path).stage("example")
    assert run_stage(spec)[0] == 0
    assert stat.S_IMODE(spec.state_dir.stat().st_mode) == 0o750
    assert stat.S_IMODE(spec.status_path.stat().st_mode) == 0o640
    path.write_text(path.read_text("utf-8").replace('"0750"', '"0777"'), "utf-8")
    with pytest.raises(SetupError) as info:
        load_config(path)
    assert info.value.code == "state_mode_invalid"


def test_overlap_check_resolves_symlinks(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    (base / "real").mkdir()
    (base / "alias").symlink_to(base / "real")
    config = {
        "pipeline": {"state_dir": f"{base}/state"},
        "stages": {
            "a": {"factory": "m:f", "target": f"{base}/real/t"},
            "b": {"factory": "m:f", "target": f"{base}/alias/t"},
        },
    }
    path = base / "p.json"
    path.write_text(json.dumps(config), "utf-8")
    with pytest.raises(SetupError) as info:
        load_config(path)
    assert info.value.code == "target_invalid"


def test_lock_lives_beside_the_target() -> None:
    assert _spec().lock_path == Path("/srv/memfw/derived/.view.lock")
