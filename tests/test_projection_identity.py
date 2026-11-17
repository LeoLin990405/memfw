"""Out-of-vault projections record which vault they belong to.

The edge (K3) and corroboration (U4) projections live under the home
directory, keyed by the vault's resolved path. The path alone does not say
which vault built a file: delete a vault and ``memfw init`` a new one at the
same path, and the old file would answer for the new vault. Each file records
the vault identity (``memfw.core.local_data.vault_identity``) and every memfw
reader refuses a mismatch. Also: the corroboration projection moved out of the
vault (budget, fingerprint, legacy file), the K4 live check hides concepts the
vault no longer has, and the relation lint strips ``mem:``/``memory:``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memfw import corroboration, edges
from memfw.cli import app as cli_app
from memfw.conversations.policy import SharePolicy
from memfw.core.local_data import OTHER_VAULT, VaultIdentity, vault_identity
from memfw.core.repository import Repository
from memfw.corroboration import (
    CorroborationError,
    OtherVaultError,
    check_projection,
    corroboration_for,
)
from memfw.edges import EdgeError, SqliteProjectionStore, vault_store
from memfw.ontology.relations_lint import lint_relations
from memfw.runtime.corroboration import PROJECTION_OTHER_VAULT, caller_corroboration
from memfw.runtime.graph import GraphError, graph_match, rebuild_edge_projection

SHARE: dict[str, Any] = {"conversation_share": {"enabled": True}}
LONG = "The service listens on port eight thousand by default"


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    return home


def _vault(path: Path, share: bool = False) -> Repository:
    repository = Repository(path)
    repository.init()
    if share:
        config = repository.read_config()
        config.update(SHARE)
        repository.configure(config)
    return repository


def _rules(repository: Repository) -> SharePolicy:
    return SharePolicy.from_config(repository.read_config())


def _concept(root: Path, slug: str, oid: str, **extra: Any) -> Path:
    meta: dict[str, Any] = {
        "oid": oid,
        "class": "Concept",
        "pref_label": slug.replace("-", " "),
        "alt_labels": [],
        "same_as": [],
        "status": "active",
        "merged_into": None,
        **extra,
    }
    path = root / "concepts" / "Concept" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(meta, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n\n{slug}\n", encoding="utf-8")
    return path


def _rewrite(repository: Repository, memory_id: str, **front: Any) -> None:
    path = repository.memories_dir / f"{memory_id}.md"
    _, head, body = path.read_text(encoding="utf-8").split("---\n", 2)
    meta = yaml.safe_load(head)
    for key, value in front.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n" + body, "utf-8")


def _ids(payload: dict[str, Any]) -> list[list[str]]:
    return [[node["id"] for node in path["nodes"]] for path in payload["paths"]]


def _reinit(path: Path, share: bool = False) -> Repository:
    """``rm -rf path; memfw init path``: a new vault at the old vault's path."""

    shutil.rmtree(path)
    assert CliRunner().invoke(cli_app, ["init", str(path)]).exit_code == 0
    repository = Repository(path)
    if share:
        config = repository.read_config()
        config.update(SHARE)
        repository.configure(config)
    return repository


# --- the reviewer's scenario: edges --------------------------------------------------


def test_a_recreated_vault_never_sees_the_old_vaults_edges(tmp_path: Path) -> None:
    path = tmp_path / "vault"
    vault_a = _vault(path)
    vault_a.add("m-1", "a", memory_id="m-1", about=["concept:secret-project"], privacy="public")
    rebuild_edge_projection(vault_a)
    assert _ids(graph_match(vault_a, start="m-1", path="about>")) == [
        ["m-1", "concept:secret-project"]
    ]

    vault_b = _reinit(path)
    vault_b.add("m-1", "b", memory_id="m-1", privacy="public")
    with pytest.raises(GraphError, match=OTHER_VAULT):
        graph_match(vault_b, start="m-1", path="about>")
    runner = CliRunner()
    args = ["graph", "match", "-r", str(path), "--from", "m-1", "--path", "about>"]
    refused = runner.invoke(cli_app, args)
    assert refused.exit_code == 2
    assert OTHER_VAULT in refused.output and "secret-project" not in refused.output
    assert runner.invoke(cli_app, ["graph", "rebuild", "-r", str(path)]).exit_code == 0
    answered = runner.invoke(cli_app, args)
    assert answered.exit_code == 0, answered.output
    assert json.loads(answered.output)["paths"] == []


def test_the_projection_records_the_identity(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault")
    repository.add("m-1", "a", memory_id="m-1", privacy="public")
    rebuild_edge_projection(repository)
    with sqlite3.connect(edges.projection_path(repository.root)) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
    identity = vault_identity(repository)
    assert meta["schema_version"] == "2"
    assert meta["vault_path"] == str(repository.root.resolve()) == identity.path
    assert meta["vault_genesis"] == identity.genesis == repository.events()[0].event_hash
    assert meta["vault_id"] == ""


def test_a_schema_one_projection_is_refused(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault")
    repository.add("m-1", "a", memory_id="m-1", privacy="public")
    rebuild_edge_projection(repository)
    path = edges.projection_path(repository.root)
    with sqlite3.connect(path) as connection:
        for key in ("vault_path", "vault_id", "vault_genesis"):
            connection.execute("DELETE FROM meta WHERE key = ?", (key,))
        connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    with pytest.raises(GraphError, match="another schema version"):
        graph_match(repository, start="m-1", path="about>")


def test_a_store_without_identity_keeps_working(tmp_path: Path) -> None:
    """The port's contract (``SqliteProjectionStore`` built directly) is unchanged."""

    store = SqliteProjectionStore(tmp_path / "plain.sqlite3")
    assert store.rebuild([]).edges == 0
    assert store.version() is not None


# --- what the identity is ------------------------------------------------------------


def test_identity_survives_compaction_and_a_late_vault_id(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault")
    for number in range(6):
        repository.add(f"m-{number}", "body", memory_id=f"m-{number}", privacy="public")
    rebuild_edge_projection(repository)
    before = vault_identity(repository)
    repository.compact_log(keep_recent=1)
    assert repository.log_catalog_path.is_file()
    assert vault_identity(repository) == before  # genesis read from the catalog
    repository.ensure_vault_id()
    late = vault_identity(repository)
    assert late.vault_id and late.genesis == before.genesis
    assert late.matches(before.meta())  # no vault_id at build: the genesis decides
    assert graph_match(repository, start="m-1", path="about>")["paths"] == []


def test_identity_rules() -> None:
    built = VaultIdentity("/v", "id-a", "g").meta()
    assert VaultIdentity("/v", "id-a", "g").matches(built)
    assert not VaultIdentity("/other", "id-a", "g").matches(built)
    assert not VaultIdentity("/v", "id-b", "g").matches(built)  # both ids: the ids decide
    assert VaultIdentity("/v", "", "g").matches(built)  # one id missing: the genesis decides
    assert not VaultIdentity("/v", "", "h").matches(built)
    assert not VaultIdentity("/v", "", "").matches({"vault_path": "/v"})  # no genesis key


def test_another_vault_id_is_refused(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault")
    repository.ensure_vault_id()
    repository.add("m-1", "a", memory_id="m-1", privacy="public")
    rebuild_edge_projection(repository)
    config = repository.read_config()
    config["vault_id"] = "00000000-0000-4000-8000-000000000000"
    repository.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(GraphError, match=OTHER_VAULT):
        graph_match(repository, start="m-1", path="about>")


def test_a_symlinked_vault_shares_the_projection(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault")
    repository.add("m-1", "a", memory_id="m-1", about=["concept:x"], privacy="public")
    rebuild_edge_projection(repository)
    link = tmp_path / "link"
    link.symlink_to(repository.root)
    via_link = Repository(link)
    assert edges.projection_path(via_link.root) == edges.projection_path(repository.root)
    assert _ids(graph_match(via_link, start="m-1", path="about>")) == [["m-1", "concept:x"]]


def test_a_vault_at_home_cannot_be_rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "homevault"
    monkeypatch.setenv("HOME", str(home))
    repository = _vault(home)
    with pytest.raises(EdgeError, match="inside the vault"):
        rebuild_edge_projection(repository)
    with pytest.raises(CorroborationError, match="inside the vault"):
        corroboration.rebuild(repository, _rules(repository))


# --- K4 live check: a concept the vault no longer has is hidden ---------------------


def test_live_check_hides_concepts_the_vault_no_longer_has(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault")
    _concept(repository.root, "food", "concept:food")
    _concept(repository.root, "gone", "concept:gone", broader=["concept:food"])
    repository.add(
        "m-1", "a", memory_id="m-1", about=["concept:food", "secret-project"], privacy="public"
    )
    repository.add("m-2", "b", memory_id="m-2", about=["unfiled"], privacy="public")
    rebuild_edge_projection(repository)
    assert _ids(graph_match(repository, start="m-1", path="about>")) == [
        ["m-1", "concept:food"],
        ["m-1", "secret-project"],
    ]
    assert _ids(graph_match(repository, start="concept:gone", path="broader>")) == [
        ["concept:gone", "concept:food"]
    ]
    # Without a rebuild: m-1 no longer names secret-project, concept:gone's file is gone.
    _rewrite(repository, "m-1", about=["concept:food"])
    (repository.root / "concepts" / "Concept" / "gone.md").unlink()
    assert _ids(graph_match(repository, start="m-1", path="about>")) == [["m-1", "concept:food"]]
    assert graph_match(repository, start="secret-project", path="<about")["paths"] == []
    assert graph_match(repository, start="concept:gone", path="broader>")["paths"] == []
    assert graph_match(repository, start="concept:food", path="<broader")["paths"] == []
    # A concept node a visible memory still names stays, file or no file.
    assert _ids(graph_match(repository, start="m-2", path="about>")) == [["m-2", "unfiled"]]
    # ... but not when only a memory the caller cannot see names it.
    _rewrite(repository, "m-2", privacy="local-private")
    repository.add("m-3", "c", memory_id="m-3", privacy="public")
    assert graph_match(repository, start="unfiled", path="<about")["paths"] == []


# --- corroboration: outside the vault, identity, legacy file -------------------------


def _corroborated(repository: Repository) -> str:
    memory = repository.add(
        LONG,
        "body",
        memory_id="m-1",
        evidence=["conv:claude:c1@1", "conv:codex:x1@1"],
        privacy="public",
    )
    return memory.id


def test_corroboration_lives_outside_the_vault(tmp_path: Path, _home: Path) -> None:
    repository = _vault(tmp_path / "vault", share=True)
    _corroborated(repository)
    corroboration.rebuild(repository, _rules(repository))
    path = corroboration.projection_path(repository.root)
    assert path.parent == _home / ".local" / "share" / "memfw" / "corroboration"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not (repository.root / "corroboration.sqlite3").exists()
    assert not list(repository.root.glob("*corroboration*"))
    found, degraded = caller_corroboration(repository, ["m-1"], app="claude")
    assert degraded is None and found["m-1"]["count"] == 2


def test_a_recreated_vault_never_sees_the_old_corroboration(tmp_path: Path) -> None:
    path = tmp_path / "vault"
    vault_a = _vault(path, share=True)
    _corroborated(vault_a)
    corroboration.rebuild(vault_a, _rules(vault_a))
    vault_b = _reinit(path, share=True)
    vault_b.add(LONG, "body", memory_id="m-1", privacy="public")
    found, degraded = caller_corroboration(vault_b, ["m-1"], app="claude")
    assert (found, degraded) == ({}, PROJECTION_OTHER_VAULT)
    projection = corroboration.projection_path(vault_b.root)
    with pytest.raises(OtherVaultError, match=OTHER_VAULT):
        check_projection(projection, vault_identity(vault_b))
    with pytest.raises(OtherVaultError):
        corroboration_for(
            ["m-1"],
            caller_app="claude",
            rules=_rules(vault_b),
            path=projection,
            identity=vault_identity(vault_b),
        )
    runner = CliRunner()
    root = str(path)
    shown = runner.invoke(cli_app, ["corroboration", "show", "m-1", "--root", root])
    assert shown.exit_code == 2 and OTHER_VAULT in shown.output
    board = runner.invoke(
        cli_app, ["project", "obsidian", "--root", root, "--out", str(tmp_path / "board")]
    )
    assert board.exit_code == 0, board.output
    assert OTHER_VAULT in board.stderr
    assert "corroboration" not in (tmp_path / "board" / "memories" / "m-1.md").read_text()
    assert runner.invoke(cli_app, ["corroboration", "rebuild", "--root", root]).exit_code == 0
    found, degraded = caller_corroboration(vault_b, ["m-1"], app="claude")
    assert degraded is None and found["m-1"]["count"] == 0


def test_a_legacy_vault_root_file_is_ignored_noted_and_kept(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault", share=True)
    _corroborated(repository)
    legacy = corroboration.legacy_path(repository.root)
    legacy.write_bytes(b"not a projection")
    runner = CliRunner()
    built = runner.invoke(cli_app, ["corroboration", "rebuild", "--root", str(repository.root)])
    assert built.exit_code == 0, built.output
    payload = json.loads(built.stdout)
    assert payload["legacy_file"] == "corroboration.sqlite3"
    assert payload["path"] == str(corroboration.projection_path(repository.root))
    assert "you may delete it" in built.stderr
    assert legacy.read_bytes() == b"not a projection"  # never read, deleted or rewritten
    shown = runner.invoke(cli_app, ["corroboration", "show", "m-1", "--root", str(repository.root)])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["corroboration"]["count"] == 2
    legacy.unlink()
    again = runner.invoke(cli_app, ["corroboration", "rebuild", "--root", str(repository.root)])
    assert "legacy_file" not in json.loads(again.stdout)


def test_a_large_corroboration_projection_never_drives_total_budget_forgetting(
    tmp_path: Path,
) -> None:
    from memfw.curation.engine import enforce_budget
    from memfw.stats import measure_physical_disk_bytes

    repository = _vault(tmp_path / "vault", share=True)
    for number in range(20):
        repository.add(f"memory {number}", "body", memory_id=f"m-{number:02d}")
    physical = measure_physical_disk_bytes(repository.root)
    config = repository.read_config()
    config.update(
        budget_mode="total",
        max_bytes=physical + 64 * 1024,
        forget_action="hard",
        budget_scope="all",
    )
    repository.configure(config)
    physical = measure_physical_disk_bytes(repository.root)
    fingerprint = repository._tree_fingerprint()
    sessions = {(f"app{n % 50}", f"s-{n:06d}-" + "p" * 150): (None, None) for n in range(40_000)}
    data = {f"m-{number:02d}": sessions for number in range(1)}
    original = corroboration.collect
    try:
        corroboration.collect = lambda _repository, _rules: data  # type: ignore[assignment]
        corroboration.rebuild(repository, _rules(repository))
    finally:
        corroboration.collect = original  # type: ignore[assignment]
    assert corroboration.projection_path(repository.root).stat().st_size > 4 * 1024 * 1024
    assert measure_physical_disk_bytes(repository.root) == physical
    assert repository._tree_fingerprint() == fingerprint
    result = enforce_budget(repository, now=datetime(2026, 11, 17, 9, 0, tzinfo=UTC))
    assert result.applied_ids == ()
    assert len(repository.list()) == 20


def test_rebuilds_during_vault_writes_never_fail(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault", share=True)
    _corroborated(repository)
    errors: list[BaseException] = []
    done = threading.Event()

    def write() -> None:
        try:
            writer = Repository(repository.root)
            for number in range(25):
                writer.add(
                    f"written {number}",
                    "body",
                    memory_id=f"w-{number:02d}",
                    evidence=[f"conv:claude:w{number}@1"],
                    about=["concept:busy"],
                    privacy="public",
                )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=write)
    thread.start()
    rounds = 0
    while not done.is_set() or rounds < 3:
        corroboration.rebuild(repository, _rules(repository))
        rebuild_edge_projection(repository)
        assert caller_corroboration(repository, ["m-1"], app="claude")[1] is None
        graph_match(repository, start="m-1", path="about>")
        rounds += 1
    thread.join()
    assert errors == []
    corroboration.rebuild(repository, _rules(repository))
    found, _ = caller_corroboration(repository, ["w-24"], app="claude")
    assert found["w-24"]["count"] == 1
    rebuild_edge_projection(repository)
    assert len(graph_match(repository, start="concept:busy", path="<about")["paths"]) == 20


# --- relation lint strips memory prefixes like the edge builder ----------------------


def test_lint_strips_memory_prefixes(tmp_path: Path) -> None:
    repository = _vault(tmp_path / "vault")
    repository.add("new", "b", memory_id="m-new", privacy="public")
    repository.add("other", "b", memory_id="m-other", tenant="other", privacy="public")
    repository.add(
        "old",
        "b",
        memory_id="m-old",
        evidence=["memory:m-other"],
        privacy="public",
    )
    _rewrite(repository, "m-old", superseded_by="mem:m-new")
    items = lint_relations(repository.root)
    assert not [item for item in items if item["kind"] == "relation_dangling"]
    assert {
        (item["relation"], item["detail"])
        for item in items
        if item["kind"] == "relation_cross_tenant"
    } == {("evidence", "m-other")}
    assert vault_store(repository).identity == vault_identity(repository)


# --- review follow-ups (#90) ---------------------------------------------------------


def test_free_text_concepts_never_list_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1: a free-text concept node is confirmed through its own edges, O(degree)."""

    repository = _vault(tmp_path / "vault")
    for number in range(300):
        repository.add(
            f"m{number}",
            "b",
            memory_id=f"m-{number:03d}",
            about=[f"free text {number % 30}"],
            privacy="public",
        )
    rebuild_edge_projection(repository)
    calls = {"list": 0, "get": 0}
    real_list, real_get = Repository.list, Repository.get

    def counting_list(self: Repository) -> Any:
        calls["list"] += 1
        return real_list(self)

    def counting_get(self: Repository, memory_id: str) -> Any:
        calls["get"] += 1
        return real_get(self, memory_id)

    monkeypatch.setattr(Repository, "list", counting_list)
    monkeypatch.setattr(Repository, "get", counting_get)
    assert _ids(graph_match(repository, start="m-001", path="about>")) == [["m-001", "free text 1"]]
    paths = graph_match(repository, start="m-001", path="about><about", limit=100)["paths"]
    assert len(paths) == 9  # the 10 memories about "free text 1", less the start
    assert calls["list"] == 0
    assert calls["get"] <= 20  # the start, its concept's 10 memories: each read once


def test_a_read_only_vault_can_still_be_rebuilt(tmp_path: Path) -> None:
    """M2: no writable lock file: read unlocked between two fingerprints."""

    repository = _vault(tmp_path / "vault", share=True)
    _corroborated(repository)
    repository.add("m-2", "b", memory_id="m-2", about=["concept:x"], privacy="public")
    (repository.root / ".memfw.lock").unlink(missing_ok=True)
    repository.root.chmod(0o555)
    try:
        reader = Repository(repository.root)
        assert rebuild_edge_projection(reader)["edges"] > 0
        assert corroboration.rebuild(reader, _rules(reader)) == {"memories": 1, "mentions": 2}
        runner = CliRunner()
        root = str(repository.root)
        assert runner.invoke(cli_app, ["graph", "rebuild", "-r", root]).exit_code == 0
        built = runner.invoke(cli_app, ["corroboration", "rebuild", "--root", root])
        assert built.exit_code == 0, built.output
        assert _ids(graph_match(reader, start="m-2", path="about>")) == [["m-2", "concept:x"]]
        assert not (repository.root / ".memfw.lock").exists()
    finally:
        repository.root.chmod(0o755)


def test_an_unlocked_read_retries_while_the_vault_changes(tmp_path: Path) -> None:
    from memfw.core.local_data import VaultBusyError, read_vault

    class Host:
        def __init__(self, prints: list[str]) -> None:
            self.prints = iter(prints)

        def _locked(self) -> Any:
            raise PermissionError(13, "Permission denied")

        def _tree_fingerprint(self) -> str:
            return next(self.prints)

    reads: list[int] = []
    # Changes during the first read, steady during the second.
    assert read_vault(Host(["a", "b", "b", "b"]), lambda: reads.append(1) or 7) == 7
    assert len(reads) == 2
    with pytest.raises(VaultBusyError, match="try again"):
        read_vault(Host(["a", "b", "c", "d", "e", "f"]), lambda: 1)

    class Broken(Host):
        def _locked(self) -> Any:
            raise OSError(5, "I/O error")

    with pytest.raises(OSError, match="I/O error"):
        read_vault(Broken([]), lambda: 1)
