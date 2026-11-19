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
  ``__import__``, ``pkgutil.resolve_name`` or entry-point style factories (pairs in
  ``NOT_MODULE_NAMES`` are exempt);
* names a memfw module through a loader call: ``import_module(".x",
  "memfw")``, ``__import__("memfw", fromlist=["x"])``, or
  ``spec_from_file_location(name, "src/memfw/x.py")``.

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
#: (module, string) pairs that look like module names but are not. The
#: entry-point group stays a plain name even if ``memfw/adapters/`` appears.
NOT_MODULE_NAMES = frozenset({("memfw.ports.registry", "memfw.adapters")})
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


def _reexport_nodes(tree: ast.Module) -> list[ast.ImportFrom]:
    """Module-level ``from`` imports, and those directly under ``if TYPE_CHECKING:``."""

    out: list[ast.ImportFrom] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            out.append(node)
        elif isinstance(node, ast.If) and ast.unparse(node.test) in (
            "TYPE_CHECKING",
            "typing.TYPE_CHECKING",
        ):
            out.extend(child for child in node.body if isinstance(child, ast.ImportFrom))
    return out


def reexports(modules: Mapping[str, Path], package: str = PACKAGE) -> dict[str, dict[str, str]]:
    """Package -> {name it re-exports from a submodule -> defining module}.

    Only module-level ``from ... import`` statements in a package ``__init__``
    count, including those under a module-level ``if TYPE_CHECKING:`` (the
    names a lazy ``__getattr__`` re-exports). Chains (a package re-exporting
    another package's re-export) are followed by ``scan_file``.
    """

    known = set(modules)
    result: dict[str, dict[str, str]] = {}
    for module, path in modules.items():
        if path.name != "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: dict[str, str] = {}
        for node in _reexport_nodes(tree):
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


def _literal(node: ast.expr, constants: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _module_from_path(text: str, known: set[str]) -> str | None:
    """``"src/memfw/cli.py"`` -> ``memfw.cli`` (the last ``memfw`` path component wins)."""

    parts = [part for part in _PATH_PARTS.split(text) if part and part != "."]
    if PACKAGE not in parts or not parts[-1].endswith(".py"):
        return None
    tail = parts[len(parts) - 1 - parts[::-1].index(PACKAGE) :]
    tail[-1] = tail[-1][: -len(".py")]
    if tail[-1] == "__init__":
        tail = tail[:-1]
    return _deepest_module(".".join(tail), known) if len(tail) > 1 else None


def _loader_targets(
    name: str,
    node: ast.Call,
    literal: list[str | None],
    constants: Mapping[str, str],
    known: set[str],
) -> list[str]:
    """memfw modules a loader call names in forms the plain string rule misses."""

    found: list[str] = []
    first = literal[0] if literal else None
    if name == "import_module" and first and first.startswith("."):
        package = literal[1] if len(literal) > 1 else None
        for keyword in node.keywords:
            if keyword.arg == "package":
                package = _literal(keyword.value, constants)
        if package:
            level = len(first) - len(first.lstrip("."))
            base = package.split(".")
            if level - 1 < len(base):
                base = base[: len(base) - (level - 1)]
                rest = first[level:]
                resolved = ".".join([*base, rest] if rest else base)
                if (target := _deepest_module(resolved, known)) is not None:
                    found.append(target)
    if name == "__import__" and first:
        fromlist: ast.expr | None = node.args[3] if len(node.args) > 3 else None
        for keyword in node.keywords:
            if keyword.arg == "fromlist":
                fromlist = keyword.value
        if isinstance(fromlist, ast.List | ast.Tuple):
            for element in fromlist.elts:
                item = _literal(element, constants)
                if item and (target := _deepest_module(f"{first}.{item}", known)):
                    found.append(target)
    if name == "spec_from_file_location":
        for text in literal[1:]:
            if text and (target := _module_from_path(text, known)) is not None:
                found.append(target)
    return found


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
    # A package ``__init__``'s ``if TYPE_CHECKING:`` re-exports declare what its
    # lazy ``__getattr__`` returns; the runtime edge is that function's import.
    typing_only = (
        {id(node) for node in _reexport_nodes(tree)} - {id(node) for node in tree.body}
        if path.name == "__init__.py"
        else set()
    )
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
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
            name = _call_name(node.func)
            literal = [_literal(arg, constants) for arg in node.args[:2]]
            texts = [value for value in literal if value is not None]
            if name == "spec_from_file_location":
                dynamic = len(texts) < 2
            else:
                dynamic = not literal or literal[0] is None
            if dynamic:
                found.append(Import(module, DYNAMIC, node.lineno))
            for target in _loader_targets(name, node, literal, constants, known_set):
                found.append(Import(module, target, node.lineno))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            value = node.value
            if _mentions_scripts(value):
                found.append(Import(module, SCRIPTS, node.lineno))
            if MODULE_STRING.fullmatch(value) and (module, value) not in NOT_MODULE_NAMES:
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
