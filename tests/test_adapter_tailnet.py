"""The tailnet host inventory Ingestor (``packages/adapters/memfw-adapter-tailnet``).

No Tailscale is needed: inputs are synthetic ``tailscale status --json``
documents (documentation-only addresses, ``example-*`` names); live mode runs a
fake ``tailscale`` script or a mocked ``Popen``.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import stat
import subprocess
import sys
import textwrap
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.ports import AdapterRegistry, AdaptersConfig, IngestItem
from memfw.ports.testing import check_ingestor

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-tailnet"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # a workspace member, not installed here
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_tailnet import (  # noqa: E402
    STATUS_ARGS,
    Settings,
    Snapshot,
    TailnetError,
    TailnetIngestor,
    adapter_spec,
    build_scan,
    child_env,
    diff,
    dns_parts,
    factory,
    host_id,
    parse_status,
    parse_status_bytes,
    run_tailscale,
)
from memfw_adapter_tailnet.cli import run as cli_run  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "tailnet"
RECENT = FIXTURES / "status_recent.json"
NOW = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
CANARIES = (
    "canary-login-7f3a",
    "canary-second-9b1c",
    "Canary Display",
    "Canary Second",
    "canary-tailnet-owner",
    "@example.com",
    "@example.org",
    "nodekey:",
    "nSYNTH",  # raw node IDs: only their hash is kept
    "192.0.2.10",  # endpoint addresses
    "is-admin",
    "ProfilePic",
    # fields of the real schema that are never read (sshHostKeys, CapMap, NodeID, ...)
    "canary-nodeid",
    "canary-sshkey",
    "canary-capmap",
    "canary-certdomain",
    "canary-extrarecord",
    "100.64.0.250",
    ":12345",
    "123456789",
    # shared-in and provider exit nodes are left out by default
    "canary-shared-host",
    "canary-sharer",
    "canary-exit-node",
    "Exampleland",
    "Example City",
    # the MagicDNS suffix names the tailnet: only its hash is kept
    "example-tailnet",
    "ts.net",
)


def _id(node_id: str) -> str:
    return host_id("id:" + node_id)


def _status() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(RECENT.read_text(encoding="utf-8"))
    return data


def _vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "memories").mkdir(parents=True)
    (vault / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    return vault


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _scan(
    tmp_path: Path, document: object, *extra: str, now: datetime = NOW, vault: bool = True
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Run the CLI on ``document``; returns ``(exit, stdout JSON, report.json)``."""

    source = _write(tmp_path / "status.json", document)
    argv = ["scan", "--input", str(source), "--state", str(tmp_path / "state"), *extra]
    if vault:
        argv += ["--vault", str(tmp_path / "vault")]
    return _run(argv, now)


def _run(argv: list[str], now: datetime = NOW) -> tuple[int, dict[str, Any], dict[str, Any]]:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_run(argv, now=lambda: now)
    state = Path(argv[argv.index("--state") + 1])
    report = json.loads((state / "report.json").read_text(encoding="utf-8"))
    return code, json.loads(buffer.getvalue()), report


def _candidates(vault: Path) -> list[dict[str, Any]]:
    directory = vault / "_candidates" / "tailnet"
    if not directory.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def _by_change(vault: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for record in _candidates(vault):
        body = json.loads(record["body"])
        name = (body["host"] or body["previous"])["name"]
        found.setdefault(record["metadata"]["changes"], []).append(name)
    return {key: sorted(value) for key, value in found.items()}


# -- packaging and isolation ------------------------------------------------------------


def test_adapter_imports_only_ports_and_stdlib() -> None:
    allowed = set(sys.stdlib_module_names) | {"memfw_adapter_tailnet"}
    files = list((ADAPTER_SRC / "memfw_adapter_tailnet").glob("*.py"))
    assert len(files) >= 6
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.startswith(("memfw.", "memfw_")):
                    assert name.startswith(("memfw.ports", "memfw_ports")), (path.name, name)
                else:
                    assert name.split(".")[0] in allowed, (path.name, name)


def test_seam_imports_the_same_names_under_both_roots() -> None:
    tree = ast.parse((ADAPTER_SRC / "memfw_adapter_tailnet" / "_ports.py").read_text("utf-8"))
    branch = next(node for node in tree.body if isinstance(node, ast.Try))
    inside = [n for n in branch.body if isinstance(n, ast.ImportFrom)]
    outside = [n for n in branch.handlers[0].body if isinstance(n, ast.ImportFrom)]
    assert inside and len(inside) == len(outside)
    for sibling, standalone in zip(inside, outside, strict=True):
        assert sibling.module and sibling.module.startswith("memfw.ports.")
        assert standalone.module == sibling.module.replace("memfw.ports.", "memfw_ports.")
        assert [a.name for a in sibling.names] == [a.name for a in standalone.names]


def test_memfw_never_imports_it_and_nothing_private_is_baked_in() -> None:
    for path in (ROOT / "src").rglob("*.py"):
        assert "memfw_adapter_tailnet" not in path.read_text(encoding="utf-8"), path
    project = tomllib.loads((ADAPTER / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["dependencies"] == ["memfw-ports==0.3.0"]
    assert project["project"]["entry-points"]["memfw.adapters"] == {
        "tailnet": "memfw_adapter_tailnet:adapter_spec"
    }
    assert project["project"]["scripts"] == {
        "memfw-adapter-tailnet": "memfw_adapter_tailnet.cli:main"
    }
    texts = [
        p.read_text(encoding="utf-8") for p in ADAPTER.rglob("*") if p.suffix in (".py", ".md")
    ]
    texts += [p.read_text(encoding="utf-8") for p in FIXTURES.iterdir()]
    texts.append((ROOT / "docs" / "adapter-tailnet.md").read_text(encoding="utf-8"))
    for text in texts:
        for marker in ("/Users/", "/home/", "/opt/"):
            assert marker not in text, marker
    # Fixture addresses are documentation / CGNAT examples only.
    for document in (_status(),):
        for node in [document["Self"], *document["Peer"].values()]:
            for ip in node["TailscaleIPs"]:
                assert ip.startswith(("100.64.", "fd7a:115c:a1e0:")), ip


def test_registry_keeps_it_off_and_builds_it_when_enabled() -> None:
    registry = AdapterRegistry()
    registry.register(adapter_spec())
    assert registry.load("tailnet", AdaptersConfig()).reason == "disabled"
    enabled = AdaptersConfig.from_mapping({"enabled": ["tailnet"], "tailnet": {}})
    status = registry.load("tailnet", enabled)
    assert status.available and isinstance(status.adapter, TailnetIngestor)
    assert status.adapter.settings.privacy == "local-private"
    shared = AdaptersConfig.from_mapping({"enabled": ["tailnet"], "tailnet": {"privacy": "Shared"}})
    adapter = registry.load("tailnet", shared).adapter
    assert isinstance(adapter, TailnetIngestor) and adapter.settings.privacy == "shared"
    for bad in ({"privacy": "everyone"}, {"endpoint": "x"}):
        with pytest.raises(ValueError):
            factory(bad)


def test_passes_the_ingestor_contract_kit(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    status = parse_status(_status())
    items = build_scan(
        status,
        None,
        now=NOW,
        privacy="local-private",
        allowlist=None,
        offline_hours=24,
        mode="input",
    ).items
    result = check_ingestor(TailnetIngestor(), vault=vault, items=items)
    assert len(result.staged) == 3


# -- parsing ------------------------------------------------------------------------------


def test_parses_the_recent_schema() -> None:
    status = parse_status(_status())
    assert status.skipped == 0 and status.backend_state == "Running"
    assert (status.skipped_shared, status.skipped_location) == (1, 1)
    hosts = {h.name: h for h in status.hosts}
    assert set(hosts) == {"laptop-a", "example-nas", "phone-b"}
    nas = hosts["example-nas"]
    assert nas.ips == ("100.64.0.11", "fd7a:115c:a1e0::b")  # IPv4 first, then IPv6
    assert nas.dns_label == "example-nas"
    assert nas.dns_suffix_hash == dns_parts("x.example-tailnet.ts.net")[1]
    assert nas.dns_suffix_hash.startswith("sfx-") and len(nas.dns_suffix_hash) == 20
    assert (nas.os, nas.online, nas.tags) == ("linux", True, ("tag:server", "tag:storage"))
    assert nas.last_seen == "2026-01-05T11:59:00Z"  # nanoseconds dropped
    assert hosts["laptop-a"].is_self and hosts["laptop-a"].last_seen is None  # Go zero time
    assert hosts["phone-b"].online is False and hosts["phone-b"].tags == ()


def test_stable_ids_are_hashes_of_the_node_id() -> None:
    first = parse_status(_status())
    again = parse_status(_status())
    assert [h.host_id for h in first.hosts] == [h.host_id for h in again.hosts]
    nas = next(h for h in first.hosts if h.name == "example-nas")
    assert nas.host_id == host_id("id:nSYNTHNAS00002CNTRL")
    assert nas.host_id.startswith("host-") and len(nas.host_id) == 29
    assert "nSYNTH" not in json.dumps([h.as_dict() for h in first.hosts])
    # The id follows the node, not its name, keys or addresses.
    moved = _status()
    peer = moved["Peer"]["nodekey:00000000000000000000000000000000000000000000000000000000000000b2"]
    peer.update(HostName="renamed-nas", TailscaleIPs=["100.64.0.99"], PublicKey="nodekey:ff")
    assert next(h for h in parse_status(moved).hosts if h.name == "renamed-nas").host_id == (
        nas.host_id
    )


def test_old_schema_without_ids_online_or_tags() -> None:
    document = {
        "BackendState": "Running",
        "Self": {
            "PublicKey": "nodekey:aa",
            "HostName": "laptop-a",
            "TailscaleIPs": ["100.64.0.10"],
        },
        "Peer": {
            "nodekey:bb": {"HostName": "example-nas", "TailscaleIPs": ["100.64.0.11"]},
            "nodekey:cc": {"DNSName": "old-box.example-tailnet.ts.net.", "TailscaleIPs": []},
        },
    }
    status = parse_status(document)
    hosts = {h.name: h for h in status.hosts}
    assert set(hosts) == {"laptop-a", "example-nas", "old-box"}  # name from the DNS label
    assert hosts["example-nas"].host_id == host_id("key:nodekey:bb")
    assert all(h.online is None and h.tags == () and h.os == "" for h in status.hosts)


def test_missing_and_mistyped_fields_are_treated_as_absent() -> None:
    document = {
        "Self": {
            "ID": "n1",
            "HostName": 7,
            "DNSName": None,
            "TailscaleIPs": "100.64.0.10",
            "Online": "yes",
            "LastSeen": "not a time",
            "Tags": "tag:x",
            "OS": ["linux"],
        },
        "Peer": {
            "k2": {
                "ID": "n2",
                "HostName": "laptop-a\u0007‮",
                "TailscaleIPs": [
                    "100.64.0.20",
                    "not-an-ip",
                    5,
                    "fd7a:115c:a1e0::20",
                    "100.64.0.20",
                ],
            },
            "k3": "not a node",
            "k4": {"HostName": "no-id-at-all"},
            "k5": {"ID": "n2", "HostName": "duplicate"},
        },
    }
    status = parse_status(document)
    assert status.skipped == 2  # a non-object and a duplicate id
    hosts = {h.host_id: h for h in status.hosts}
    assert hosts[host_id("key:k4")].name == "no-id-at-all"  # old schema: the map key
    assert parse_status({"Self": {"HostName": "laptop-a"}}).skipped == 1  # Self without any id
    first = hosts[host_id("id:n1")]
    assert (first.name, first.dns_label, first.ips, first.online, first.last_seen) == (
        "unnamed",
        "",
        (),
        None,
        None,
    )
    assert (first.tags, first.os) == ((), "")
    second = hosts[host_id("id:n2")]
    assert second.name == "laptop-a"  # control and bidi characters stripped
    assert second.ips == ("100.64.0.20", "fd7a:115c:a1e0::20")


def test_ipv6_only_and_no_peer_map() -> None:
    for peer in ({}, None, "absent"):
        document: dict[str, Any] = {
            "BackendState": "Running",
            "Self": {"ID": "n1", "HostName": "laptop-a", "TailscaleIPs": ["fd7a:115c:a1e0::a"]},
        }
        if peer != "absent":
            document["Peer"] = peer
        status = parse_status(document)
        assert [(h.name, h.ips) for h in status.hosts] == [("laptop-a", ("fd7a:115c:a1e0::a",))]
    assert parse_status({}).hosts == ()  # no Self, no Peer


@pytest.mark.parametrize("state", ["Stopped", "NeedsLogin", "Starting", "NoState"])
def test_a_backend_that_is_not_running_is_refused(state: str) -> None:
    with pytest.raises(TailnetError, match="backend_not_running"):
        parse_status({"BackendState": state, "Peer": {}})


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"[1, 2]", "bad_status_json"),
        (b"{nope", "bad_status_json"),
        (b"\xff\xfe", "bad_status_json"),
        (b"[" * 100_000, "bad_status_json"),
    ],
)
def test_bad_documents_fail_with_a_fixed_code(data: bytes, code: str) -> None:
    with pytest.raises(TailnetError, match=code):
        parse_status_bytes(data)


# -- privacy ------------------------------------------------------------------------------


def test_user_and_login_data_never_reach_any_output(tmp_path: Path) -> None:
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("laptop-a\nexample-nas.example-tailnet.ts.net\n", encoding="utf-8")
    import contextlib
    import io

    source = _write(tmp_path / "status.json", _status())
    _vault(tmp_path)
    buffer = io.StringIO()
    argv = [
        "scan",
        "--input",
        str(source),
        "--state",
        str(tmp_path / "state"),
        "--vault",
        str(tmp_path / "vault"),
        "--allowlist",
        str(allowlist),
    ]
    with contextlib.redirect_stdout(buffer):
        code = cli_run(argv, now=lambda: NOW)
    assert code == 3  # phone-b is not on the allowlist
    outputs = [buffer.getvalue()]
    for directory in (tmp_path / "state", tmp_path / "vault" / "_candidates"):
        outputs += [p.read_text(encoding="utf-8") for p in directory.rglob("*") if p.is_file()]
    assert len(outputs) >= 6  # stdout, snapshot, report, lock, three candidates
    for text in outputs:
        for canary in CANARIES:
            assert canary not in text, canary


def test_report_and_stdout_carry_no_names_ids_or_addresses(tmp_path: Path) -> None:
    _vault(tmp_path)
    code, out, report = _scan(tmp_path, _status())
    assert code == 0
    text = json.dumps(out) + (tmp_path / "state" / "report.json").read_text("utf-8")
    for leak in ("laptop-a", "example-nas", "phone-b", "100.64.", "fd7a:", "host-", "ts.net"):
        assert leak not in text, leak
    assert report["status"] == "ok" and report["committed"] is True


def test_candidates_default_to_local_private_and_the_operator_may_widen(tmp_path: Path) -> None:
    _vault(tmp_path)
    _scan(tmp_path, _status())
    records = _candidates(tmp_path / "vault")
    assert records and {r["metadata"]["privacy"] for r in records} == {"local-private"}
    other = tmp_path / "second"
    other.mkdir()
    _vault(other)
    _scan(other, _status(), "--privacy", "shared")
    assert {r["metadata"]["privacy"] for r in _candidates(other / "vault")} == {"shared"}


def test_the_ingestor_never_loosens_an_items_privacy(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    ingestor = TailnetIngestor(Settings(privacy="shared"))
    items = [
        IngestItem("a", "x", "r1", {"privacy": "public"}),
        IngestItem("b", "x", "r2", {"privacy": "local-private"}),
        IngestItem("c", "x", "r3", {}),
        IngestItem("d", "x", "r4", {"privacy": "whatever"}),
    ]
    ingestor.stage(vault, items, proposal_id="p-1", source="tailscale-status")
    levels = {r["title"]: r["metadata"]["privacy"] for r in _candidates(vault)}
    assert levels == {"a": "shared", "b": "local-private", "c": "shared", "d": "local-private"}


# -- diff and candidates -------------------------------------------------------------------


def test_first_run_stages_every_host_as_new_and_records_state(tmp_path: Path) -> None:
    _vault(tmp_path)
    code, out, report = _scan(tmp_path, _status())
    assert code == 0 and out["committed"] is True and out["staged"] == 3
    assert _by_change(tmp_path / "vault") == {"new": ["example-nas", "laptop-a", "phone-b"]}
    record = next(r for r in _candidates(tmp_path / "vault") if "example-nas" in r["title"])
    assert record["title"] == "tailnet host new: example-nas"
    assert record["source"] == "tailscale-status" and record["status"] == "staged"
    assert record["proposal_id"] == "tailnet-scan-20260105T120000Z"
    meta = record["metadata"]
    assert (meta["kind"], meta["epistemic"], meta["causal"], meta["concept"]) == (
        "host-fact",
        "fact",
        "none",
        "host:example-nas",
    )
    body = json.loads(record["body"])
    assert body["previous"] is None and body["changes"] == ["new"]
    assert body["host"] == {
        "host_id": meta["host_id"],
        "name": "example-nas",
        "dns_label": "example-nas",
        "dns_suffix_hash": dns_parts("x.example-tailnet.ts.net")[1],
        "ips": ["100.64.0.11", "fd7a:115c:a1e0::b"],
        "os": "linux",
        "tags": ["tag:server", "tag:storage"],
        "self": False,
    }
    assert body["since"] is None  # first run: no previous snapshot
    for volatile in ("online", "last_seen", "observed_at"):
        assert volatile not in record["body"] and volatile not in meta
    snapshot = tmp_path / "state" / "snapshot.json"
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state" / "report.json").stat().st_mode) == 0o600
    assert report["first_run"] is True and report["counts"]["new"] == 3


def test_unchanged_hosts_produce_no_candidate_even_when_online_moves(tmp_path: Path) -> None:
    _vault(tmp_path)
    _scan(tmp_path, _status())
    before = len(_candidates(tmp_path / "vault"))
    later = _status()
    for node in later["Peer"].values():
        node["Online"] = not node["Online"]
        node["LastSeen"] = "2026-01-06T09:00:00Z"
    code, out, report = _scan(tmp_path, later, now=NOW + timedelta(days=1))
    assert code == 0 and out["counts"]["candidates"] == 0 and out["staged"] == 0
    assert len(_candidates(tmp_path / "vault")) == before
    assert report["first_run"] is False


def test_new_gone_ip_change_rename_os_and_tags(tmp_path: Path) -> None:
    _vault(tmp_path)
    _scan(tmp_path, _status())
    vault = tmp_path / "vault"
    for path in (vault / "_candidates" / "tailnet").glob("*.json"):
        path.unlink()  # reviewed; keep only the second run's candidates
    later = _status()
    peers = later["Peer"]
    nas = peers["nodekey:00000000000000000000000000000000000000000000000000000000000000b2"]
    nas["TailscaleIPs"] = ["100.64.0.21", "fd7a:115c:a1e0::b"]
    nas["HostName"], nas["DNSName"] = "example-nas-2", "example-nas-2.example-tailnet.ts.net."
    later["Self"]["OS"], later["Self"]["Tags"] = "linux", ["tag:laptop"]
    del peers["nodekey:00000000000000000000000000000000000000000000000000000000000000c3"]
    peers["nodekey:d4"] = {
        "ID": "nSYNTHNEW0004CNTRL",
        "HostName": "laptop-c",
        "TailscaleIPs": ["100.64.0.13"],
        "Online": True,
    }
    code, out, report = _scan(tmp_path, later, now=NOW + timedelta(hours=1))
    assert code == 0
    assert _by_change(vault) == {
        "new": ["laptop-c"],
        "gone": ["phone-b"],
        "renamed+ip_changed": ["example-nas-2"],
        "os_changed+tags_changed": ["laptop-a"],
    }
    counts = report["counts"]
    assert (counts["new"], counts["gone"], counts["renamed"], counts["ip_changed"]) == (1, 1, 1, 1)
    gone = next(r for r in _candidates(vault) if r["metadata"]["changes"] == "gone")
    body = json.loads(gone["body"])
    assert body["host"] is None and body["previous"]["name"] == "phone-b"
    assert body["previous"]["ips"] == ["100.64.0.12"]
    renamed = next(r for r in _candidates(vault) if "renamed" in r["metadata"]["changes"])
    body = json.loads(renamed["body"])
    assert (body["previous"]["name"], body["host"]["name"]) == ("example-nas", "example-nas-2")
    assert body["previous"]["ips"] == ["100.64.0.11", "fd7a:115c:a1e0::b"]
    assert renamed["title"] == "tailnet host renamed, ip_changed: example-nas-2"
    # The gone host is forgotten: a third identical run stages nothing.
    code, out, _ = _scan(tmp_path, later, now=NOW + timedelta(hours=2))
    assert out["counts"]["candidates"] == 0
    snapshot = json.loads((tmp_path / "state" / "snapshot.json").read_text("utf-8"))
    tombstones = [r for r in snapshot["hosts"].values() if r["gone_since"]]
    assert [r["name"] for r in tombstones] == ["phone-b"]


def test_a_returning_host_is_returned_with_its_previous_facts(tmp_path: Path) -> None:
    _vault(tmp_path)
    _scan(tmp_path, _status())
    later = _status()
    phone = later["Peer"].pop(
        "nodekey:00000000000000000000000000000000000000000000000000000000000000c3"
    )
    _scan(tmp_path, later, now=NOW + timedelta(hours=1))  # gone (e.g. hidden by an ACL)
    vault = tmp_path / "vault"
    for path in (vault / "_candidates" / "tailnet").glob("*.json"):
        path.unlink()
    later["Peer"]["nodekey:c3"] = {**phone, "TailscaleIPs": ["100.64.0.33"]}
    code, out, report = _scan(tmp_path, later, now=NOW + timedelta(hours=2))
    assert _by_change(vault) == {"returned+ip_changed": ["phone-b"]}
    record = _candidates(vault)[0]
    body = json.loads(record["body"])
    assert body["previous"]["ips"] == ["100.64.0.12"] and body["host"]["ips"] == ["100.64.0.33"]
    assert report["counts"]["returned"] == 1 and report["counts"]["new"] == 0


def test_tombstones_expire(tmp_path: Path) -> None:
    from memfw_adapter_tailnet.scan import TOMBSTONE_DAYS

    status = parse_status(_status())
    first = build_scan(
        status,
        None,
        now=NOW,
        privacy="local-private",
        allowlist=None,
        offline_hours=24,
        mode="input",
    )
    empty = parse_status({"BackendState": "Running"})
    snap = Snapshot(first.snapshot, "2026-01-05T12:00:00Z")
    gone = build_scan(
        empty,
        snap,
        now=NOW,
        privacy="local-private",
        allowlist=None,
        offline_hours=24,
        mode="input",
    )
    assert len(gone.snapshot) == 3 and all(r["gone_since"] for r in gone.snapshot.values())
    later = NOW + timedelta(days=TOMBSTONE_DAYS + 1)
    old = build_scan(
        empty,
        Snapshot(gone.snapshot, "2026-01-05T12:00:00Z"),
        now=later,
        privacy="local-private",
        allowlist=None,
        offline_hours=24,
        mode="input",
    )
    assert old.snapshot == {} and old.items == []


def test_diff_is_pure_and_ignores_volatile_fields() -> None:
    status = parse_status(_status())
    hosts = {h.host_id: {**h.as_dict(), "online": None, "last_seen": None} for h in status.hosts}
    assert diff(Snapshot(hosts, "2026-01-01T00:00:00Z"), status) == []
    assert {c.codes for c in diff(None, status)} == {("new",)}


def test_preview_without_a_vault_stages_nothing_and_keeps_the_snapshot(tmp_path: Path) -> None:
    code, out, report = _scan(tmp_path, _status(), vault=False)
    assert code == 0 and out["committed"] is False and out["counts"]["candidates"] == 3
    assert not (tmp_path / "state" / "snapshot.json").exists()
    assert report["committed"] is False and "staged" not in out
    _vault(tmp_path)
    _, out, _ = _scan(tmp_path, _status())
    assert out["staged"] == 3  # the preview did not consume the changes


def test_a_rerun_after_a_failed_stage_sees_the_same_changes(tmp_path: Path) -> None:
    # No vault.yaml: staging is refused, so the snapshot must not advance.
    (tmp_path / "vault").mkdir()
    code, out, report = _scan(tmp_path, _status())
    assert code == 1 and out == {"error": "staging_failed", "status": "error"}
    assert report["status"] == "error" and report["error"] == "staging_failed"
    assert not (tmp_path / "state" / "snapshot.json").exists()
    (tmp_path / "vault" / "vault.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    code, out, _ = _scan(tmp_path, _status())
    assert code == 0 and out["staged"] == 3


def test_an_identical_rerun_on_the_same_snapshot_is_idempotent(tmp_path: Path) -> None:
    _vault(tmp_path)
    snapshot_dir = tmp_path / "state"
    _scan(tmp_path, _status())
    first = sorted(p.name for p in (tmp_path / "vault" / "_candidates" / "tailnet").iterdir())
    (snapshot_dir / "snapshot.json").unlink()  # as if the run crashed before committing
    _, out, _ = _scan(tmp_path, _status(), now=NOW + timedelta(minutes=5))
    assert out["staged"] == 0
    assert sorted(p.name for p in (tmp_path / "vault" / "_candidates" / "tailnet").iterdir()) == (
        first
    )


def test_crash_between_staging_and_snapshot_then_rerun_stages_duplicates_only(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    _scan(tmp_path, _status())
    snapshot = (tmp_path / "state" / "snapshot.json").read_text("utf-8")
    changed = _status()
    changed["Self"]["OS"] = "linux"
    nas = changed["Peer"][
        "nodekey:00000000000000000000000000000000000000000000000000000000000000b2"
    ]
    nas["Online"], nas["LastSeen"] = False, "2026-01-05T12:30:00Z"  # volatile, must not matter
    _, out, report = _scan(tmp_path, changed, now=NOW + timedelta(hours=1))
    assert out["staged"] == 1
    (tmp_path / "state" / "snapshot.json").write_text(snapshot, "utf-8")  # the crash
    nas["Online"], nas["LastSeen"] = True, "2026-01-05T13:01:00Z"
    _, out, report = _scan(tmp_path, changed, now=NOW + timedelta(hours=1, minutes=1))
    assert (out["staged"], report["duplicates"]) == (0, 1)
    assert len(_candidates(tmp_path / "vault")) == 4
    record = next(r for r in _candidates(tmp_path / "vault") if "os_changed" in r["title"])
    assert json.loads(record["body"])["since"] == "2026-01-05T12:00:00Z"


def test_a_corrupt_snapshot_is_refused_not_treated_as_a_first_run(tmp_path: Path) -> None:
    _vault(tmp_path)
    (tmp_path / "state").mkdir(mode=0o700)
    for content in (
        "{not json",
        json.dumps({"format": "other", "version": 1, "hosts": {}}),
        json.dumps({"format": "memfw.tailnet.snapshot", "version": 1, "hosts": []}),
        json.dumps({"format": "memfw.tailnet.snapshot", "version": 1, "hosts": {}}),  # no time
    ):
        (tmp_path / "state" / "snapshot.json").write_text(content, encoding="utf-8")
        code, out, _ = _scan(tmp_path, _status())
        assert (code, out["error"]) == (1, "bad_snapshot")
    assert _candidates(tmp_path / "vault") == []


def _good_snapshot(tmp_path: Path) -> dict[str, Any]:
    _vault(tmp_path)
    _scan(tmp_path, _status())
    data: dict[str, Any] = json.loads((tmp_path / "state" / "snapshot.json").read_text("utf-8"))
    return data


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ips", 5),
        ("ips", [1]),
        ("tags", "tag:x"),
        ("name", None),
        ("dns_label", 3),
        ("online", "yes"),
        ("last_seen", "never"),
        ("offline_since", 7),
        ("gone_since", "soon"),
        ("self", "no"),
    ],
)
def test_snapshot_field_types_are_validated(tmp_path: Path, field: str, value: object) -> None:
    data = _good_snapshot(tmp_path)
    first = next(iter(data["hosts"]))
    data["hosts"][first][field] = value
    (tmp_path / "state" / "snapshot.json").write_text(json.dumps(data), "utf-8")
    code, out, report = _scan(tmp_path, _status(), now=NOW + timedelta(hours=1))
    assert (code, out["error"], report["error"]) == (1, "bad_snapshot", "bad_snapshot")


def test_snapshot_keys_must_be_host_ids(tmp_path: Path) -> None:
    data = _good_snapshot(tmp_path)
    data["hosts"]["laptop-a"] = data["hosts"].pop(next(iter(data["hosts"])))
    (tmp_path / "state" / "snapshot.json").write_text(json.dumps(data), "utf-8")
    code, out, _ = _scan(tmp_path, _status(), now=NOW + timedelta(hours=1))
    assert (code, out["error"]) == (1, "bad_snapshot")


def test_an_unexpected_exception_is_reported_as_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw_adapter_tailnet.cli as cli

    def boom(*args: object, **kwargs: object) -> object:
        raise KeyError("canary-internal-detail")

    monkeypatch.setattr(cli, "build_scan", boom)
    _vault(tmp_path)
    code, out, report = _scan(tmp_path, _status())
    assert (code, out, report["error"]) == (1, {"error": "internal", "status": "error"}, "internal")
    assert "canary-internal-detail" not in json.dumps(report)
    assert not (tmp_path / "state" / "snapshot.json").exists()


def test_an_existing_state_directory_is_tightened(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o755)
    state.chmod(0o775)
    _vault(tmp_path)
    code, _, _ = _scan(tmp_path, _status())
    assert code == 0 and stat.S_IMODE(state.stat().st_mode) == 0o700


def test_a_state_directory_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memfw_adapter_tailnet.scan as scan

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    other = os.getuid() + 1
    monkeypatch.setattr(scan.os, "getuid", lambda: other)
    with pytest.raises(TailnetError, match="state_insecure"):
        with scan.state_lock(state):
            pass


def test_state_directory_refusals(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "state"
    link.symlink_to(real)
    import contextlib
    import io

    buffer = io.StringIO()
    source = _write(tmp_path / "status.json", _status())
    with contextlib.redirect_stdout(buffer):
        code = cli_run(["scan", "--input", str(source), "--state", str(link)], now=lambda: NOW)
    assert code == 1 and json.loads(buffer.getvalue())["error"] == "state_is_symlink"


def test_a_concurrent_scan_is_refused(tmp_path: Path) -> None:
    from memfw_adapter_tailnet.scan import state_lock

    with state_lock(tmp_path / "state"):
        with pytest.raises(TailnetError, match="state_locked"):
            with state_lock(tmp_path / "state"):
                pass


# -- allowlist alerts --------------------------------------------------------------------


def _allow(tmp_path: Path, text: str) -> str:
    path = tmp_path / "allow.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_allowlisted_host_offline_longer_than_the_threshold(tmp_path: Path) -> None:
    _vault(tmp_path)
    allow = _allow(
        tmp_path,
        "# expected machines\nLAPTOP-A\nexample-nas.example-tailnet.ts.net.\n"
        f"{_id('nSYNTHPHONE003CNTRL')}  # the phone, by host id\n",
    )
    # phone-b last seen 2026-01-01T08:00Z, NOW is 2026-01-05T12:00Z: about 100 h offline.
    code, out, report = _scan(tmp_path, _status(), "--allowlist", allow)
    assert code == 3 and out["alerts"] == ["listed_offline"] == report["alerts"]
    counts = report["counts"]
    assert (counts["listed_offline"], counts["listed_missing"], counts["unknown"]) == (1, 0, 0)
    assert report["status"] == "alert"
    code, out, report = _scan(tmp_path, _status(), "--allowlist", allow, "--offline-hours", "200")
    assert code == 0 and report["alerts"] == [] and report["counts"]["listed_offline"] == 0


def test_unknown_and_missing_hosts_and_the_allowlist_is_never_changed(tmp_path: Path) -> None:
    _vault(tmp_path)
    allow = _allow(tmp_path, "laptop-a\nexample-printer\n")
    before = Path(allow).read_bytes()
    code, out, report = _scan(tmp_path, _status(), "--allowlist", allow)
    assert code == 3
    counts = report["counts"]
    assert (counts["unknown"], counts["listed_missing"], counts["listed_offline"]) == (2, 1, 0)
    assert report["alerts"] == ["listed_missing", "unknown"]
    assert Path(allow).read_bytes() == before


def test_host_id_entries_match_and_offline_since_is_tracked_without_last_seen(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    document = {
        "BackendState": "Running",
        "Self": {"ID": "n1", "DNSName": "laptop-a.example-tailnet.ts.net.", "Online": True},
        "Peer": {"k": {"ID": "n2", "HostName": "example-nas", "Online": False}},
    }
    allow = _allow(tmp_path, f"laptop-a\n{host_id('id:n2')}\n")
    code, _, report = _scan(tmp_path, document, "--allowlist", allow)
    assert code == 0 and report["counts"]["listed_offline"] == 0  # offline since now
    code, _, report = _scan(tmp_path, document, "--allowlist", allow, now=NOW + timedelta(hours=25))
    assert code == 3 and report["counts"]["listed_offline"] == 1  # since the first run
    document["Peer"]["k"]["Online"] = True
    code, _, report = _scan(tmp_path, document, "--allowlist", allow, now=NOW + timedelta(hours=26))
    assert code == 0 and report["counts"]["listed_offline"] == 0


def test_without_an_allowlist_alert_counts_are_null(tmp_path: Path) -> None:
    _vault(tmp_path)
    code, _, report = _scan(tmp_path, _status())
    assert code == 0 and report["allowlist"] is False
    assert report["counts"]["unknown"] is None and report["alerts"] == []
    assert report["counts"]["listed_ambiguous"] is None


def test_a_self_reported_hostname_never_satisfies_the_allowlist(tmp_path: Path) -> None:
    """A new machine calling itself a listed name must stay unknown (M2)."""

    _vault(tmp_path)
    document = {
        "BackendState": "Running",
        "Self": {"ID": "n1", "HostName": "laptop-a", "DNSName": "laptop-a.example-tailnet.ts.net."},
        "Peer": {
            # Impostor: HostName copies a listed machine; MagicDNS gave it another label.
            "k2": {
                "ID": "n2",
                "HostName": "example-nas",
                "DNSName": "example-nas-1.example-tailnet.ts.net.",
            },
        },
    }
    allow = _allow(tmp_path, "laptop-a\nexample-nas\n")
    code, _, report = _scan(tmp_path, document, "--allowlist", allow)
    counts = report["counts"]
    assert code == 3 and (counts["unknown"], counts["listed_missing"]) == (1, 1)


def test_full_dns_entries_compare_the_hashed_suffix(tmp_path: Path) -> None:
    _vault(tmp_path)
    document = {
        "BackendState": "Running",
        "Self": {"ID": "n1", "DNSName": "laptop-a.example-tailnet.ts.net."},
        "Peer": {"k2": {"ID": "n2", "DNSName": "laptop-a.other-tailnet.ts.net."}},
    }
    allow = _allow(tmp_path, "laptop-a.example-tailnet.ts.net\n")
    code, _, report = _scan(tmp_path, document, "--allowlist", allow, "--include-shared")
    counts = report["counts"]
    assert (counts["unknown"], counts["listed_missing"], counts["listed_ambiguous"]) == (1, 0, 0)


def test_an_entry_matching_several_hosts_is_ambiguous_and_vouches_for_none(
    tmp_path: Path,
) -> None:
    _vault(tmp_path)
    document = {
        "BackendState": "Running",
        "Self": {"ID": "n1", "DNSName": "laptop-a.example-tailnet.ts.net."},
        "Peer": {
            "k2": {"ID": "n2", "DNSName": "laptop-a.other-tailnet.ts.net.", "ShareeNode": True},
        },
    }
    allow = _allow(tmp_path, "laptop-a\n")
    code, _, report = _scan(tmp_path, document, "--allowlist", allow, "--include-shared")
    counts = report["counts"]
    assert (counts["listed_ambiguous"], counts["unknown"], counts["listed_missing"]) == (1, 2, 0)
    assert code == 3 and report["alerts"] == ["listed_ambiguous", "unknown"]


def test_shared_and_location_nodes_are_left_out_unless_opted_in(tmp_path: Path) -> None:
    _vault(tmp_path)
    code, _, report = _scan(tmp_path, _status())
    counts = report["counts"]
    assert (counts["hosts"], counts["skipped_shared"], counts["skipped_location"]) == (3, 1, 1)
    other = tmp_path / "opt-in"
    other.mkdir()
    _vault(other)
    code, _, report = _scan(other, _status(), "--include-shared")
    counts = report["counts"]
    assert (counts["hosts"], counts["skipped_shared"], counts["skipped_location"]) == (4, 0, 1)
    assert "canary-shared-host-41ac" in _by_change(other / "vault")["new"]
    assert parse_status(_status(), include_shared=True).skipped_location == 1
    assert factory({"include_shared": True}).settings.include_shared is True
    with pytest.raises(ValueError):
        factory({"include_shared": "yes"})


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("bad entry with spaces\n", "bad_allowlist"),
        ("ok\n" * 1 + "x" * 2_000_000, "allowlist_too_large"),
    ],
)
def test_bad_allowlists_are_refused(tmp_path: Path, text: str, code: str) -> None:
    _vault(tmp_path)
    rc, out, report = _scan(tmp_path, _status(), "--allowlist", _allow(tmp_path, text))
    assert (rc, out["error"], report["error"]) == (1, code, code)
    assert _candidates(tmp_path / "vault") == []


# -- input handling and the command line ----------------------------------------------------


def test_input_errors_and_argument_errors(tmp_path: Path) -> None:
    import contextlib
    import io

    state = str(tmp_path / "state")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli_run(["scan", "--input", str(tmp_path / "missing.json"), "--state", state])
    assert code == 1 and json.loads(buffer.getvalue())["error"] == "input_not_a_file"
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    code, out, _ = _run(["scan", "--input", str(big), "--state", state])
    assert (code, out["error"]) == (1, "status_too_large")
    for argv in (
        ["scan", "--state", state],  # neither --input nor --live
        ["scan", "--live", "--input", str(big), "--state", state],
        ["scan", "--live", "--state", state, "--offline-hours", "0"],
        ["scan", "--live", "--state", state, "--timeout", "500"],
        ["scan", "--live", "--state", state, "--privacy", "world"],
    ):
        with pytest.raises(SystemExit) as raised:
            cli_run(argv)
        assert raised.value.code == 2


def test_module_entry_point_runs(tmp_path: Path) -> None:
    source = _write(tmp_path / "status.json", _status())
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "memfw_adapter_tailnet",
            "scan",
            "--input",
            str(source),
            "--state",
            str(tmp_path / "state"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ADAPTER_SRC) + os.pathsep + str(ROOT / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["counts"]["hosts"] == 3


# -- live mode ------------------------------------------------------------------------------


def _fake_tailscale(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "bin" / "tailscale"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    script.chmod(0o700)
    return script


def test_live_mode_runs_the_fixed_argv_without_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = tmp_path / "seen.json"
    script = _fake_tailscale(
        tmp_path,
        f"""
        import json, os, sys
        json.dump({{"argv": sys.argv[1:], "env": sorted(os.environ), "stdin": sys.stdin.read()}},
                  open({str(record)!r}, "w"))
        sys.stdout.write(open({str(RECENT)!r}).read())
        """,
    )
    for name in ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "http://192.0.2.1:3128")
    monkeypatch.setenv("MEMFW_TAILNET_KEEP", "1")
    _vault(tmp_path)
    code, out, report = _run(
        [
            "scan",
            "--live",
            "--tailscale",
            str(script),
            "--state",
            str(tmp_path / "state"),
            "--vault",
            str(tmp_path / "vault"),
        ]
    )
    assert code == 0 and out["staged"] == 3 and report["mode"] == "live"
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert seen["argv"] == list(STATUS_ARGS) == ["status", "--json"]
    assert seen["stdin"] == ""
    lowered = {name.lower() for name in seen["env"]}
    assert not lowered & {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    assert "MEMFW_TAILNET_KEEP" in seen["env"]


def test_live_mode_uses_no_shell_and_bounded_settings() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    class FakeProcess:
        pid = 0

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            calls.append((argv, kwargs))
            import io

            self.stdout = io.BytesIO(RECENT.read_bytes())

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:  # pragma: no cover - not reached
            raise AssertionError

    data = run_tailscale(sys.executable, timeout=5, popen=FakeProcess)
    assert parse_status_bytes(data).hosts
    argv, kwargs = calls[0]
    assert argv == [sys.executable, "status", "--json"]
    assert kwargs["shell"] is False and kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL and kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert not {k.lower() for k in kwargs["env"]} & {"http_proxy", "https_proxy", "all_proxy"}


def test_live_mode_times_out_and_kills_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    script = _fake_tailscale(
        tmp_path,
        f"""
        import time
        time.sleep(30)
        open({str(marker)!r}, "w").write("x")
        """,
    )
    import time

    started = time.monotonic()
    with pytest.raises(TailnetError, match="tailscale_timeout"):
        run_tailscale(str(script), timeout=0.5)
    assert time.monotonic() - started < 5
    time.sleep(0.2)
    assert not marker.exists()


def test_live_mode_caps_the_output_and_reports_failures(tmp_path: Path) -> None:
    flood = _fake_tailscale(
        tmp_path, "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n"
    )
    with pytest.raises(TailnetError, match="status_too_large"):
        run_tailscale(str(flood), timeout=10, max_bytes=1 << 20)
    failing = tmp_path / "fail"
    failing.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    failing.chmod(0o700)
    with pytest.raises(TailnetError, match="tailscale_failed"):
        run_tailscale(str(failing), timeout=5)
    with pytest.raises(TailnetError, match="tailscale_not_found"):
        run_tailscale(str(tmp_path / "nope"), timeout=5)
    with pytest.raises(TailnetError, match="tailscale_not_found"):
        run_tailscale("memfw-no-such-tailscale-binary", timeout=5)
    with pytest.raises(TailnetError, match="bad_timeout"):
        run_tailscale(str(failing), timeout=0)


def test_live_failure_is_reported_and_commits_nothing(tmp_path: Path) -> None:
    stopped = copy.deepcopy(_status())
    stopped["BackendState"] = "Stopped"
    stopped["Peer"] = {}
    _vault(tmp_path)
    _scan(tmp_path, _status())
    snapshot = (tmp_path / "state" / "snapshot.json").read_bytes()
    code, out, report = _scan(tmp_path, stopped, now=NOW + timedelta(hours=1))
    assert (code, out["error"], report["error"]) == (
        1,
        "backend_not_running",
        "backend_not_running",
    )
    assert (tmp_path / "state" / "snapshot.json").read_bytes() == snapshot  # no mass "gone"


def test_child_env_strips_proxy_variables_in_any_case() -> None:
    env = child_env(
        {
            "HTTP_PROXY": "a",
            "Https_Proxy": "b",
            "all_proxy": "c",
            "NO_PROXY": "d",
            "PATH": "/bin",
            "LANG": "C",
        }
    )
    assert env == {"PATH": "/bin", "LANG": "C"}


def test_live_timeout_kills_grandchildren_too(tmp_path: Path) -> None:
    """The whole process group goes: a grandchild's heartbeat stops after the deadline."""

    beat = tmp_path / "beat"
    child = tmp_path / "heartbeat.py"
    child.write_text(
        f"import time\nwhile True:\n    open({str(beat)!r}, 'a').write('x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    script = _fake_tailscale(
        tmp_path,
        f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, {str(child)!r}])
        time.sleep(30)
        """,
    )
    import time

    with pytest.raises(TailnetError, match="tailscale_timeout"):
        run_tailscale(str(script), timeout=1.0)
    time.sleep(0.3)
    assert beat.exists()
    size = beat.stat().st_size
    time.sleep(0.4)
    assert beat.stat().st_size == size  # nothing is still writing
