"""Import-layer guard for every ``memfw`` module (memfw-architecture section 7, A0).

The layer map is ``tests/architecture/layers.py``; today's known violations are
``tests/architecture/allowlist.py``, each with its exact import sites. The
allowlist can only shrink: a new violation fails, a new import site on an
allowlisted edge fails, a fixed site or edge fails until the entry is updated,
and (when git history is available) the list may not be longer than at the
merge-base.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest

from tests.architecture import allowlist, layers, scan

#: ``len(allowlist.ALLOWLIST)``. Lower it when an entry is removed; never raise it.
ALLOWLIST_SIZE = 27

STEP = re.compile(r"^A[1-5]\b")
ALLOWLIST_PATH = "tests/architecture/allowlist.py"
#: Ref the merge-base is taken against (CI: fetch it, see docs/architecture.md).
BASE_REF = os.environ.get("MEMFW_ARCH_BASE_REF", "origin/main")

Edge = tuple[str, str]
Sites = dict[str, int]


@functools.cache
def _imports() -> tuple[scan.Import, ...]:
    return tuple(scan.scan_tree())


def _unit(module: str) -> str:
    # Every memfw module has a unit: "memfw" itself is a key (checked below).
    unit = scan.unit_of(module, layers.UNITS)
    return unit if unit is not None else scan.PACKAGE


def _edge_of(item: scan.Import) -> Edge | None:
    """The unit-level edge an import makes, or ``None`` (stdlib, third party, same unit)."""

    source = _unit(item.source)
    if item.target.startswith("<"):
        return (source, item.target)
    if item.target.split(".")[0] != scan.PACKAGE:
        return None
    target = _unit(item.target)
    return None if target == source else (source, target)


def _kind(item: scan.Import, edge: Edge) -> str | None:
    """Why an import breaks the rules, or ``None`` when it is allowed."""

    source, target = edge
    if target == scan.SCRIPTS:
        return "library code reaches into scripts/"
    if target == scan.ESCAPES:
        return "relative import escapes the package"
    if target == scan.BARE_ROOT:
        return "bare `import memfw`: import names from their defining module"
    if target == scan.DYNAMIC:
        allowed = item.source in layers.DYNAMIC_IMPORT_ALLOWED
        return None if allowed else "loader call on a name the scan cannot read"
    restricted = layers.RESTRICTED.get(source)
    if restricted is not None and target not in restricted:
        return f"{source} may import only {sorted(restricted)}"
    low = layers.RANK[layers.UNITS[source]]
    high = layers.RANK[layers.UNITS[target]]
    if high > low:
        return "upward import"
    if high == low and edge not in layers.SAME_LAYER_ALLOWED:
        return "same-layer import not in SAME_LAYER_ALLOWED"
    return None


def _sites(items: list[scan.Import]) -> Sites:
    """Importing module -> number of distinct lines."""

    lines: dict[str, set[int]] = defaultdict(set)
    for item in items:
        lines[item.source].add(item.line)
    return {module: len(found) for module, found in sorted(lines.items())}


def _violations(items: tuple[scan.Import, ...]) -> dict[Edge, tuple[str, Sites]]:
    grouped: dict[Edge, list[scan.Import]] = defaultdict(list)
    kinds: dict[Edge, str] = {}
    for item in items:
        edge = _edge_of(item)
        if edge is None:
            continue
        kind = _kind(item, edge)
        if kind is not None:
            grouped[edge].append(item)
            kinds[edge] = kind
    return {edge: (kinds[edge], _sites(found)) for edge, found in grouped.items()}


@functools.cache
def _current() -> dict[Edge, tuple[str, Sites]]:
    return _violations(_imports())


@functools.cache
def _edges() -> frozenset[Edge]:
    return frozenset(edge for item in _imports() if (edge := _edge_of(item)) is not None)


def _total_sites(entries: dict[Edge, object]) -> int | None:
    total = 0
    for value in entries.values():
        if not (isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict)):
            return None
        total += sum(value[1].values())
    return total


# --- the map itself ----------------------------------------------------------


def test_every_top_level_module_is_classified() -> None:
    """A new top-level module or subpackage must be placed in a layer.

    Nested modules inherit their package's unit (``memfw.capture.new`` belongs
    to ``memfw.capture``), except that every ``cli.py`` must be in L5.
    """

    assert scan.PACKAGE in layers.UNITS
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
            assert layers.UNITS[_unit(name)] == "L5-interface", f"{name} maps to {_unit(name)}"


def test_same_layer_allowances_are_same_layer_acyclic_and_used() -> None:
    graph: dict[str, set[str]] = defaultdict(set)
    for source, target in layers.SAME_LAYER_ALLOWED:
        assert layers.UNITS[source] == layers.UNITS[target], (source, target)
        assert layers.UNITS[source] != "L3-capability" or source == "memfw.recall.builtin", (
            "L3 capabilities talk through ports, not SAME_LAYER_ALLOWED",
            source,
            target,
        )
        assert (source, target) in _edges(), f"stale SAME_LAYER_ALLOWED: {source} -> {target}"
        graph[source].add(target)
    assert not scan.strongly_connected(graph)


def test_dynamic_import_allowances_match_their_sites() -> None:
    found = _sites([item for item in _imports() if item.target == scan.DYNAMIC])
    for module, (reason, count) in layers.DYNAMIC_IMPORT_ALLOWED.items():
        assert reason, module
        assert found.get(module) == count, (
            f"{module}: {found.get(module, 0)} dynamic loader sites, allowance says {count}"
        )


# --- the rules ---------------------------------------------------------------


def test_no_violation_outside_the_allowlist() -> None:
    current = _current()
    new = sorted(set(current) - set(allowlist.ALLOWLIST))
    assert not new, "new architecture violations (fix them; do not allowlist):\n" + "\n".join(
        f"  {source} -> {target}: {current[(source, target)][0]} {current[(source, target)][1]}"
        for source, target in new
    )


def test_allowlisted_edges_have_exactly_their_recorded_sites() -> None:
    """New import sites on an allowlisted edge fail; fixed ones must be removed from it."""

    problems: list[str] = []
    for edge, (_, recorded) in allowlist.ALLOWLIST.items():
        if edge not in _current():
            continue
        actual = _current()[edge][1]
        grown = {
            module: count for module, count in actual.items() if count > recorded.get(module, 0)
        }
        if grown:
            problems.append(f"  {edge}: new import sites {grown} (fix them; do not record them)")
        elif actual != recorded:
            problems.append(f"  {edge}: sites shrank to {actual}; update the entry to match")
    assert not problems, "allowlist site mismatch:\n" + "\n".join(problems)


def test_allowlist_has_no_stale_entries() -> None:
    """Fixed violations must be deleted from the allowlist, so it only shrinks."""

    stale = sorted(set(allowlist.ALLOWLIST) - set(_current()))
    assert not stale, "delete these fixed entries from tests/architecture/allowlist.py:\n" + (
        "\n".join(f"  {source} -> {target}" for source, target in stale)
    )


def test_allowlist_entries_name_their_step_and_never_grow() -> None:
    for edge, (reason, sites) in allowlist.ALLOWLIST.items():
        assert STEP.match(reason), f"{edge}: reason must start with the step (A1..A5): {reason!r}"
        assert sites and all(count > 0 for count in sites.values()), edge
    assert len(allowlist.ALLOWLIST) <= ALLOWLIST_SIZE, (
        "the allowlist only shrinks; fix the new violation instead of listing it"
    )
    assert len(allowlist.ALLOWLIST) == ALLOWLIST_SIZE, (
        f"allowlist shrank to {len(allowlist.ALLOWLIST)}: lower ALLOWLIST_SIZE to match"
    )


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(scan.REPO), *args], capture_output=True, text=True, check=False
    )


def parse_allowlist(source: str) -> dict[Edge, object]:
    """The ``ALLOWLIST`` literal of an allowlist file's source (no code is run)."""

    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "ALLOWLIST" for t in targets):
                assert node.value is not None
                value = ast.literal_eval(node.value)
                assert isinstance(value, dict)
                return value
    raise ValueError("no ALLOWLIST assignment")


def growth_since_base(before: dict[Edge, object], after: dict[Edge, object]) -> list[str]:
    """Every way ``after`` is not a shrink of ``before`` (empty: it only shrank).

    Keys must be a subset (a renamed or swapped entry is growth), each entry's
    per-module site counts may not exceed the base's, and neither may the
    entry count or total site count. Base entries in the old ``reason``-only
    format have no sites, so only their keys are compared.
    """

    problems = [f"new entry {edge}" for edge in sorted(set(after) - set(before))]
    for edge in sorted(set(after) & set(before)):
        old, new = before[edge], after[edge]
        if not (isinstance(old, tuple) and isinstance(new, tuple)):
            continue
        old_sites, new_sites = old[1], new[1]
        for module, count in sorted(new_sites.items()):
            if count > old_sites.get(module, 0):
                problems.append(
                    f"{edge}: {module} has {count} sites, {old_sites.get(module, 0)} at the base"
                )
    if len(after) > len(before):
        problems.append(f"entries grew from {len(before)} to {len(after)}")
    before_sites, after_sites = _total_sites(before), _total_sites(after)
    if before_sites is not None and after_sites is not None and after_sites > before_sites:
        problems.append(f"total sites grew from {before_sites} to {after_sites}")
    return problems


def _skip_or_fail(reason: str) -> None:
    """Locally a missing base ref skips; in CI (``CI`` set) it is a failure."""

    if os.environ.get("CI"):
        pytest.fail(f"{reason}; CI must fetch it (actions/checkout fetch-depth: 0)")
    pytest.skip(reason)


def test_allowlist_only_shrinks_against_the_merge_base() -> None:
    """Shrink-only across PRs: no entry, module or site count beyond the merge-base's."""

    if _git("rev-parse", "--verify", "--quiet", f"{BASE_REF}^{{commit}}").returncode != 0:
        _skip_or_fail(f"{BASE_REF} is not available (shallow checkout?); set MEMFW_ARCH_BASE_REF")
    merge_base = _git("merge-base", "HEAD", BASE_REF)
    if merge_base.returncode != 0:
        _skip_or_fail(f"no merge-base between HEAD and {BASE_REF} (shallow history?)")
    base = merge_base.stdout.strip()
    shown = _git("show", f"{base}:{ALLOWLIST_PATH}")
    if shown.returncode != 0:
        pytest.skip(f"{ALLOWLIST_PATH} does not exist at merge-base {base[:12]}")
    problems = growth_since_base(parse_allowlist(shown.stdout), dict(allowlist.ALLOWLIST))
    assert not problems, f"allowlist grew since merge-base {base[:12]}:\n" + "\n".join(
        f"  {problem}" for problem in problems
    )


def test_growth_since_base_catches_renames_swaps_and_site_growth() -> None:
    base: dict[Edge, object] = {
        ("a", "b"): ("A2: x", {"a.m": 2}),
        ("c", "d"): ("A3: y", {"c.n": 1}),
    }
    assert growth_since_base(base, dict(base)) == []
    assert growth_since_base(base, {("a", "b"): ("A2: x", {"a.m": 1})}) == []
    # Swap one entry for another: same count, still growth.
    swapped = {("a", "b"): base[("a", "b")], ("c", "e"): ("A3: y", {"c.n": 1})}
    assert growth_since_base(base, swapped) == ["new entry ('c', 'e')"]
    # Move a site to another module: same total, still growth.
    moved = {("a", "b"): ("A2: x", {"a.m": 1, "a.o": 1}), ("c", "d"): base[("c", "d")]}
    assert growth_since_base(base, moved) == ["('a', 'b'): a.o has 1 sites, 0 at the base"]
    grown = {("a", "b"): ("A2: x", {"a.m": 3}), ("c", "d"): base[("c", "d")]}
    problems = growth_since_base(base, grown)
    assert "('a', 'b'): a.m has 3 sites, 2 at the base" in problems
    assert "total sites grew from 3 to 4" in problems
    old_format: dict[Edge, object] = {("a", "b"): "A2: x"}
    assert growth_since_base(old_format, {("a", "b"): ("A2: x", {"a.m": 9})}) == []
    assert growth_since_base(old_format, {("z", "y"): ("A2: x", {"z.m": 1})}) == [
        "new entry ('z', 'y')"
    ]


def test_skip_or_fail_fails_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        _skip_or_fail("no base")
    monkeypatch.delenv("CI")
    with pytest.raises(pytest.skip.Exception):
        _skip_or_fail("no base")


def test_parse_allowlist_reads_the_literal_only() -> None:
    source = (
        "from typing import Final\n"
        "ALLOWLIST: Final[dict[tuple[str, str], tuple[str, dict[str, int]]]] = {\n"
        "    ('a', 'b'): ('A2: x' ' y', {'a.m': 2}),\n"
        "}\n"
    )
    parsed = parse_allowlist(source)
    assert parsed == {("a", "b"): ("A2: x y", {"a.m": 2})}
    assert _total_sites(parsed) == 2
    assert _total_sites({("a", "b"): "A2: old format"}) is None
    assert parse_allowlist(Path(scan.REPO / ALLOWLIST_PATH).read_text(encoding="utf-8")) == dict(
        allowlist.ALLOWLIST
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
        if _unit(item.source) != "memfw.ports" or item.target.startswith("memfw.ports"):
            continue
        assert item.target.split(".")[0] in stdlib, f"{item.source}:{item.line} -> {item.target}"


# --- mutations against the real map ------------------------------------------


def _mutant(tmp_path: Path, module: str, code: str) -> dict[Edge, tuple[str, Sites]]:
    """Violations a new file ``module`` with ``code`` would add to today's tree."""

    path = tmp_path / "mutant.py"
    path.write_text(code, encoding="utf-8")
    modules = scan.discover_modules()
    found = scan.scan_file(path, module, modules, scan.reexports(modules))
    return _violations(tuple(found))


@pytest.mark.parametrize(
    ("code", "edge"),
    [
        # The root re-export resolves to its defining unit: no laundering via L0.
        ("from memfw import RelatedIndex\n", ("memfw.core", "memfw.related")),
        ("def f():\n    from .. import RelatedIndex\n", ("memfw.core", "memfw.related")),
        ("import memfw\n", ("memfw.core", scan.BARE_ROOT)),
        ("import importlib\nimportlib.import_module('memfw.cli')\n", ("memfw.core", "memfw.cli")),
        ("NAME = 'memfw.mcp.server'\n__import__(NAME)\n", ("memfw.core", "memfw.mcp")),
        ("import pkgutil\npkgutil.resolve_name('memfw.cli:app')\n", ("memfw.core", "memfw.cli")),
        (
            "import importlib\ndef f(n):\n    importlib.import_module(n)\n",
            (
                "memfw.core",
                scan.DYNAMIC,
            ),
        ),
        ("from importlib.util import spec_from_file_location as s\n", None),
        ("P = '../../scripts'\n", ("memfw.core", scan.SCRIPTS)),
        ("P = 'scripts/tool.py'\n", ("memfw.core", scan.SCRIPTS)),
        ("P = 'transcripts/x'\nQ = 'memfw.adapters'\n", None),
    ],
)
def test_mutations_are_caught(tmp_path: Path, code: str, edge: Edge | None) -> None:
    found = _mutant(tmp_path, "memfw.core.mutant", code)
    if edge is None:
        assert not found, found
    else:
        assert edge in found, found


def test_new_site_on_an_allowlisted_edge_is_caught(tmp_path: Path) -> None:
    found = _mutant(tmp_path, "memfw.capture.mutant", "from memfw.cli import _repo\n")
    edge = ("memfw.capture", "memfw.cli")
    assert edge in found
    merged = dict(_current()[edge][1])
    for module, count in found[edge][1].items():
        merged[module] = merged.get(module, 0) + count
    assert merged != allowlist.ALLOWLIST[edge][1]


# --- the scanner -------------------------------------------------------------


def test_scanner_resolves_relative_imports_reexports_and_strings(tmp_path: Path) -> None:
    root = tmp_path / "src"
    package = root / "memfw"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("from .a import Thing\n", encoding="utf-8")
    (package / "a.py").write_text("Thing = 1\n", encoding="utf-8")
    (package / "sub" / "__init__.py").write_text(
        "from .. import a\nfrom ..a import Thing\nfrom memfw import Thing as T\n",
        encoding="utf-8",
    )
    (package / "sub" / "b.py").write_text(
        '"""Docstring naming scripts/ and memfw_readonly_service does not count."""\n'
        "from importlib.util import spec_from_file_location\n"
        "def f(path):\n"
        "    from . import c\n"
        "    from ...outside import x\n"
        "    import os\n"
        "    spec_from_file_location('n', path)\n"
        "    return Path('x') / 'scripts' / 'memfw_readonly_service.py'\n",
        encoding="utf-8",
    )
    found = scan.scan_tree(root, "memfw")
    pairs = {(item.source, item.target) for item in found}
    assert ("memfw.sub", "memfw.a") in pairs
    assert ("memfw.sub", "memfw") not in pairs  # ``Thing`` resolves to memfw.a
    assert ("memfw.sub.b", "memfw.sub") in pairs  # ``from . import c``: c is not a module
    assert ("memfw.sub.b", scan.ESCAPES) in pairs
    assert ("memfw.sub.b", "os") in pairs
    assert ("memfw.sub.b", scan.DYNAMIC) in pairs
    assert sorted({item.line for item in found if item.target == scan.SCRIPTS}) == [8]


def test_entry_point_group_name_is_never_an_edge(tmp_path: Path) -> None:
    root = tmp_path / "src"
    (root / "memfw" / "ports").mkdir(parents=True)
    (root / "memfw" / "adapters").mkdir()
    for name in ("__init__.py", "ports/__init__.py", "adapters/__init__.py"):
        (root / "memfw" / name).write_text("", encoding="utf-8")
    group = 'ENTRY_POINT_GROUP = "memfw.adapters"\n'
    (root / "memfw" / "ports" / "registry.py").write_text(group, encoding="utf-8")
    (root / "memfw" / "ports" / "other.py").write_text(group, encoding="utf-8")
    pairs = {(item.source, item.target) for item in scan.scan_tree(root, "memfw")}
    assert ("memfw.ports.registry", "memfw.adapters") not in pairs
    assert ("memfw.ports.other", "memfw.adapters") in pairs  # the exemption is per module
    assert ("memfw.ports.registry", "memfw.adapters") in scan.NOT_MODULE_NAMES
    registry = (scan.SRC / "memfw" / "ports" / "registry.py").read_text(encoding="utf-8")
    assert 'ENTRY_POINT_GROUP = "memfw.adapters"' in registry


@pytest.mark.parametrize(
    ("code", "target"),
    [
        ("import importlib\nimportlib.import_module('.cli', 'memfw')\n", "memfw.cli"),
        (
            "import importlib\nimportlib.import_module('..cli', package='memfw.core')\n",
            "memfw.cli",
        ),
        ("__import__('memfw', fromlist=['cli'])\n", "memfw.cli"),
        ("__import__('memfw.mcp', None, None, ['server'])\n", "memfw.mcp.server"),
        (
            "from importlib.util import spec_from_file_location\n"
            "spec_from_file_location('x', 'src/memfw/cli.py')\n",
            "memfw.cli",
        ),
        (
            "from importlib.util import spec_from_file_location\n"
            "spec_from_file_location('x', '../memfw/mcp/__init__.py')\n",
            "memfw.mcp",
        ),
    ],
)
def test_loader_call_forms_resolve(tmp_path: Path, code: str, target: str) -> None:
    path = tmp_path / "mutant.py"
    path.write_text(code, encoding="utf-8")
    modules = scan.discover_modules()
    found = scan.scan_file(path, "memfw.core.mutant", modules, scan.reexports(modules))
    assert target in {item.target for item in found}
    assert scan.DYNAMIC not in {item.target for item in found}


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
