"""Packaging boundaries: ``memfw.ports`` is stdlib-only; recall reaches internals in one module."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "memfw"


def _imports(path: Path, package: str) -> set[str]:
    """Every module a file imports, anywhere in it (functions included).

    Relative imports are resolved against ``package`` (e.g. ``memfw.ports``):
    ``from . import x`` → ``memfw.ports.x``, ``from ..core import y`` →
    ``memfw.core``.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    parts = package.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    names.add(node.module)
                continue
            base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
            if node.level - 1 >= len(parts):
                names.add("<escapes-package>")
                continue
            if node.module:
                names.add(".".join([*base, node.module]))
            else:
                names.update(".".join([*base, alias.name]) for alias in node.names)
    return names


def _absolute_imports(path: Path) -> set[str]:
    return _imports(path, f"memfw.{path.parent.name}")


def _imported_after(statement: str) -> list[str]:
    """memfw modules a statement imports beyond what ``import memfw`` already loads."""

    code = (
        "import json, sys\n"
        "import memfw\n"
        "before = {m for m in sys.modules if m == 'memfw' or m.startswith('memfw.')}\n"
        f"{statement}\n"
        "after = {m for m in sys.modules if m == 'memfw' or m.startswith('memfw.')}\n"
        "print(json.dumps(sorted(after - before)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=SRC.parents[1],
    )
    return list(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_ports_sources_import_only_stdlib() -> None:
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for path in sorted((SRC / "ports").glob("*.py")):
        for name in _absolute_imports(path):
            if name.startswith("memfw.ports"):
                continue
            assert name.split(".")[0] in stdlib, f"{path.name} imports {name}"


def test_scanner_resolves_relative_imports_inside_functions(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def f():\n"
        "    from ..core import repository\n"
        "    from . import types\n"
        "    from ...x import y\n"
        "    import os\n",
        encoding="utf-8",
    )
    names = _imports(sample, "memfw.ports")
    assert {"memfw.core", "memfw.ports.types", "os"} <= names
    assert "<escapes-package>" in names


def test_importing_ports_loads_no_other_memfw_module() -> None:
    added = _imported_after("import memfw.ports, memfw.ports.testing")
    assert added
    assert all(name.startswith("memfw.ports") for name in added), added


def test_recall_core_modules_use_only_ports_and_siblings() -> None:
    for path in sorted((SRC / "recall").glob("*.py")):
        if path.name == "builtin.py":
            continue
        for name in _absolute_imports(path):
            assert name != "<escapes-package>", path.name
            if name.split(".")[0] == "memfw":
                assert name.startswith(("memfw.ports", "memfw.recall")), f"{path.name}: {name}"
                assert name != "memfw.recall.builtin", f"{path.name} imports builtin"


def test_importing_recall_does_not_load_memfw_internals() -> None:
    added = _imported_after("import memfw.recall")
    allowed = ("memfw.ports", "memfw.recall")
    assert all(name.startswith(allowed) for name in added), added
    assert "memfw.recall.builtin" not in added
