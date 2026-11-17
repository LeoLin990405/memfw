"""The package ↔ ``PORT_VERSION`` compatibility table (architecture sections 4 and 6).

``docs/ports-and-recall.md`` section 1.1 lists, per workspace package and
version, every port it ships or builds on with that port's contract version.
The table must match ``memfw.ports.PORT_VERSIONS`` and the package metadata.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from memfw.ports import CLAIMS_PORT_VERSION, PORT_VERSIONS, USAGE_PORT_VERSION

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "ports-and-recall.md"
ROW = re.compile(r"^\| `([a-z-]+)` \| ([0-9.]+) \| `([A-Za-z]+)` \| (\d+) \|$")
#: Ports each non-ports package builds on.
CONSUMES = {"memfw-recall": {"RecallProvider"}}


def _table() -> list[tuple[str, str, str, int]]:
    text = DOC.read_text(encoding="utf-8")
    body = text.split("<!-- port-versions:start -->", 1)[1].split("<!-- port-versions:end -->")[0]
    rows = []
    for line in body.strip().splitlines()[2:]:
        match = ROW.match(line.strip())
        assert match, line
        rows.append((match[1], match[2], match[3], int(match[4])))
    return rows


def _version(package: str) -> str:
    data = tomllib.loads((ROOT / "packages" / package / "pyproject.toml").read_text("utf-8"))
    return str(data["project"]["version"])


def test_ports_rows_are_exactly_port_versions() -> None:
    rows = [row for row in _table() if row[0] == "memfw-ports"]
    assert {(port, number) for _, _, port, number in rows} == set(PORT_VERSIONS.items())
    assert len(rows) == len(PORT_VERSIONS)
    assert {version for _, version, _, _ in rows} == {_version("memfw-ports")}


def test_consumer_rows_match_metadata_and_versions() -> None:
    for package, ports in CONSUMES.items():
        rows = [row for row in _table() if row[0] == package]
        assert {port for _, _, port, _ in rows} == ports, package
        for _, version, port, number in rows:
            assert version == _version(package), package
            assert number == PORT_VERSIONS[port], (package, port)


def test_every_table_package_is_a_workspace_member() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = {Path(m).name for m in data["tool"]["uv"]["workspace"]["members"]}
    assert {row[0] for row in _table()} == members


def test_named_port_constants_feed_the_table() -> None:
    assert PORT_VERSIONS["ClaimVerifier"] == PORT_VERSIONS["ClaimStager"] == CLAIMS_PORT_VERSION
    assert PORT_VERSIONS["UsageSink"] == USAGE_PORT_VERSION
