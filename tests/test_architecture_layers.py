"""Import-layer guard for every ``memfw`` module (memfw-architecture section 7, A0).

The layer map is ``tests/architecture/layers.py``; today's known violations are
``tests/architecture/allowlist.py``. The allowlist can only shrink: a new
violation fails, and so does an entry whose edge no longer exists.
"""

from __future__ import annotations

import functools
import re
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from tests.architecture import allowlist, layers, scan

#: ``len(allowlist.ALLOWLIST)``. Lower it when an entry is removed; never raise it.
ALLOWLIST_SIZE = 26

STEP = re.compile(r"^A[1-5]\b")


@functools.cache
def _imports() -> tuple[scan.Import, ...]:
    return tuple(scan.scan_tree())


@functools.cache
def _edges() -> dict[tuple[str, str], list[scan.Import]]:
    """Unit-level edges (source unit, target unit) -> the imports behind them."""

    edges: dict[tuple[str, str], list[scan.Import]] = defaultdict(list)
    for item in _imports():
        source = scan.unit_of(item.source, layers.UNITS)
        assert source is not None, f"{item.source} is not in the layer map"
        if item.target in {scan.SCRIPTS, scan.ESCAPES}:
            edges[(source, item.target)].append(item)
            continue
        if item.target.split(".")[0] != scan.PACKAGE:
            continue
        target = scan.unit_of(item.target, layers.UNITS)
        assert target is not None, f"{item.source} imports unmapped {item.target}"
        if target != source:
            edges[(source, target)].append(item)
    return dict(edges)


def _kind(source: str, target: str) -> str | None:
    """Why an edge breaks the rules, or ``None`` when it is allowed."""

    if target == scan.SCRIPTS:
        return "imports scripts/"
    if target == scan.ESCAPES:
        return "relative import escapes the package"
    restricted = layers.RESTRICTED.get(source)
    if restricted is not None and target not in restricted:
        return f"{source} may import only {sorted(restricted)}"
    low = layers.RANK[layers.UNITS[source]]
    high = layers.RANK[layers.UNITS[target]]
    if high > low:
        return "upward import"
    if high == low and (source, target) not in layers.SAME_LAYER_ALLOWED:
        return "same-layer import not in SAME_LAYER_ALLOWED"
    return None


@functools.cache
def _violations() -> dict[tuple[str, str], str]:
    return {edge: kind for edge in _edges() if (kind := _kind(*edge)) is not None}


def _describe(edge: tuple[str, str], edges: dict[tuple[str, str], list[scan.Import]]) -> str:
    sites = sorted({f"{item.source}:{item.line}" for item in edges.get(edge, [])})
    shown = ", ".join(sites[:3]) + (" ..." if len(sites) > 3 else "")
    return f"{edge[0]} -> {edge[1]} ({shown})"


# --- the map itself ----------------------------------------------------------


def test_every_module_is_classified() -> None:
    """A new top-level module or subpackage must be placed in a layer."""

    modules = scan.discover_modules()
    top = {".".join(name.split(".")[:2]) for name in modules}
    missing = sorted(top - set(layers.UNITS))
    assert not missing, f"add these to tests/architecture/layers.py UNITS: {missing}"


def test_every_unit_exists_and_has_a_known_layer() -> None:
    modules = scan.discover_modules()
    stale = sorted(
        unit
        for unit in layers.UNITS
        if not any(name == unit or name.startswith(unit + ".") for name in modules)
    )
    assert not stale, f"layer map names modules that do not exist: {stale}"
    unknown = {unit: layer for unit, layer in layers.UNITS.items() if layer not in layers.RANK}
    assert not unknown, unknown


def test_cli_modules_are_interface_layer() -> None:
    """Every Typer sub-app module sits in L5, so a new one cannot hide in L3."""

    for name in scan.discover_modules():
        leaf = name.rpartition(".")[2]
        if leaf == "cli" or leaf.endswith("_cli"):
            unit = scan.unit_of(name, layers.UNITS)
            assert unit is not None
            assert layers.UNITS[unit] == "L5-interface", f"{name} maps to {unit}"


def test_same_layer_allowances_are_same_layer_acyclic_and_used() -> None:
    edges = _edges()
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in layers.SAME_LAYER_ALLOWED:
        assert layers.UNITS[source] == layers.UNITS[target], (source, target)
        assert layers.UNITS[source] != "L3-capability" or source == "memfw.recall.builtin", (
            "L3 capabilities talk through ports, not SAME_LAYER_ALLOWED",
            source,
            target,
        )
        assert (source, target) in edges, f"stale SAME_LAYER_ALLOWED entry: {source} -> {target}"
        graph[source].add(target)
    assert not scan.strongly_connected(graph)


# --- the rules ---------------------------------------------------------------


def test_no_violation_outside_the_allowlist() -> None:
    edges = _edges()
    new = sorted(set(_violations()) - set(allowlist.ALLOWLIST))
    assert not new, "new architecture violations (fix them; do not allowlist):\n" + "\n".join(
        f"  {_describe(edge, edges)}: {_violations()[edge]}" for edge in new
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Fixed violations must be deleted from the allowlist, so it only shrinks."""

    stale = sorted(set(allowlist.ALLOWLIST) - set(_violations()))
    assert not stale, "delete these fixed entries from tests/architecture/allowlist.py:\n" + (
        "\n".join(f"  {source} -> {target}" for source, target in stale)
    )


def test_allowlist_entries_name_their_step_and_never_grow() -> None:
    for edge, reason in allowlist.ALLOWLIST.items():
        assert STEP.match(reason), f"{edge}: reason must start with the step (A1..A5): {reason!r}"
    assert len(allowlist.ALLOWLIST) <= ALLOWLIST_SIZE, (
        "the allowlist only shrinks; fix the new violation instead of listing it"
    )
    assert len(allowlist.ALLOWLIST) == ALLOWLIST_SIZE, (
        f"allowlist shrank to {len(allowlist.ALLOWLIST)}: lower ALLOWLIST_SIZE to match"
    )


def test_no_cycles_once_allowlisted_edges_are_removed() -> None:
    """Every package cycle today runs through an allowlisted edge; no new ones."""

    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in _edges():
        if target.startswith("<") or (source, target) in allowlist.ALLOWLIST:
            continue
        graph[source].add(target)
    assert not scan.strongly_connected(graph)


def test_ports_import_only_stdlib() -> None:
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    for item in _imports():
        if scan.unit_of(item.source, layers.UNITS) != "memfw.ports":
            continue
        if item.target.startswith("memfw.ports"):
            continue
        assert item.target.split(".")[0] in stdlib, f"{item.source}:{item.line} -> {item.target}"


# --- the scanner -------------------------------------------------------------


def test_scanner_resolves_relative_from_imports_and_scripts(tmp_path: Path) -> None:
    root = tmp_path / "src"
    package = root / "pkg"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("", encoding="utf-8")
    (package / "sub" / "__init__.py").write_text(
        "from .. import a\nfrom ..a import thing\n", encoding="utf-8"
    )
    (package / "sub" / "b.py").write_text(
        '"""Docstring naming scripts/ and memfw_readonly_service does not count."""\n'
        "def f():\n"
        "    from . import c\n"
        "    from ...outside import x\n"
        "    import os\n"
        "    return Path('x') / 'scripts' / 'memfw_readonly_service.py'\n",
        encoding="utf-8",
    )
    found = scan.scan_tree(root, "pkg")
    pairs = {(item.source, item.target) for item in found}
    assert ("pkg.sub", "pkg.a") in pairs
    assert ("pkg.sub.b", "pkg.sub") in pairs  # ``from . import c``: c is not a module
    assert ("pkg.sub.b", scan.ESCAPES) in pairs
    assert ("pkg.sub.b", "os") in pairs
    assert [item.line for item in found if item.target == scan.SCRIPTS] == [6, 6]


def test_unit_of_prefers_the_longest_key() -> None:
    units = {"memfw.core": 1, "memfw.core.repository": 2}
    assert scan.unit_of("memfw.core.models", units) == "memfw.core"
    assert scan.unit_of("memfw.core.repository", units) == "memfw.core.repository"
    assert scan.unit_of("memfw.corex", units) is None


@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        ({"a": {"b"}, "b": {"c"}}, []),
        ({"a": {"b"}, "b": {"a"}, "c": {"a"}}, [["a", "b"]]),
    ],
)
def test_strongly_connected(graph: dict[str, set[str]], expected: list[list[str]]) -> None:
    assert scan.strongly_connected(graph) == expected
