"""``memfw-ports`` and ``memfw-recall`` as standalone wheels (A5b).

memfw-architecture section 9: both "can be installed and tested as standalone
wheels without memfw". ``packages/memfw-ports`` and ``packages/memfw-recall``
build those wheels from ``src/memfw/ports`` and ``src/memfw/recall`` under the
import names ``memfw_ports`` and ``memfw_recall`` (docs/ports-and-recall.md
section 1.1).

The static checks always run. The build + clean-venv install + smoke run is
opt-in (``MEMFW_STANDALONE_WHEELS=1``) and needs ``uv``: it builds both wheels
into a temporary directory, installs them into a fresh venv without memfw and
runs ``tests/standalone/wheel_smoke.py`` there.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "memfw"
PACKAGES = ROOT / "packages"
SMOKE = ROOT / "tests" / "standalone" / "wheel_smoke.py"
#: memfw-internal: reaches the vault, repository and retrievers.
RECALL_EXCLUDED = {"builtin.py"}


def _force_include(package: str) -> dict[str, str]:
    data = tomllib.loads((PACKAGES / package / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel.get("bypass-selection") is True, package  # only what force-include names
    return dict(wheel["force-include"])


def test_ports_wheel_maps_the_whole_ports_tree() -> None:
    mapping = _force_include("memfw-ports")
    assert mapping["../../src/memfw/ports"] == "memfw_ports"
    assert set(mapping) == {"../../src/memfw/ports", "py.typed"}


def test_recall_wheel_lists_every_recall_module_but_builtin() -> None:
    mapping = _force_include("memfw-recall")
    modules = {
        source.removeprefix("../../src/memfw/recall/"): target
        for source, target in mapping.items()
        if source.startswith("../../src/memfw/recall/")
    }
    expected = {p.name for p in (SRC / "recall").glob("*.py")} - RECALL_EXCLUDED
    assert set(modules) == expected  # a new recall module must be added (or excluded) here
    assert all(target == f"memfw_recall/{name}" for name, target in modules.items())
    assert not RECALL_EXCLUDED & set(modules)
    data = tomllib.loads((PACKAGES / "memfw-recall" / "pyproject.toml").read_text("utf-8"))
    ports = tomllib.loads((PACKAGES / "memfw-ports" / "pyproject.toml").read_text("utf-8"))
    pin = f"memfw-ports=={ports['project']['version']}"
    assert data["project"]["dependencies"] == [pin]


def test_recall_has_no_subpackage_the_wheel_could_miss() -> None:
    """The recall wheel lists top-level files; a subpackage would silently be left out."""

    nested = [
        path.relative_to(SRC / "recall").as_posix()
        for path in (SRC / "recall").rglob("*.py")
        if path.parent != SRC / "recall"
    ]
    assert nested == [], "add the subpackage to packages/memfw-recall/pyproject.toml"


def test_wheel_versions_follow_the_ports_api_version() -> None:
    from memfw.ports import PORTS_API_VERSION

    for package in ("memfw-ports", "memfw-recall"):
        data = tomllib.loads((PACKAGES / package / "pyproject.toml").read_text("utf-8"))
        assert data["project"]["version"] == PORTS_API_VERSION, package


#: Workspace members: the two wheels built from memfw's tree, then the adapters.
MEMBERS = (
    "memfw-ports",
    "memfw-recall",
    "adapters/memfw-adapter-deepseek",
    "adapters/memfw-adapter-bm25s",
    "adapters/memfw-adapter-qdrant",
    "adapters/memfw-adapter-qmd",
    "adapters/memfw-adapter-ocr",
)
#: Third-party wheels an adapter needs, by exact pin (the versions uv.lock
#: resolves and the adapter's pyproject pins), installed before the local
#: wheels (which are then installed with ``--no-index``).
ADAPTER_DEPENDENCIES = (
    "bm25s==0.3.16",
    "jieba==0.42.1",
    "PyYAML==6.0.3",
    "numpy==2.4.6" if sys.version_info < (3, 12) else "numpy==2.5.5",
)


def _install_pinned(uv: str, python: Path) -> None:
    """The pinned dependencies from uv's cache when it has them, else from the index."""

    base = (uv, "pip", "install", "--quiet", "--python", python)
    offline = subprocess.run(
        [str(a) for a in (*base, "--offline", *ADAPTER_DEPENDENCIES)],
        capture_output=True,
        text=True,
        check=False,
    )
    if offline.returncode != 0:
        _run(*base, *ADAPTER_DEPENDENCIES)


def test_members_pin_their_build_backend_and_ship_license_and_notice() -> None:
    for package in MEMBERS:
        data = tomllib.loads((PACKAGES / package / "pyproject.toml").read_text("utf-8"))
        assert data["build-system"]["requires"] == ["hatchling==1.33.0"], package
        assert data["project"]["license-files"] == ["LICENSE", "NOTICE"], package
        for name in ("LICENSE", "NOTICE"):
            link = PACKAGES / package / name
            assert link.is_symlink() and link.resolve() == (ROOT / name).resolve(), link


def test_workspace_lists_both_packages_and_memfw_does_not_depend_on_them() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = data["tool"]["uv"]["workspace"]["members"]
    assert members == [f"packages/{name}" for name in MEMBERS]
    deps = " ".join(data["project"]["dependencies"])
    assert "memfw-ports" not in deps and "memfw-recall" not in deps  # self-contained
    assert "memfw-adapter" not in deps  # memfw never depends on an adapter


def _seam_branches() -> tuple[list[ast.ImportFrom], list[ast.ImportFrom]]:
    tree = ast.parse((SRC / "recall" / "_ports.py").read_text(encoding="utf-8"))
    branch = next(node for node in tree.body if isinstance(node, ast.If))
    inside = [node for node in branch.body if isinstance(node, ast.ImportFrom)]
    outside = [node for node in branch.orelse if isinstance(node, ast.ImportFrom)]
    return inside, outside


def test_recall_seam_imports_the_same_names_under_both_roots() -> None:
    inside, outside = _seam_branches()
    assert inside and len(inside) == len(outside)
    for sibling, standalone in zip(inside, outside, strict=True):
        assert sibling.level == 2 and sibling.module and sibling.module.startswith("ports.")
        assert standalone.level == 0 and standalone.module == "memfw_" + sibling.module
        assert [a.name for a in sibling.names] == [a.name for a in standalone.names]


def _escapes(source: str, depth: int) -> list[str]:
    """Relative imports in ``source`` that climb above the wheel's top-level package.

    ``depth`` is how deep the file's package sits below that top level: 0 for
    ``memfw_ports/x.py`` and ``memfw_recall/x.py``, 1 for
    ``memfw_ports/reference/x.py``. Inside memfw the same climb would reach
    ``memfw`` itself, which the wheel does not contain.
    """

    return [
        "." * node.level + (node.module or "")
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.level > depth + 1
    ]


def test_escape_check_catches_a_climb_out_of_the_wheel() -> None:
    """Mutation test for the check below."""

    assert _escapes("from ..ports import types\n", 0) == ["..ports"]
    assert _escapes("def f():\n    from ..core import x\n", 0) == ["..core"]
    assert _escapes("from ...core import x\n", 1) == ["...core"]
    assert _escapes("from ..types import x\nfrom . import y\n", 1) == []
    assert _escapes("from .fusion import x\nimport os\n", 0) == []
    coordinator = (SRC / "recall" / "coordinator.py").read_text(encoding="utf-8")
    assert _escapes(coordinator, 0) == []
    mutated = coordinator.replace("from .fusion import", "from ..ports.fusion import", 1)
    assert _escapes(mutated, 0) == ["..ports.fusion"]


def test_shipped_files_never_import_above_their_wheel() -> None:
    files = [
        (p, len(p.relative_to(SRC / "ports").parts) - 1) for p in (SRC / "ports").rglob("*.py")
    ]
    files += [
        (p, 0)
        for p in (SRC / "recall").glob("*.py")
        # The seam's ``..ports`` branch runs only when recall has a parent package.
        if p.name not in RECALL_EXCLUDED | {"_ports.py"}
    ]
    assert len(files) > 20
    for path, depth in files:
        assert _escapes(path.read_text(encoding="utf-8"), depth) == [], path


def test_only_the_seam_names_the_standalone_ports() -> None:
    """Recall modules shipped in the wheel reach the ports only through ``_ports.py``."""

    for path in sorted((SRC / "recall").glob("*.py")):
        if path.name in RECALL_EXCLUDED or path.name == "_ports.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert not node.module.startswith(("memfw.", "memfw_")), (path.name, node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("memfw"), (path.name, alias.name)


def test_seam_reexports_memfw_ports_inside_memfw() -> None:
    import memfw.ports.protocols
    import memfw.ports.types
    from memfw.recall import _ports, coordinator

    for name in _ports.__all__:
        source = memfw.ports.protocols if name == "RecallProvider" else memfw.ports.types
        assert getattr(_ports, name) is getattr(source, name), name
        assert getattr(coordinator, name) is getattr(source, name), name


def _run(*args: str | Path, cwd: Path = ROOT) -> str:
    completed = subprocess.run(
        [str(a) for a in args], cwd=cwd, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
@pytest.mark.skipif(
    os.environ.get("MEMFW_STANDALONE_WHEELS") != "1",
    reason="opt in with MEMFW_STANDALONE_WHEELS=1 (builds wheels, creates a venv)",
)
def test_wheels_install_and_pass_the_contract_kit_without_memfw(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    dist = tmp_path / "dist"
    for package in (
        "memfw-ports",
        "memfw-recall",
        "memfw-adapter-deepseek",
        "memfw-adapter-bm25s",
        "memfw-adapter-qdrant",
        "memfw-adapter-qmd",
        "memfw-adapter-ocr",
    ):
        _run(uv, "build", "--wheel", "--package", package, "-o", dist)
    wheels = sorted(p.name for p in dist.glob("*.whl"))
    from memfw.ports import PORTS_API_VERSION as v

    assert wheels == [
        "memfw_adapter_bm25s-0.1.1-py3-none-any.whl",
        "memfw_adapter_deepseek-0.1.1-py3-none-any.whl",
        "memfw_adapter_ocr-0.1.0-py3-none-any.whl",
        "memfw_adapter_qdrant-0.1.1-py3-none-any.whl",
        "memfw_adapter_qmd-0.1.0-py3-none-any.whl",
        f"memfw_ports-{v}-py3-none-any.whl",
        f"memfw_recall-{v}-py3-none-any.whl",
    ]
    venv = tmp_path / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(uv, "venv", "--quiet", "--python", f"{sys.version_info[0]}.{sys.version_info[1]}", venv)
    _install_pinned(uv, python)
    _run(uv, "pip", "install", "--quiet", "--python", python, "--no-index", *dist.glob("*.whl"))
    # -I: no PYTHONPATH, no cwd or script directory on sys.path, so this tree's
    # src/ cannot leak in; the smoke refuses to run if memfw is importable.
    out = _run(
        python,
        "-I",
        SMOKE,
        "--recall",
        "--deepseek",
        "--bm25s",
        "--qdrant",
        "--qmd",
        "--ocr",
        cwd=tmp_path,
    )
    assert "standalone wheel smoke passed" in out
    assert "ok check_model_backend DeepSeekBackend (fake server)" in out
    assert "ok check_recall_provider QmdMemoryProvider + QmdNotesProvider (fake server)" in out
    assert "ok check_recall_provider InMemoryRecallProvider" in out
    assert "ok check_deriver OcrDeriver (fake engine)" in out
    assert "ok Coordinator.search" in out
    assert "ok check_recall_provider Bm25sProvider" in out
    assert "ok check_recall_provider QdrantProvider (fake server)" in out


def test_adapter_dependency_pins_match_the_lockfile_and_pyproject() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {(pkg["name"], pkg["version"]) for pkg in lock["package"]}
    for pin in ADAPTER_DEPENDENCIES:
        name, version = pin.split("==")
        assert (name.lower(), version) in locked, pin
    project = tomllib.loads(
        (PACKAGES / "adapters" / "memfw-adapter-bm25s" / "pyproject.toml").read_text("utf-8")
    )
    deps = project["project"]["dependencies"]
    assert all("==" in dep for dep in deps), deps  # every dependency pinned exactly
    for pin in ADAPTER_DEPENDENCIES:
        assert any(dep.split(";")[0].strip() == pin for dep in deps), pin


def test_ci_wheel_job_builds_and_smokes_every_adapter_with_the_pins() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for package in (
        "memfw-adapter-deepseek",
        "memfw-adapter-bm25s",
        "memfw-adapter-qdrant",
        "memfw-adapter-qmd",
        "memfw-adapter-ocr",
    ):
        assert f"uv build --wheel --package {package} " in ci, package
    assert "bm25s==0.3.16 jieba==0.42.1 PyYAML==6.0.3 numpy==2.4.6" in ci  # CI runs Python 3.11
    assert "wheel_smoke.py --recall --deepseek --bm25s --qdrant --qmd --ocr" in ci


def test_qmd_adapter_dependencies_are_pinned_and_installed_first() -> None:
    project = tomllib.loads(
        (PACKAGES / "adapters" / "memfw-adapter-qmd" / "pyproject.toml").read_text("utf-8")
    )
    deps = project["project"]["dependencies"]
    assert all("==" in dep for dep in deps), deps  # every dependency pinned exactly
    third_party = [dep for dep in deps if not dep.startswith("memfw-")]
    assert third_party == ["PyYAML==6.0.3"], deps
    assert all(pin in ADAPTER_DEPENDENCIES for pin in third_party), third_party
