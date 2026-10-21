from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from memfw.cli import app

runner = CliRunner()
NOW = "2026-01-31T12:00:00+00:00"
ID_SEED = "cli-day2-deterministic-seed"


def invoke(root: Path, *args: str) -> Result:
    return runner.invoke(app, ["--now", NOW, "--id-seed", ID_SEED, *args])


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def tree_hashes(root: Path) -> dict[str, str]:
    return {path: hashlib.sha256(data).hexdigest() for path, data in tree_bytes(root).items()}


def make_vault(root: Path) -> tuple[str, str]:
    result = invoke(root, "init", str(root))
    assert result.exit_code == 0, result.output
    hot_id = "hot-memory"
    cold_id = "cold-memory"
    for memory_id, tier in ((hot_id, "hot"), (cold_id, "cold")):
        result = invoke(
            root,
            "add",
            "--title",
            f"Title {memory_id}",
            "--body",
            f"Body for {memory_id}",
            "--root",
            str(root),
            "--id",
            memory_id,
            "--tier",
            tier,
        )
        assert result.exit_code == 0, result.output
    return hot_id, cold_id


def test_fixed_now_and_seed_produce_identical_full_vault_trees(tmp_path: Path) -> None:
    roots = [tmp_path / "vault-a", tmp_path / "vault-b"]
    normalized_outputs: list[list[str]] = []

    for root in roots:
        init_result = invoke(root, "init", str(root))
        assert init_result.exit_code == 0, init_result.output
        outputs: list[str] = []
        outputs.append(init_result.output.replace(str(root.resolve()), "<VAULT>").strip())

        add_result = invoke(
            root,
            "add",
            "--title",
            "Deterministic note",
            "--body",
            "A stable body for CLI replay.",
            "--root",
            str(root),
            "--id",
            "deterministic-note",
        )
        assert add_result.exit_code == 0, add_result.output
        outputs.append(add_result.output.strip())

        update_result = invoke(
            root,
            "update",
            "deterministic-note",
            "--root",
            str(root),
            "--body",
            "Updated stable body for CLI replay.",
        )
        assert update_result.exit_code == 0, update_result.output
        outputs.append(update_result.output.strip())

        index_result = invoke(root, "index", "rebuild", "--root", str(root))
        assert index_result.exit_code == 0, index_result.output
        outputs.append(index_result.output.strip())

        forget_result = invoke(
            root,
            "forget",
            "run",
            "--policy",
            "fifo",
            "--root",
            str(root),
            "--dry-run",
        )
        assert forget_result.exit_code == 0, forget_result.output
        outputs.append(forget_result.output.strip())

        layer_result = invoke(
            root,
            "layer-set",
            "deterministic-note",
            "consolidated",
            "--root",
            str(root),
            "--apply",
        )
        assert layer_result.exit_code == 0, layer_result.output
        outputs.append(layer_result.output.strip())

        for command in (("tiers", "status"), ("log", "verify")):
            result = invoke(root, *command, "--root", str(root))
            assert result.exit_code == 0, result.output
            outputs.append(result.output.strip())

        normalized_outputs.append(outputs)

    assert tree_bytes(roots[0]) == tree_bytes(roots[1])
    assert normalized_outputs[0] == normalized_outputs[1]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("init", lambda root, hot, cold: ("init", str(root), "--dry-run")),
        (
            "add",
            lambda root, hot, cold: (
                "add",
                "--title",
                "Dry run",
                "--body",
                "No write",
                "--root",
                str(root),
                "--id",
                "dry-run-added",
                "--dry-run",
            ),
        ),
        (
            "update",
            lambda root, hot, cold: (
                "update",
                hot,
                "--root",
                str(root),
                "--title",
                "Changed",
                "--dry-run",
            ),
        ),
        ("delete", lambda root, hot, cold: ("delete", hot, "--root", str(root), "--dry-run")),
        (
            "rollback",
            lambda root, hot, cold: (
                "rollback",
                "nonexistent-event",
                "--root",
                str(root),
                "--dry-run",
            ),
        ),
        (
            "cold-archive",
            lambda root, hot, cold: (
                "cold-archive",
                "dry-archive",
                "--root",
                str(root),
                "--dry-run",
            ),
        ),
        (
            "cold-restore",
            lambda root, hot, cold: ("cold-restore", cold, "--root", str(root), "--dry-run"),
        ),
        (
            "record-proposal",
            lambda root, hot, cold: (
                "record-proposal",
                hot,
                "--operation",
                "update",
                "--rationale",
                "test",
                "--payload",
                json.dumps({"memory_id": hot, "operation": "update", "changes": {"title": "x"}}),
                "--agent-id",
                "agent",
                "--signature",
                "sig",
                "--root",
                str(root),
                "--dry-run",
            ),
        ),
        ("fsck", lambda root, hot, cold: ("fsck", "--root", str(root), "--dry-run")),
        ("recover", lambda root, hot, cold: ("recover", "--root", str(root), "--dry-run")),
        (
            "index-rebuild",
            lambda root, hot, cold: ("index", "rebuild", "--root", str(root), "--dry-run"),
        ),
        (
            "layer-set",
            lambda root, hot, cold: ("layer-set", hot, "anchor", "--root", str(root), "--dry-run"),
        ),
        (
            "forget-run",
            lambda root, hot, cold: (
                "forget",
                "run",
                "--policy",
                "fifo",
                "--root",
                str(root),
                "--dry-run",
            ),
        ),
    ],
)
def test_mutation_dry_runs_are_supported_and_leave_tree_unchanged(
    tmp_path: Path,
    name: str,
    arguments: object,
) -> None:
    root = tmp_path / name
    hot, cold = make_vault(root)
    before = tree_hashes(root)

    result = invoke(root, *arguments(root, hot, cold))  # type: ignore[operator]

    assert result.exit_code == 0, f"{name} dry-run failed:\n{result.output}"
    assert tree_hashes(root) == before, f"{name} --dry-run changed vault files"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("init", lambda root: ("init", str(root), "--dry-run")),
        (
            "add",
            lambda root: (
                "add",
                "--title",
                "Missing vault",
                "--body",
                "No write",
                "--root",
                str(root),
                "--dry-run",
            ),
        ),
        ("index-rebuild", lambda root: ("index", "rebuild", "--root", str(root), "--dry-run")),
        (
            "forget-run",
            lambda root: ("forget", "run", "--policy", "fifo", "--root", str(root), "--dry-run"),
        ),
    ],
)
def test_dry_run_never_creates_a_missing_vault(
    tmp_path: Path, name: str, arguments: object
) -> None:
    root = tmp_path / f"missing-{name}"

    invoke(root, *arguments(root))  # type: ignore[operator]

    assert not root.exists(), f"{name} dry-run created {root}"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("budget show", lambda root: ("budget", "show", "--root", str(root))),
        (
            "forget run",
            lambda root: ("forget", "run", "--policy", "fifo", "--root", str(root), "--dry-run"),
        ),
        ("tier status", lambda root: ("tiers", "status", "--root", str(root))),
        ("tier status alias", lambda root: ("tier", "status", "--root", str(root))),
        (
            "layer-set",
            lambda root: (
                "layer-set",
                "smoke-memory",
                "consolidated",
                "--root",
                str(root),
                "--dry-run",
            ),
        ),
        ("stats JSON", lambda root: ("stats", "--root", str(root))),
        ("log verify", lambda root: ("log", "verify", "--root", str(root))),
    ],
)
def test_day2_cli_routes_smoke_with_json_output(
    tmp_path: Path, name: str, arguments: object
) -> None:
    root = tmp_path / "smoke-vault"
    make_vault(root)
    if name == "layer-set":
        result = invoke(
            root,
            "add",
            "--title",
            "Smoke",
            "--body",
            "Body",
            "--root",
            str(root),
            "--id",
            "smoke-memory",
        )
        assert result.exit_code == 0, result.output

    result = invoke(root, *arguments(root))  # type: ignore[operator]

    assert result.exit_code == 0, f"{name} route failed:\n{result.output}"
    json.loads(result.output)


def test_layer_set_nested_route_is_available(tmp_path: Path) -> None:
    root = tmp_path / "layer-route-vault"
    make_vault(root)

    result = invoke(
        root,
        "layer",
        "set",
        "hot-memory",
        "consolidated",
        "--root",
        str(root),
        "--dry-run",
    )

    assert result.exit_code == 0, f"layer set route failed:\n{result.output}"
    assert json.loads(result.output) == {
        "memory_id": "hot-memory",
        "before": "current",
        "after": "consolidated",
    }


def test_forget_apply_scope_all_archives_cold_stub_and_keeps_replay_index_consistent(
    tmp_path: Path,
) -> None:
    from memfw.core.repository import Repository
    from memfw.index import MemoryIndex

    root = tmp_path / "forget-apply-vault"
    _, _ = make_vault(root)
    configured = invoke(
        root,
        "budget",
        "configure",
        "--level",
        "1MB",
        "--max-count",
        "1",
        "--root",
        str(root),
        "--apply",
    )
    assert configured.exit_code == 0, configured.output

    repository = Repository(root)
    index = MemoryIndex(repository.index_path)
    index.rebuild(repository)
    result = invoke(
        root,
        "forget",
        "run",
        "--policy",
        "fifo",
        "--scope",
        "all",
        "--action",
        "cold",
        "--apply",
        "--root",
        str(root),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["limits"]["max_count"] == 1
    assert len(payload["decisions"]) == 2, "scope=all must consider hot and cold records"
    assert payload["forgotten_ids"] == ["cold-memory"]

    # A cold forget must leave a real, searchable stub. Rebuild the disposable
    # projection and check that replay, Markdown and index agree on terminal state.
    cold = repository.get("cold-memory")
    assert cold.tier.value == "cold"
    assert cold.is_stub, "forget --apply cold must archive the forgotten body as a stub"
    assert cold.body == ""
    assert cold.archive_ref

    replay = repository.replay()
    documents = repository.list()
    current_hashes = {
        document.id: hashlib.sha256(repository._current_path(document.id).read_bytes()).hexdigest()
        for document in documents
    }
    assert replay == current_hashes

    index.rebuild(repository)
    indexed = {row[0]: row for row in index.rows()}
    assert set(indexed) == {document.id for document in documents}
    assert indexed["cold-memory"][2] == ""
    assert indexed["cold-memory"][11] == 1
    assert indexed["cold-memory"][12] == cold.archive_ref
    assert index.search("Body for cold-memory") == [
        ("cold-memory", "Title cold-memory"),
    ]
