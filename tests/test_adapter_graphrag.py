"""The GraphRAG Deriver adapter (``packages/adapters/memfw-adapter-graphrag``).

GraphRAG never runs here. The input side uses memfw's real projection export;
the batch run goes through an injected process runner that records its argv and
environment; the output side reads ``tests/fixtures/graphrag_contract/output``,
CSV tables in GraphRAG 3.3.0's final column layout written with the same
pandas ``to_csv`` path its CSV table provider uses (list cells as Python and
NumPy reprs).
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memfw.core.repository import Repository
from memfw.ports import AdapterRegistry, AdaptersConfig, DeriveRequest
from memfw.ports.staging import stage_candidates
from memfw.ports.testing import check_deriver, tree_digest
from memfw.projection import export_projection

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages" / "adapters" / "memfw-adapter-graphrag"
ADAPTER_SRC = ADAPTER / "src"
if str(ADAPTER_SRC) not in sys.path:  # the adapter is a workspace member, not installed here
    sys.path.insert(0, str(ADAPTER_SRC))

from memfw_adapter_graphrag import (  # noqa: E402
    EXTERNAL_VERSION,
    SPEC,
    GraphRagDeriver,
    GraphRagError,
    Settings,
    candidates,
    factory,
)
from memfw_adapter_graphrag.deriver import (  # noqa: E402
    INPUT_DIR,
    INPUT_INDEX,
    check_endpoint,
    parse_list,
    read_output,
    render_settings,
)

FIXTURE = ROOT / "tests" / "fixtures" / "graphrag_contract"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
PRIVATE_MARKER = "walrus-private-marker-17"
MODELS = {"completion_model": "qwen2.5:7b-instruct", "embedding_model": "nomic-embed-text"}


def _counter() -> Any:
    counter = iter(range(10_000))
    return lambda: hashlib.sha256(f"seed:{next(counter)}".encode()).hexdigest()[:32]


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    repository = Repository(root, clock=lambda: NOW, id_factory=_counter())
    repository.init()
    specs = [
        ("m-backup", "Backup", "The nightly backup job copies the vault to the NAS.", "public"),
        ("m-release", "Release", "Release 0.4.0 ships the adapter registry.", "shared"),
        ("m-private", "Private", PRIVATE_MARKER, "local-private"),
        ("m-legacy", "Legacy", PRIVATE_MARKER, None),
    ]
    for memory_id, title, body, privacy in specs:
        tags = [f"fleet_path:notes/{memory_id}.md"]  # the projection export joins on it
        repository.add(title, body, memory_id=memory_id, tags=tags, privacy=privacy)
    # The fixture output was indexed at version 3 of the two shareable memories.
    for memory_id in ("m-backup", "m-release"):
        path = root / "memories" / f"{memory_id}.md"
        text = path.read_text(encoding="utf-8")
        assert "\nversion: 1\n" in text
        path.write_text(text.replace("\nversion: 1\n", "\nversion: 3\n"), encoding="utf-8")
    return root


def export(vault: Path, tmp_path: Path) -> Path:
    """One generation of memfw's real projection export of ``vault``."""

    meta = tmp_path / "status" / "memory-meta-index.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    ids = sorted(path.stem for path in (vault / "memories").glob("*.md"))
    items = [{"path": f"notes/{memory_id}.md", "lifecycle": "active"} for memory_id in ids]
    meta.write_text(
        json.dumps({"generated_at": NOW.isoformat(), "root": "/synthetic", "items": items}),
        encoding="utf-8",
    )
    result = export_projection(vault, tmp_path / "projection", lifecycle_source=meta, now=NOW)
    assert result["counts"]["projected"] == len(ids), result["counts"]
    return Path(result["tree"])


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "graphrag"
    root.mkdir()
    shutil.copytree(FIXTURE / "output", root / "output")
    return root


class Runner:
    def __init__(self, status: int = 0) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, argv: Sequence[str], env: Mapping[str, str], cwd: Path, timeout: float, log: Path
    ) -> int:
        self.calls.append(
            {"argv": list(argv), "env": dict(env), "cwd": cwd, "timeout": timeout, "log": log}
        )
        return self.status


def make(project: Path, runner: Runner | None = None, **settings: Any) -> GraphRagDeriver:
    parsed = Settings.from_mapping({"root": str(project), **MODELS, **settings})
    return GraphRagDeriver(parsed, runner=runner or Runner(), environ=ENVIRON)


ENVIRON = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/test",
    "SECRET_THING": "do-not-pass",
    "LANG": "C",
}
IDS = ("m-backup", "m-release", "m-private", "m-legacy", "m-none")


# -- the contract ------------------------------------------------------------------------------


def test_spec_matches_the_pinned_release() -> None:
    assert SPEC.version == EXTERNAL_VERSION == "3.3.0"
    assert SPEC.kind == "deriver" and SPEC.license == "MIT" and SPEC.requires == ()


def test_graph_view_passes_the_contract_and_keeps_only_requested_current_rows(
    vault: Path, project: Path
) -> None:
    deriver = make(project)
    view = check_deriver(deriver, vault=vault, request=DeriveRequest("graph", IDS))
    assert view.cited_ids() == ("m-backup", "m-release")
    assert view.epistemic_cap == "inference" and view.causal_cap == "association"
    payload = json.loads(view.content)
    assert payload["format"] == "memfw-graphrag-v1"
    names = [row["name"] for row in payload["rows"] if row["type"] == "entity"]
    # Mixed with a now-private memory, a stale version, an unstaged file, no unit: dropped.
    assert names == ["ADAPTER REGISTRY", "BACKUP JOB", "CRON", "NAS"]
    relations = [(r["source"], r["target"]) for r in payload["rows"] if r["type"] == "relation"]
    assert relations == [("BACKUP JOB", "NAS")]
    assert PRIVATE_MARKER not in view.content


def test_rows_naming_an_unrequested_memory_are_dropped(vault: Path, project: Path) -> None:
    view = make(project).derive(vault, DeriveRequest("graph", ("m-backup",)))
    rows = json.loads(view.content)["rows"]
    assert view.cited_ids() == ("m-backup",)
    assert all(row["sources"] == ["m-backup"] for row in rows)
    assert "ADAPTER REGISTRY" not in view.content


def test_reports_view_keeps_communities_built_only_from_requested_memories(
    vault: Path, project: Path
) -> None:
    view = check_deriver(make(project), vault=vault, request=DeriveRequest("reports", IDS))
    rows = json.loads(view.content)["rows"]
    # Dropped: a private entity (2), an entity id the table lacks (3), a private
    # child community (4), a covariate from an unstaged file (6), a private
    # relationship (7), a community the table lacks (9).
    assert [row["title"] for row in rows] == [
        "Nightly backup",
        "Release and backups",
        "Backup overview",
    ]
    # Community 1's own units are m-release only; its entity adds m-backup.
    assert rows[1]["sources"] == ["m-backup", "m-release"]
    capped = make(project, max_report_chars=10).derive(vault, DeriveRequest("reports", IDS))
    assert {len(row["summary"]) for row in json.loads(capped.content)["rows"]} == {10}


def test_community_tracing_follows_entities_relationships_covariates_and_children() -> None:
    from memfw_adapter_graphrag.deriver import _CommunityUnits

    output = read_output(FIXTURE / "output")
    tracer = _CommunityUnits(output)
    memories = lambda c: sorted({output.unit_memory.get(u, ("?", 0))[0] for u in tracer.units(c)})  # noqa: E731
    assert memories("0") == ["m-backup"]
    assert memories("1") == ["m-backup", "m-release"]
    assert memories("2") == ["m-backup", "m-private"]
    assert memories("4") == ["m-backup", "m-private"]  # through child 2
    assert memories("6") == ["?", "m-backup"]  # the covariate's unit maps to no memory
    assert memories("7") == ["m-backup", "m-private"]
    assert tracer.units("3") is None and tracer.units("9") is None


def test_community_tracing_refuses_malformed_cells_and_cycles() -> None:
    from memfw_adapter_graphrag.deriver import Output, _CommunityUnits

    def community(cid: str, **cells: str) -> dict[str, str]:
        base = {"community": cid, "text_unit_ids": "[]", "entity_ids": "[]"}
        return {**base, "relationship_ids": "[]", "children": "[]", **cells}

    rows = [
        community("1", children="[2]"),
        community("2", children="[1]"),
        community("3", entity_ids="not a list"),
        community("4", children="[x]"),
        community("5"),
    ]
    tracer = _CommunityUnits(Output({}, (), (), rows, ()))
    assert [tracer.units(c) for c in ("1", "3", "4")] == [None, None, None]
    assert tracer.units("5") == []


def test_a_newer_vault_version_makes_the_index_stale(vault: Path, project: Path) -> None:
    path = vault / "memories" / "m-backup.md"
    path.write_text(path.read_text().replace("\nversion: 3\n", "\nversion: 4\n"), encoding="utf-8")
    with pytest.raises(ValueError, match="no rows"):
        make(project).derive(vault, DeriveRequest("graph", ("m-backup",)))


def test_only_private_memories_reads_nothing(vault: Path, project: Path) -> None:
    with pytest.raises(ValueError, match="may leave the vault"):
        make(project).derive(vault, DeriveRequest("graph", ("m-private", "m-legacy")))


def test_unknown_kind_and_missing_output(vault: Path, project: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        make(project).derive(vault, DeriveRequest("answer", IDS))
    shutil.rmtree(project / "output")
    with pytest.raises(ValueError, match="no rows"):
        make(project).derive(vault, DeriveRequest("graph", IDS))


def test_view_is_deterministic(vault: Path, project: Path) -> None:
    request = DeriveRequest("graph", IDS)
    assert make(project).derive(vault, request) == make(project).derive(vault, request)


# -- 1. input from memfw's projection export ------------------------------------------------


def test_stage_input_copies_only_shareable_documents_from_a_projection(
    vault: Path, project: Path, tmp_path: Path
) -> None:
    tree = export(vault, tmp_path)
    (project / INPUT_DIR).mkdir()
    (project / INPUT_DIR / "stale.v1.txt").write_text("old", encoding="utf-8")
    before = tree_digest(vault)
    result = make(project).stage_input(tree, vault)
    assert tree_digest(vault) == before
    assert result.staged == ("m-backup", "m-release")
    assert set(result.skipped) == {"m-private", "m-legacy"}
    files = sorted(p.name for p in (project / INPUT_DIR).iterdir())
    assert files == [INPUT_INDEX, "m-backup.v3.txt", "m-release.v3.txt"]
    staged = (project / INPUT_DIR / "m-backup.v3.txt").read_bytes()
    assert staged == (tree / "files" / "memories" / "m-backup.md").read_bytes()
    everything = "".join(p.read_text() for p in (project / INPUT_DIR).iterdir())
    assert PRIVATE_MARKER not in everything


def test_stage_input_refuses_a_tampered_projection_file(
    vault: Path, project: Path, tmp_path: Path
) -> None:
    tree = export(vault, tmp_path)
    target = tree / "files" / "memories" / "m-backup.md"
    target.chmod(0o600)
    target.write_text(target.read_text() + "\ninjected\n", encoding="utf-8")
    result = make(project).stage_input(tree, vault)
    assert "m-backup" in result.skipped and result.staged == ("m-release",)


def test_stage_input_rechecks_the_vault_not_just_the_manifest(
    vault: Path, project: Path, tmp_path: Path
) -> None:
    tree = export(vault, tmp_path)
    path = vault / "memories" / "m-release.md"
    path.write_text(path.read_text().replace("privacy: shared", "privacy: local-private"))
    assert make(project).stage_input(tree, vault).staged == ("m-backup",)


def test_stage_input_refuses_a_root_inside_the_vault(vault: Path, tmp_path: Path) -> None:
    inside = vault / "graphrag"
    inside.mkdir()
    tree = export(vault, tmp_path)
    with pytest.raises(GraphRagError, match="root_inside_vault"):
        make(inside).stage_input(tree, vault)


def test_stage_input_refuses_a_bad_manifest(vault: Path, project: Path, tmp_path: Path) -> None:
    with pytest.raises(GraphRagError, match="bad_projection"):
        make(project).stage_input(tmp_path / "nowhere", vault)


# -- 2. the batch run (never executed here) ---------------------------------------------------


def _staged(project: Path, deriver: GraphRagDeriver | None = None) -> None:
    (project / INPUT_DIR).mkdir(exist_ok=True)
    (project / INPUT_DIR / INPUT_INDEX).write_text("{}\n", encoding="utf-8")
    (deriver or make(project)).write_settings()


def test_run_index_argv_env_and_log(project: Path) -> None:
    runner = Runner()
    deriver = make(project, runner, command="/opt/graphrag/bin/graphrag", pass_env=["LOCAL_KEY"])
    _staged(project, deriver)
    deriver._environ = {**ENVIRON, "LOCAL_KEY": "k", "OPENAI_API_BASE": "https://x"}
    assert deriver.run_index() == 0
    (call,) = runner.calls
    assert call["argv"] == [
        "/opt/graphrag/bin/graphrag",
        "index",
        "--root",
        str(project),
        "--method",
        "standard",
    ]
    assert call["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test",
        "LANG": "C",
        "LOCAL_KEY": "k",
        "DO_NOT_TRACK": "1",
    }
    assert call["cwd"] == project and call["timeout"] == 6 * 3600.0
    assert call["log"] == project / "logs" / "memfw-graphrag-index.log"


@pytest.mark.parametrize(("status", "code"), [(1, "index_failed"), (-1, "index_timeout")])
def test_run_index_failures_are_codes(project: Path, status: int, code: str) -> None:
    _staged(project)
    with pytest.raises(GraphRagError, match=code):
        make(project, Runner(status), command="/opt/graphrag").run_index()


def test_run_index_needs_staged_input_and_settings(project: Path) -> None:
    runner = Runner()
    with pytest.raises(GraphRagError, match="no_settings"):
        make(project, runner, command="/opt/graphrag").run_index()
    make(project).write_settings()
    with pytest.raises(GraphRagError, match="no_input"):
        make(project, runner, command="/opt/graphrag").run_index()
    assert runner.calls == []


def test_run_index_missing_command_is_missing_dependency(project: Path) -> None:
    _staged(project)
    runner = Runner()
    with pytest.raises(GraphRagError, match="missing_dependency"):
        make(project, runner, command="no-such-graphrag-binary-xyz").run_index()
    assert runner.calls == []


def test_the_adapter_writes_settings_with_explicit_loopback_endpoints(project: Path) -> None:
    make(project, embedding_api_base="http://127.0.0.2:8080/v1").write_settings()
    config = json.loads((project / "settings.yaml").read_text(encoding="utf-8"))
    completion = config["completion_models"]["default_completion_model"]
    embedding = config["embedding_models"]["default_embedding_model"]
    assert completion["api_base"] == "http://127.0.0.1:11434/v1"
    assert embedding["api_base"] == "http://127.0.0.2:8080/v1"
    assert completion["model"] == "qwen2.5:7b-instruct" and embedding["model"] == "nomic-embed-text"
    assert config["table_provider"] == {"type": "csv"}
    for key in ("input_storage", "output_storage", "update_output_storage", "reporting"):
        assert config[key]["type"] == "file" and "/" not in config[key]["base_dir"], key
    assert config["cache"]["storage"] == {"type": "file", "base_dir": "cache"}
    assert "connection_string" not in json.dumps(config) and "${" not in json.dumps(config)
    recorded = (project / ".memfw-settings.sha256").read_text().strip()
    assert recorded == hashlib.sha256((project / "settings.yaml").read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "change",
    [
        lambda p: (p / "settings.yaml").write_text(
            (p / "settings.yaml").read_text().replace("127.0.0.1", "192.0.2.1")
        ),
        lambda p: (p / "settings.yaml").write_text(
            (p / "settings.yaml").read_text() + "# ${GRAPHRAG_API_BASE}\n"
        ),
        lambda p: (p / ".env").write_text("GRAPHRAG_API_BASE=https://example.com\n"),
        lambda p: (p / ".memfw-settings.sha256").write_text("0" * 64 + "\n"),
    ],
)
def test_a_changed_settings_file_or_a_dotenv_never_starts_a_run(project: Path, change: Any) -> None:
    _staged(project)
    change(project)
    runner = Runner()
    with pytest.raises(GraphRagError):
        make(project, runner, command="/opt/graphrag").run_index()
    assert runner.calls == []


def test_settings_from_another_adapter_configuration_are_stale(project: Path) -> None:
    _staged(project)
    runner = Runner()
    with pytest.raises(GraphRagError, match="settings_changed"):
        make(project, runner, command="/opt/graphrag", completion_model="other").run_index()
    assert runner.calls == []


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://127.0.0.1:11434/v1", ""),
        ("http://[::1]:11434/v1", ""),
        ("http://localhost:11434/v1", "cloud_not_allowed"),
        ("https://api.openai.com/v1", "cloud_not_allowed"),
        ("http://192.0.2.1:11434/v1", "cloud_not_allowed"),
        ("http://127.0.0.1:1/${X}", "bad_endpoint"),
        ("http://user:pw@127.0.0.1/v1", "bad_endpoint"),
        ("127.0.0.1:11434", "bad_endpoint"),
    ],
)
def test_endpoint_rule_is_literal_loopback(url: str, code: str) -> None:
    assert check_endpoint(url, cloud=False) == code


def test_cloud_allows_only_https_elsewhere() -> None:
    assert check_endpoint("https://api.openai.com/v1", cloud=True) == ""
    assert check_endpoint("http://api.openai.com/v1", cloud=True) == "cloud_not_allowed"


def test_rendering_needs_both_models(project: Path) -> None:
    with pytest.raises(GraphRagError, match="no_models"):
        render_settings(Settings(root=str(project)))


# -- configuration and registry ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "code"),
    [
        ({}, "no_root"),
        ({"root": "relative/dir"}, "bad_root"),
        ({"root": "/nonexistent-graphrag-root-xyz"}, "no_root"),
        ({"root": "/tmp", "api_key": "x"}, "key_in_config"),
        ({"root": "/tmp", "surprise": 1}, "bad_option"),
        ({"root": "/tmp", "method": "standard-update"}, "bad_option"),
        ({"root": "/tmp", "pass_env": ["lower"]}, "bad_option"),
        ({"root": "/tmp", "cloud": "yes"}, "bad_option"),
        ({"root": "/tmp", "pass_env": ["OPENAI_API_BASE"]}, "bad_option"),
        ({"root": "/tmp", "pass_env": ["MODEL_BASE_URL"]}, "bad_option"),
        ({"root": "/tmp", "pass_env": ["LLM_ENDPOINT"]}, "bad_option"),
        ({"root": "/tmp", "pass_env": ["OLLAMA_HOST"]}, "bad_option"),
        ({"root": "/tmp", "api_base": "http://localhost:11434/v1"}, "bad_endpoint"),
        ({"root": "/tmp", "completion_model": "a b"}, "bad_option"),
    ],
)
def test_bad_settings_are_unavailable_with_a_code(settings: dict[str, Any], code: str) -> None:
    assert factory(settings).available().reason == code


def test_registry_loads_it_only_when_enabled(project: Path) -> None:
    registry = AdapterRegistry()
    registry.register(SPEC)
    assert registry.load("graphrag", AdaptersConfig()).reason == "disabled"
    config = {"enabled": ["graphrag"], "graphrag": {"root": str(project)}}
    status = registry.load("graphrag", AdaptersConfig.from_mapping(config))
    assert status.available and status.adapter is not None


# -- the output is data ------------------------------------------------------------------------


def test_list_cells_parse_python_and_numpy_reprs() -> None:
    assert parse_list("['a', 'b']") == ["a", "b"]
    assert parse_list("['a' 'b'\n 'c']") == ["a", "b", "c"]
    assert parse_list('["a"]') == ["a"]
    for bad in ("", "a, b", None, 3, "[]"):
        assert parse_list(bad) == []


def test_fixture_tables_have_graphrag_final_columns() -> None:
    output = read_output(FIXTURE / "output")
    assert len(output.unit_memory) == 4  # notes.txt maps to no memory
    assert set(output.entities[0]) == {
        "id", "human_readable_id", "title", "type", "description", "text_unit_ids",
        "frequency", "degree",
    }  # fmt: skip
    assert set(output.relationships[0]) == {
        "id", "human_readable_id", "source", "target", "description", "weight",
        "combined_degree", "text_unit_ids",
    }  # fmt: skip


def test_symlinked_tables_are_ignored(vault: Path, project: Path, tmp_path: Path) -> None:
    real = project / "output" / "entities.csv"
    moved = tmp_path / "elsewhere.csv"
    real.rename(moved)
    real.symlink_to(moved)
    view = make(project).derive(vault, DeriveRequest("graph", IDS))
    assert all(row["type"] == "relation" for row in json.loads(view.content)["rows"])


# -- candidates through memfw's staging ------------------------------------------------------


def test_candidates_stage_only_under_candidates_and_stay_capped(vault: Path, project: Path) -> None:
    for kind in ("graph", "reports"):
        view = make(project).derive(vault, DeriveRequest(kind, IDS))
        items = candidates(view)
        assert items and {item.metadata["causal"] for item in items} == {"none"}
        before = tree_digest(vault, exclude=("_candidates",))
        first = stage_candidates(vault, "graphrag", items, proposal_id="p-1", source="graphrag")
        again = stage_candidates(vault, "graphrag", items, proposal_id="p-1", source="graphrag")
        assert len(first.staged) == len(items) and again.staged == ()
        assert tree_digest(vault, exclude=("_candidates",)) == before


# -- layering ---------------------------------------------------------------------------------


def test_adapter_imports_only_stdlib_and_the_ports() -> None:
    allowed = set(sys.stdlib_module_names) | {"memfw_adapter_graphrag", "memfw_ports"}
    for path in sorted((ADAPTER_SRC / "memfw_adapter_graphrag").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            else:
                continue
            for name in names:
                assert name.split(".")[0] in allowed or name.startswith("memfw.ports"), name
    for path in (ROOT / "src" / "memfw").rglob("*.py"):
        assert "memfw_adapter_graphrag" not in path.read_text(encoding="utf-8"), path
