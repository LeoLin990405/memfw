"""``runtime.recall`` wiring: ``lifecycle_source`` and the ``kh:`` notes resolver.

Production keeps the lifecycle meta-index outside the vault
(``<base>/status/memory-meta-index.json``), so the Runtime takes its path from
``vault.yaml``; and the notes index is registered as the ``kh:`` resolver of
``mode=two_stage``. Both default off: without the keys every output is as
before (``test_wiring_defaults_are_byte_identical_to_base_ref``).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memfw.core.epistemic import OPEN_QUESTION_TYPE
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.notes import NotesIndex
from memfw.runtime import build_runtime
from memfw.runtime.recall_adapters import RecallAdapterError
from memfw.runtime.recall_wiring import NotesWiring, RecallWiring, parse_wiring

NOW = datetime.now(UTC)
PAST = (NOW - timedelta(days=3)).isoformat()
ABOUT = "concept:backup-target"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _append_config(repository: Repository, recall: dict[str, Any]) -> None:
    path = repository.root / "vault.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "runtime:\n  recall: " + json.dumps(recall) + "\n",
        encoding="utf-8",
    )


def _meta_index(path: Path, items: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"generated_at": "2026-11-17T00:00:00Z", "items": items}), "utf-8")
    return path


def _imported_vault(tmp_path: Path) -> tuple[Repository, Path]:
    """Imported memories (``fleet_path`` tags) and an out-of-vault meta-index."""

    repository = Repository(tmp_path / "vault")
    repository.init()
    for memory_id in ("m-keep", "m-retired", "m-expired", "m-flag"):
        repository.add(
            f"Backup {memory_id}",
            "backup target notes",
            memory_id=memory_id,
            memory_type="procedural",  # resident in the ``core`` context slot
            about=[ABOUT],
            tags=[f"fleet_path:{memory_id}.md"],
        )
    repository.add(
        "Backup target unknown",
        "Two memories disagree on the backup target.",
        memory_id="q-retired",
        memory_type=OPEN_QUESTION_TYPE,
        about=[ABOUT],
        tags=["fleet_path:q-retired.md"],
        open_question={"question": "Which backup target?", "conflicts": ["m-keep"]},
    )
    repository.add(
        "Backup target open",
        "Which target holds now?",
        memory_id="q-open",
        memory_type=OPEN_QUESTION_TYPE,
        about=[ABOUT],
        open_question={"question": "Which target holds now?", "conflicts": ["m-keep"]},
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    outside = _meta_index(
        tmp_path / "base" / "status" / "memory-meta-index.json",
        [
            {"path": "m-keep.md", "lifecycle": "active"},
            {"path": "m-retired.md", "lifecycle": "retired"},
            {"path": "m-expired.md", "valid_until": PAST},
            {"path": "m-flag.md", "lifecycle": "active", "lifecycle_override": True},
            {"path": "q-retired.md", "lifecycle": "retired"},
        ],
    )
    return repository, outside


def _ids(payload: dict[str, Any]) -> set[str]:
    return {row["id"] for row in payload["results"]}


# --- parsing -----------------------------------------------------------------------


def test_parse_defaults_and_paths(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    assert parse_wiring({}, root=root) == RecallWiring()
    assert parse_wiring({"runtime": {}}, root=root) == RecallWiring()
    assert parse_wiring({"runtime": {"recall": {"adapters": []}}}, root=root) == RecallWiring()
    wiring = parse_wiring(
        {
            "runtime": {
                "recall": {
                    "lifecycle_source": "../status/memory-meta-index.json",
                    "notes": {"root": "~/notes", "index": "/srv/notes.sqlite3"},
                }
            }
        },
        root=root,
    )
    assert wiring.lifecycle_source == root / ".." / "status" / "memory-meta-index.json"
    assert wiring.notes == NotesWiring(
        root=Path("~/notes").expanduser(), index=Path("/srv/notes.sqlite3")
    )
    shared = parse_wiring(
        {
            "runtime": {
                "recall": {
                    "notes": {
                        "root": "n",
                        "index": "n.db",
                        "legacy_privacy": "shared",
                        "tenant": "t",
                    }
                }
            }
        },
        root=root,
    )
    assert shared.notes == NotesWiring(root / "n", root / "n.db", "shared", "t")


@pytest.mark.parametrize(
    ("recall", "code"),
    [
        ({"lifecycle_source": ""}, "bad_lifecycle_source"),
        ({"lifecycle_source": 3}, "bad_lifecycle_source"),
        ({"lifecycle_source": ["a"]}, "bad_lifecycle_source"),
        ({"notes": "n"}, "bad_notes"),
        ({"notes": {"root": "n"}}, "bad_notes"),
        ({"notes": {"root": "n", "index": "i", "other": 1}}, "bad_notes"),
        ({"notes": {"root": "n", "index": "i", "legacy_privacy": "secret"}}, "bad_notes"),
        ({"notes": {"root": "n", "index": "i", "tenant": 1}}, "bad_notes"),
    ],
)
def test_parse_refuses_malformed_values(tmp_path: Path, recall: Any, code: str) -> None:
    with pytest.raises(RecallAdapterError) as caught:
        parse_wiring({"runtime": {"recall": recall}}, root=tmp_path)
    assert caught.value.code == code


def test_a_malformed_setting_fails_the_call(tmp_path: Path) -> None:
    repository, _outside = _imported_vault(tmp_path)
    _append_config(repository, {"lifecycle_source": 7})
    runtime = build_runtime(repository.root)
    with pytest.raises(RecallAdapterError):
        runtime.search("backup", limit=10, mode="two_stage", policy="strict")
    with pytest.raises(RecallAdapterError):
        runtime.context_pack(repository, policy_name="strict", limit_tokens=2000)


# --- lifecycle_source -----------------------------------------------------------------


def test_two_stage_reads_the_configured_out_of_vault_meta_index(tmp_path: Path) -> None:
    repository, outside = _imported_vault(tmp_path)
    unset = build_runtime(repository.root)
    strict = {"limit": 10, "mode": "two_stage", "policy": "strict"}
    everything = {"m-keep", "m-retired", "m-expired", "m-flag"}
    # No default file in the vault: the vault alone decides, as before.
    assert _ids(unset.search("backup target", **strict)) >= everything
    _append_config(repository, {"lifecycle_source": str(outside)})
    runtime = build_runtime(repository.root)
    found = _ids(runtime.search("backup target", **strict))
    assert found & everything == {"m-keep", "m-flag"}  # the boolean flag is not a lifecycle
    # compat_fts reads no meta-index field.
    assert _ids(runtime.search("backup target", limit=10, mode="two_stage")) >= everything


def test_a_configured_meta_index_that_goes_missing_fails_closed(tmp_path: Path) -> None:
    repository, outside = _imported_vault(tmp_path)
    _append_config(repository, {"lifecycle_source": str(outside)})
    outside.unlink()
    runtime = build_runtime(repository.root)
    found = _ids(runtime.search("backup target", limit=10, mode="two_stage", policy="strict"))
    assert not found & {"m-keep", "m-retired", "m-expired", "m-flag"}
    assert "q-open" in found  # not imported: unaffected


def test_context_strict_uses_the_meta_index_only_when_configured(tmp_path: Path) -> None:
    repository, outside = _imported_vault(tmp_path)

    def core_ids(runtime: Any) -> set[str]:
        pack = runtime.context_pack(repository, policy_name="strict", limit_tokens=8000)
        return {entry.id for slot in pack.slots for entry in slot.entries}

    before = core_ids(build_runtime(repository.root, context_cache=False))
    assert {"m-retired", "m-expired"} <= before  # vault-only reading, as before
    _append_config(repository, {"lifecycle_source": str(outside)})
    runtime = build_runtime(repository.root, context_cache=False)
    after = core_ids(runtime)
    assert not after & {"m-retired", "m-expired", "q-retired"}
    assert "m-keep" in after and "m-flag" in after
    keep = runtime.context_keep("strict", now=NOW)  # no repository: opened here
    assert not keep(repository.get("m-retired")) and keep(repository.get("m-keep"))


def test_open_question_queue_and_labels_use_the_meta_index_only_when_configured(
    tmp_path: Path,
) -> None:
    repository, outside = _imported_vault(tmp_path)

    def queue_ids(runtime: Any) -> list[str]:
        payload = runtime.open_question_queue(repository, app="operator", allow_operator=True)
        return sorted(item["id"] for item in payload["questions"])

    def labels(runtime: Any) -> tuple[str, ...]:
        found = runtime.open_question_labels(
            repository,
            ["m-keep"],
            policy_name="strict",
            source="",
            include_history=False,
            as_of=None,
        )
        return tuple(found.get("m-keep", ()))

    unset = build_runtime(repository.root)
    assert queue_ids(unset) == ["q-open", "q-retired"]
    assert "open_question:q-retired" in labels(unset)
    _append_config(repository, {"lifecycle_source": str(outside)})
    runtime = build_runtime(repository.root)
    assert queue_ids(runtime) == ["q-open"]
    assert "open_question:q-retired" not in labels(runtime)
    assert "open_question:q-open" in labels(runtime)


# --- the kh: notes resolver -------------------------------------------------------------


def _notes(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "notes"
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "restore.md").write_text(
        "---\nprivacy: public\n---\n# Restore drills\nRun the restore drill monthly.\n",
        encoding="utf-8",
    )
    (root / "ops" / "secret.md").write_text(
        "---\nprivacy: local-private\n---\n# Keys\nThe vault keys live offline.\n",
        encoding="utf-8",
    )
    db = tmp_path / "notes.sqlite3"
    NotesIndex(db).index(root)
    return root, db


def _kh_vault(tmp_path: Path, notes: dict[str, Any] | None) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    repository.add("Restore drill", "restore drill memory", memory_id="m-restore")
    MemoryIndex(repository.index_path).rebuild(repository)
    recall: dict[str, Any] = {
        "adapters": [
            {
                "factory": "tests.recall_adapter_stub:refs_factory",
                "options": {
                    "name": "kh-stub",
                    "refs": [
                        "kh:ops/restore.md#Restore%20drills",
                        "kh:ops/secret.md#Keys",
                        "kh:ops/missing.md",
                    ],
                },
            }
        ]
    }
    if notes is not None:
        recall["notes"] = notes
    _append_config(repository, recall)
    return repository


def test_kh_refs_resolve_to_note_sections_when_a_notes_index_is_configured(
    tmp_path: Path,
) -> None:
    root, db = _notes(tmp_path)
    repository = _kh_vault(tmp_path, {"root": str(root), "index": str(db)})
    payload = build_runtime(repository.root).search("restore drill", limit=10, mode="two_stage")
    assert _ids(payload) == {"m-restore"}  # memory hits stay apart
    notes = payload["notes"]
    found = [(row["path"], row["heading"]) for row in notes]
    assert found == [("ops/restore.md", "Restore drills")]
    assert notes[0]["content"] == "Run the restore drill monthly."
    assert "kh-stub" in notes[0]["providers"]
    assert "offline" not in json.dumps(payload)  # local-private: never without a clearance


def test_kh_resolver_takes_the_server_clearance(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    repository = _kh_vault(tmp_path, {"root": str(root), "index": str(db)})
    every = frozenset({"public", "shared", "local-private"})
    payload = build_runtime(repository.root, clearance=every).search(
        "restore drill", limit=10, mode="two_stage"
    )
    assert {row["path"] for row in payload["notes"]} == {"ops/restore.md", "ops/secret.md"}
    public = build_runtime(repository.root, clearance=frozenset({"public"})).search(
        "restore drill", limit=10, mode="two_stage"
    )
    assert {row["path"] for row in public["notes"]} == {"ops/restore.md"}


def test_kh_refs_stay_unresolved_without_a_notes_index(tmp_path: Path) -> None:
    repository = _kh_vault(tmp_path, None)
    payload = build_runtime(repository.root).search("restore drill", limit=10, mode="two_stage")
    assert "notes" not in payload
    assert payload["recall_metrics"]["dropped"].get("unresolved") == 3


def test_kh_notes_in_another_tenant_are_dropped(tmp_path: Path) -> None:
    root, db = _notes(tmp_path)
    repository = _kh_vault(tmp_path, {"root": str(root), "index": str(db), "tenant": "alpha"})
    payload = build_runtime(repository.root).search("restore drill", limit=10, mode="two_stage")
    assert "notes" not in payload
    alpha = build_runtime(repository.root, tenant="alpha").search(
        "restore drill", limit=10, mode="two_stage"
    )
    assert [row["path"] for row in alpha["notes"]] == ["ops/restore.md"]


# --- defaults stay byte-identical --------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_wiring_defaults_are_byte_identical_to_base_ref() -> None:
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    results = byte_identical.compare(
        "tests.harness.recall_wiring_probe:default_outputs",
        [
            {"meta": "none"},
            {"meta": "vault"},
            {"meta": "vault", "clearance": "public,shared"},
        ],
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        assert result.identical, f"{result.payload}:\n{result.diff[:4000]}"


# --- review fixes: fail closed on vault.yaml, dream, the C4 evaluation --------------------


@pytest.mark.parametrize("damage", ["malformed", "unreadable"])
def test_a_vault_yaml_that_cannot_be_read_fails_closed(tmp_path: Path, damage: str) -> None:
    """It might name a ``lifecycle_source``: never fall back to the vault-only reading."""

    repository, outside = _imported_vault(tmp_path)
    _append_config(repository, {"lifecycle_source": str(outside)})
    config = repository.root / "vault.yaml"
    if damage == "malformed":
        config.write_text(config.read_text(encoding="utf-8") + "  : [unclosed\n", "utf-8")
    runtime = build_runtime(repository.root)
    if damage == "unreadable":
        if os.geteuid() == 0:
            pytest.skip("root ignores file modes")
        config.chmod(0)
    try:
        with pytest.raises(RecallAdapterError) as search:
            runtime.search("backup target", limit=10, mode="two_stage", policy="strict")
        assert search.value.code == "bad_config"
        with pytest.raises(RecallAdapterError) as context:
            runtime.context_pack(repository, policy_name="strict", limit_tokens=2000)
        assert context.value.code == "bad_config"
    finally:
        config.chmod(0o644)
    assert runtime._recall_wiring is None  # never cached: the next call reads again


def test_no_vault_yaml_gives_the_defaults(tmp_path: Path) -> None:
    runtime = build_runtime(tmp_path / "absent")
    assert runtime.recall_wiring() == RecallWiring()
    assert runtime.configured_lifecycle() is None


def test_one_meta_index_snapshot_per_recall_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw import projection
    from memfw.recall import builtin

    repository, outside = _imported_vault(tmp_path)
    _append_config(repository, {"lifecycle_source": str(outside)})
    runtime = build_runtime(repository.root)
    calls: list[object] = []
    real = projection.load_lifecycle

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(projection, "load_lifecycle", counting)
    monkeypatch.setattr(builtin, "load_lifecycle", counting)
    runtime.search("backup target", limit=10, mode="two_stage", policy="strict", labels=True)
    assert len(calls) == 1


def test_dream_defaults_to_the_configured_lifecycle_source(tmp_path: Path) -> None:
    from memfw.dream.questions import accept_question, lifecycle_source_from_config
    from memfw.dream.run import DreamOptions

    repository, outside = _imported_vault(tmp_path)
    assert lifecycle_source_from_config(repository.read_config(), repository.root) is None
    _append_config(repository, {"lifecycle_source": str(outside)})
    assert lifecycle_source_from_config(repository.read_config(), repository.root) == outside
    assert DreamOptions().lifecycle_source is None  # the flag stays unset by default
    # accept_question reads the key when no flag is given: a malformed key fails.
    config = repository.root / "vault.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(str(outside), ""), encoding="utf-8"
    )
    from memfw.projection import ProjectionError

    with pytest.raises(ProjectionError):
        accept_question(repository, "dream-x", ["m-keep", "m-retired"], dry_run=True)


class _Stop(Exception):
    pass


class _Supersede:
    action = "supersede"


@pytest.mark.parametrize("explicit", [False, True])
def test_dream_privacy_reads_the_configured_meta_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    """``run_dream`` joins the configured meta-index unless ``--lifecycle-source`` is given."""

    from memfw.dream import run as dream_run

    repository, outside = _imported_vault(tmp_path)
    _append_config(repository, {"lifecycle_source": str(outside)})
    named = tmp_path / "explicit.json"
    _meta_index(named, [])
    seen: list[Any] = []

    def capture(*_args: Any, lifecycle: Any, **_kwargs: Any) -> Any:
        seen.append(lifecycle)
        raise _Stop

    # A conflict is what makes the dream run the open-question privacy check.
    monkeypatch.setattr(dream_run, "curate", lambda *a, **k: [_Supersede()])
    monkeypatch.setattr(dream_run, "conflict_questions", capture)
    options = dream_run.DreamOptions(refresh=False, lifecycle_source=named if explicit else None)
    with pytest.raises(_Stop):
        dream_run.run_dream(repository.root, now=NOW, dry_run=True, options=options)
    (lifecycle,) = seen
    assert lifecycle.meta is not None
    retired = lifecycle.meta.lookup("m-retired.md")
    if explicit:
        assert retired is None  # the flag's (empty) meta-index wins
    else:
        assert retired is not None and retired["lifecycle"] == "retired"


def test_c4_evaluation_reads_a_private_copy_of_an_external_meta_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memfw import projection
    from memfw.runtime import context_eval as ce
    from memfw.runtime.context_eval import EvalOptions, SnapshotChanged, evaluate_snapshot

    options = EvalOptions(policy="strict", slots=("core",), repeats=1, environ={})
    repository, outside = _imported_vault(tmp_path)
    plain = evaluate_snapshot(repository.root, options=options)["snapshot"]["visible"]
    _append_config(repository, {"lifecycle_source": str(outside)})
    reads: list[Path] = []
    real = projection.load_lifecycle

    def recording(root: Any, source: Any = None) -> Any:
        if source is not None:
            reads.append(Path(source))
        return real(root, source)

    monkeypatch.setattr(projection, "load_lifecycle", recording)
    joined = evaluate_snapshot(repository.root, options=options)["snapshot"]["visible"]
    assert joined == plain - 3  # m-retired, m-expired, q-retired
    assert reads and all(path != outside and path.name == outside.name for path in reads)

    real_copy = ce._evaluate_copy

    def touching(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = real_copy(*args, **kwargs)
        _meta_index(outside, [{"path": "m-keep.md", "lifecycle": "retired"}])
        return out

    monkeypatch.setattr(ce, "_evaluate_copy", touching)
    with pytest.raises(SnapshotChanged):
        evaluate_snapshot(repository.root, options=options)
