"""Library code under ``src/memfw`` never depends on ``scripts/``.

Scripts are entry points: they may import the library, never the reverse
(architecture rule 4). A wheel need not ship ``scripts/``, and a library
that loads a script's private helpers breaks whenever the script changes.
This scans the source with ``ast``, so nothing here is imported or run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "memfw"
FORBIDDEN_MODULES = ("scripts", "memfw_readonly_service")
#: Loaders that are fine for library files but must never name scripts.
PATH_LOADERS = frozenset({"spec_from_file_location", "import_module", "__import__"})
#: Calls a library module has no reason to make at all.
ALWAYS_FLAGGED = frozenset({"exec", "addsitedir", "SourceFileLoader", "run_path"})
PATH_MUTATORS = frozenset({"insert", "append", "extend", "remove", "clear", "pop"})


def _forbidden_module(name: str) -> bool:
    return any(part in FORBIDDEN_MODULES for part in name.split("."))


def _forbidden_text(text: str) -> bool:
    return text == "scripts" or "scripts/" in text or "memfw_readonly_service" in text


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _mentions_scripts(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and (_forbidden_text(child.value) or "scripts" in child.value)
        for child in ast.walk(node)
    )


class _Aliases:
    """Names bound to the ``sys`` module and to ``sys.path`` in one file."""

    def __init__(self, tree: ast.AST) -> None:
        self.sys = {"sys"}
        self.path: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sys":
                        self.sys.add(alias.asname or "sys")
            elif isinstance(node, ast.ImportFrom) and node.module == "sys":
                for alias in node.names:
                    if alias.name == "path":
                        self.path.add(alias.asname or "path")

    def is_sys_path(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Subscript):
            node = node.value
        if isinstance(node, ast.Name):
            return node.id in self.path
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.sys
        )


def _violations_in(source: str, filename: str = "<sample>") -> list[str]:
    tree = ast.parse(source, filename=filename)
    aliases = _Aliases(tree)
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
                f"{line}: import {alias.name}"
                for alias in node.names
                if _forbidden_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _forbidden_module(module) or any(
                _forbidden_module(alias.name) for alias in node.names
            ):
                found.append(f"{line}: from {module} import ...")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in ALWAYS_FLAGGED:
                found.append(f"{line}: {name}(...)")
            elif name in PATH_LOADERS and _mentions_scripts(node):
                found.append(f"{line}: {name}(...) names scripts")
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in PATH_MUTATORS
                and aliases.is_sys_path(func.value)
            ):
                found.append(f"{line}: sys.path.{func.attr}(...)")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(aliases.is_sys_path(target) for target in targets):
                found.append(f"{line}: assigns sys.path")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and _forbidden_text(node.value):
                found.append(f"{line}: names scripts")
    return found


def test_library_never_imports_scripts() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 100
    problems = {
        str(path.relative_to(SRC)): found
        for path in files
        if (found := _violations_in(path.read_text(encoding="utf-8"), str(path)))
    }
    assert problems == {}


CAUGHT = {
    "import": "import scripts.memfw_readonly_service as service\n",
    "from-import": "from scripts import memfw_readonly_service\n",
    "from-module": "from scripts.memfw_readonly_service import search_vault\n",
    "spec": "import importlib.util\nimportlib.util.spec_from_file_location('x', 'scripts/x.py')\n",
    "import_module": "import importlib\nimportlib.import_module('scripts.thing')\n",
    "string-scripts": "from pathlib import Path\nchecked = Path('.') / 'scripts' / 'x.py'\n",
    "string-slash": "where = 'scripts/launch.py'\n",
    "string-service": "name = 'memfw_readonly_service'\n",
    "sys.path.insert": "import sys\nsys.path.insert(0, 'elsewhere')\n",
    "sys.path.append": "import sys\nsys.path.append('elsewhere')\n",
    "sys.path +=": "import sys\nsys.path += ['elsewhere']\n",
    "sys.path[:]": "import sys\nsys.path[:] = ['elsewhere']\n",
    "sys alias": "import sys as system\nsystem.path.insert(0, 'elsewhere')\n",
    "from sys import path": "from sys import path\npath.append('elsewhere')\n",
    "from sys import path as": "from sys import path as where\nwhere += ['elsewhere']\n",
    "exec": "exec('x = 1')\n",
    "builtins.exec": "import builtins\nbuiltins.exec('x = 1')\n",
    "site.addsitedir": "import site\nsite.addsitedir('elsewhere')\n",
    "from site import addsitedir": "from site import addsitedir\naddsitedir('elsewhere')\n",
    "SourceFileLoader": (
        "from importlib.machinery import SourceFileLoader\nSourceFileLoader('x', 'y.py')\n"
    ),
    "runpy.run_path": "import runpy\nrunpy.run_path('y.py')\n",
}


@pytest.mark.parametrize("source", CAUGHT.values(), ids=list(CAUGHT))
def test_the_guard_catches_each_pattern(source: str) -> None:
    assert _violations_in(source), source


def test_the_guard_allows_ordinary_library_code() -> None:
    source = (
        '"""Mentions ``scripts/memfw_readonly_service.py`` in a docstring only."""\n'
        "import importlib.util\n"
        "import sys\n"
        "from pathlib import Path\n"
        "spec = importlib.util.spec_from_file_location('memfw_read_path', 'read_path.py')\n"
        "module = sys.modules[__name__]\n"
        "transcripts = 'agent transcripts'\n"
        "if spec is not None and spec.loader is not None:\n"
        "    spec.loader.exec_module(importlib.util.module_from_spec(spec))\n"
    )
    assert _violations_in(source) == []
