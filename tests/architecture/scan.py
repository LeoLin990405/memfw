"""AST import scanner for the architecture guards.

Everything here is static: files are parsed, never imported. A module depends
on another when it

* imports it (``import`` / ``from ... import``, at any depth, including inside
  functions and under ``TYPE_CHECKING``; relative imports are resolved);
* imports a name a package ``__init__`` re-exports: ``from memfw import
  RelatedIndex`` depends on ``memfw.related``, the defining module, so the
  root package cannot launder an upward import;
* holds a string constant naming a memfw module (``"memfw.cli"`` or
  ``"memfw.cli:app"``), the usual input of ``importlib.import_module``,
  ``__import__``, ``pkgutil.resolve_name`` or entry-point style factories.

Two pseudo-targets mark what the AST cannot resolve:

* ``<scripts>``: a heuristic for library code reaching into the repository's
  ``scripts/`` directory: an import of ``scripts``/a script module, or a
  non-docstring string constant with ``scripts`` as a path component
  (``"scripts"``, ``"scripts/x.py"``, ``"../../scripts"``) or naming a
  script module (``memfw_readonly_service``).
* ``<dynamic-import>``: a loader call (``importlib.import_module``,
  ``__import__``, ``importlib.util.spec_from_file_location``,
  ``pkgutil.resolve_name``) whose target is not a string constant (or a module-level name bound to
  one).

Not seen: names built at run time from pieces, entry points loaded through
``importlib.metadata``, and attribute access on a bare ``import memfw``
binding (``memfw.X``); bare ``import memfw`` is reported as
``<bare-root-import>`` for that reason.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PACKAGE = "memfw"
ESCAPES = "<escapes-package>"
#: Pseudo-unit for library code that reaches into the repository's ``scripts/``.
SCRIPTS = "<scripts>"
#: Pseudo-unit for a loader call whose target the AST cannot read.
DYNAMIC = "<dynamic-import>"
#: Pseudo-unit for ``import memfw`` (attribute access on it is not resolved).
BARE_ROOT = "<bare-root-import>"
#: Script modules that are not on ``sys.path`` but get loaded by file path.
SCRIPT_MODULES = ("memfw_readonly_service",)
#: Call names (last dotted component) that load a module from a string or path.
LOADERS = frozenset({"import_module", "__import__", "spec_from_file_location", "resolve_name"})
MODULE_STRING = re.compile(r"^memfw(\.\w+)+(:\w+)?$")
_PATH_PARTS = re.compile(r"[\\/]")


@dataclass(frozen=True, slots=True)
class Import:
    """One dependency of a module (see the module docstring for the kinds)."""

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


def _mentions_scripts(value: str) -> bool:
    if any(name in value for name in SCRIPT_MODULES):
        return True
    return "scripts" in _PATH_PARTS.split(value)


def _deepest_module(name: str, known: set[str]) -> str | None:
    """The longest known module that is ``name`` or a dotted prefix of it (not the root)."""

    parts = name.split(".")
    for end in range(len(parts), 1, -1):
        candidate = ".".join(parts[:end])
        if candidate in known:
            return candidate
    return None


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def reexports(modules: Mapping[str, Path], package: str = PACKAGE) -> dict[str, dict[str, str]]:
    """Package -> {name it re-exports from a submodule -> defining module}.

    Only module-level ``from ... import`` statements in a package ``__init__``
    count. Chains (a package re-exporting another package's re-export) are
    followed by ``scan_file``.
    """

    known = set(modules)
    result: dict[str, dict[str, str]] = {}
    for module, path in modules.items():
        if path.name != "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            base = _resolve_from(node, module)
            if not base or base.split(".")[0] != package:
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                target = candidate if candidate in known else base
                if target != module:
                    names[alias.asname or alias.name] = target
        if names:
            result[module] = names
    return result


def _through_reexports(base: str, name: str, exported: Mapping[str, Mapping[str, str]]) -> str:
    target = base
    seen: set[tuple[str, str]] = set()
    while name in exported.get(target, {}) and (target, name) not in seen:
        seen.add((target, name))
        target = exported[target][name]
    return target


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "text"`` bindings (so ``import_module(NAME)`` resolves)."""

    constants: dict[str, str] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = value.value
    return constants


def scan_file(
    path: Path,
    module: str,
    known: Iterable[str] = (),
    exported: Mapping[str, Mapping[str, str]] | None = None,
) -> list[Import]:
    """Every dependency of one file (see the module docstring)."""

    known_set = set(known)
    exported = exported or {}
    package = _package_of(module, path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[Import] = []
    docstrings = _docstring_nodes(tree)
    constants = _module_constants(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE and module != PACKAGE:
                    found.append(Import(module, BARE_ROOT, node.lineno))
                found.append(Import(module, alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(node, package)
            if base is None:
                found.append(Import(module, ESCAPES, node.lineno))
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in known_set:
                    target = candidate
                else:
                    target = _through_reexports(base, alias.name, exported)
                found.append(Import(module, target, node.lineno))
        elif isinstance(node, ast.Call) and _call_name(node.func) in LOADERS:
            literal = [
                arg.value if isinstance(arg, ast.Constant) else constants[arg.id]
                for arg in node.args[:2]
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str))
                or (isinstance(arg, ast.Name) and arg.id in constants)
            ]
            name = _call_name(node.func)
            dynamic = len(literal) < 2 if name == "spec_from_file_location" else not literal
            if dynamic:
                found.append(Import(module, DYNAMIC, node.lineno))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            value = node.value
            if _mentions_scripts(value):
                found.append(Import(module, SCRIPTS, node.lineno))
            if MODULE_STRING.fullmatch(value):
                named = _deepest_module(value.partition(":")[0], known_set)
                if named is not None:
                    found.append(Import(module, named, node.lineno))
    script_imports = [
        Import(module, SCRIPTS, item.line)
        for item in found
        if item.target.split(".")[0] in {"scripts", *SCRIPT_MODULES}
    ]
    return found + script_imports


def scan_tree(src: Path = SRC, package: str = PACKAGE) -> list[Import]:
    """Every dependency of every module under ``src/<package>``."""

    modules = discover_modules(src, package)
    exported = reexports(modules, package)
    found: list[Import] = []
    for module, path in modules.items():
        found.extend(scan_file(path, module, modules, exported))
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
