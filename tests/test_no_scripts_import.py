"""Library code under ``src/memfw`` never depends on ``scripts/``.

Scripts are entry points: they may import the library, never the reverse
(architecture rule 4). A wheel need not ship ``scripts/``, and a library
that loads a script's private helpers breaks whenever the script changes.
This scans the source with ``ast``, so nothing here is imported or run.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "memfw"
FORBIDDEN_MODULES = ("scripts", "memfw_readonly_service")
#: Calls that load code from a path or change where imports are found.
LOADERS = frozenset(
    {
        "spec_from_file_location",
        "import_module",
        "__import__",
        "run_path",
        "SourceFileLoader",
    }
)


def _forbidden(name: str) -> bool:
    parts = name.split(".")
    return any(part in FORBIDDEN_MODULES for part in parts)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _mentions_scripts(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if "scripts" in child.value or "memfw_readonly_service" in child.value:
                return True
    return False


def _is_sys_path(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    # Docstrings may name the script; code that builds a path to it may not.
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            found.extend(
                f"{line}: import {alias.name}" for alias in node.names if _forbidden(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _forbidden(module) or any(_forbidden(alias.name) for alias in node.names):
                found.append(f"{line}: from {module} import ...")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in LOADERS and _mentions_scripts(node):
                found.append(f"{line}: {name}(...) names scripts")
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and _is_sys_path(func.value)
                and func.attr in {"insert", "append", "extend"}
            ):
                found.append(f"{line}: sys.path.{func.attr}(...)")
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                base = target.value if isinstance(target, ast.Subscript) else target
                if _is_sys_path(base):
                    found.append(f"{line}: assigns sys.path")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and "memfw_readonly_service" in node.value:
                found.append(f"{line}: names memfw_readonly_service")
    return found


def test_library_never_imports_scripts() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 100
    problems = {str(path.relative_to(SRC)): found for path in files if (found := _violations(path))}
    assert problems == {}


def test_the_guard_catches_each_pattern(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import sys\n"
        "import scripts.memfw_readonly_service as service\n"
        "from scripts import memfw_readonly_service\n"
        "from scripts.memfw_readonly_service import search_vault\n"
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('x', 'scripts/memfw_readonly_service.py')\n"
        "sys.path.insert(0, 'scripts')\n"
        "sys.path += ['elsewhere']\n"
        "from pathlib import Path\n"
        "checkout = Path(__file__).parents[2] / 'scripts' / 'memfw_readonly_service.py'\n",
        encoding="utf-8",
    )
    found = _violations(sample)
    assert len(found) >= 7, found
    assert any("sys.path.insert" in item for item in found)
    assert any("assigns sys.path" in item for item in found)
    assert any("spec_from_file_location" in item for item in found)
    assert any(item.startswith("10: names") for item in found), found
