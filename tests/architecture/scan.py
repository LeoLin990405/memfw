"""AST import scanner for the architecture guards.

Everything here is static: files are parsed, never imported. Every import in a
file counts, including ones inside functions and under ``TYPE_CHECKING``,
because a lazy import is still a dependency.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PACKAGE = "memfw"
ESCAPES = "<escapes-package>"
#: Pseudo-unit for library code that reaches into the repository's ``scripts/``.
SCRIPTS = "<scripts>"
#: Script modules that are not on ``sys.path`` but get loaded by file path.
SCRIPT_MODULES = ("memfw_readonly_service",)


@dataclass(frozen=True, slots=True)
class Import:
    """One dependency of a module: an import, or a file-path load of ``scripts/``."""

    source: str
    target: str
    line: int


def discover_modules(src: Path = SRC, package: str = PACKAGE) -> dict[str, Path]:
    """Dotted module name -> file, for every ``.py`` under ``src/<package>``."""

    modules: dict[str, Path] = {}
    for path in sorted((src / package).rglob("*.py")):
        parts = list(path.relative_to(src).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _package_of(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _resolve_from(node: ast.ImportFrom, package: str) -> str | None:
    if node.level == 0:
        return node.module or ""
    parts = package.split(".")
    if node.level - 1 >= len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _docstring_nodes(tree: ast.Module) -> set[int]:
    nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                nodes.add(id(body[0].value))
    return nodes


def scan_file(path: Path, module: str, known: Iterable[str] = ()) -> list[Import]:
    """Every dependency of one file.

    Relative imports are resolved against the file's package. ``from X import
    a`` targets ``X.a`` when that is a known module, else ``X``. Library code
    that names a ``scripts/`` module or the ``scripts`` directory in a string
    (a file-path load) yields a ``<scripts>`` dependency; docstrings and
    comments do not count.
    """

    known_set = set(known)
    package = _package_of(module, path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[Import] = []
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(Import(module, alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, package)
            if base is None:
                found.append(Import(module, ESCAPES, node.lineno))
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                target = candidate if candidate in known_set else base
                found.append(Import(module, target, node.lineno))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and (node.value == "scripts" or any(name in node.value for name in SCRIPT_MODULES))
        ):
            found.append(Import(module, SCRIPTS, node.lineno))
    script_imports = [
        Import(module, SCRIPTS, item.line)
        for item in found
        if item.target.split(".")[0] in {"scripts", *SCRIPT_MODULES}
    ]
    return found + script_imports


def scan_tree(src: Path = SRC, package: str = PACKAGE) -> list[Import]:
    """Every dependency of every module under ``src/<package>``."""

    modules = discover_modules(src, package)
    found: list[Import] = []
    for module, path in modules.items():
        found.extend(scan_file(path, module, modules))
    return found


def unit_of(module: str, units: Mapping[str, object]) -> str | None:
    """The longest key of ``units`` that is ``module`` or a dotted prefix of it."""

    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in units:
            return candidate
    return None


def strongly_connected(graph: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan's SCCs with more than one node, each sorted, in sorted order."""

    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = 0

    def visit(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    nodes = set(graph) | {target for targets in graph.values() for target in targets}
    for node in sorted(nodes):
        if node not in index:
            visit(node)
    return sorted(result)
