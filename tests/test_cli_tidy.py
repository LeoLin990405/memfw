"""CLI tidy-up: open questions from the CLI, ``--json`` readers, grouped help.

* ``memfw add --type open-question --question ... --conflicts ... --missing ...``
  and ``memfw update`` with the same options (plus ``--resolved-by``) go
  through the U1 gates: no epistemic/causal/experience claim, every conflict
  exists in the question's tenant, resolution needs a current answer there.
* ``list``, ``show`` and ``index search`` gain ``--json``; without it their
  output is byte-identical to the base ref (opt-in harness check).
* every top-level command and group sits in one ``memfw --help`` panel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from memfw import cli
from memfw.cli import app
from memfw.core.epistemic import OPEN_QUESTION_TYPE
from memfw.core.repository import Repository
from memfw.mcp.schema import MEMORY_USED_TOOL, TOOLS

runner = CliRunner(env={"COLUMNS": "200"})


def _vault(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "vault")
    repository.init()
    return repository


def _run(*args: str) -> Any:
    return runner.invoke(app, list(args))


def _memories(repository: Repository) -> list[Path]:
    return sorted((repository.root / "memories").glob("*.md"))


def _add_question(repository: Repository, *extra: str) -> Any:
    return _run(
        "add",
        "--title",
        "port question",
        "--body",
        "Two memories disagree on the port.",
        "--type",
        OPEN_QUESTION_TYPE,
        "--question",
        "Which port does the service use?",
        "--root",
        str(repository.root),
        *extra,
    )


# --- add --type open-question ------------------------------------------------------


def test_add_open_question_happy_path(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    left = repository.add("8000", "port 8000", tenant="team-a")
    right = repository.add("8080", "port 8080", tenant="team-a")
    result = _add_question(
        repository,
        "--tenant",
        "team-a",
        "--conflicts",
        f"{left.id},{right.id}",
        "--conflicts",
        left.id,
        "--missing",
        "the deployed config",
    )
    assert result.exit_code == 0, result.output
    memory_id = result.stdout.strip()
    document = repository.get(memory_id)
    assert document.type == OPEN_QUESTION_TYPE
    assert document.tenant == "team-a"
    assert document.epistemic is None and document.causal == "none"
    assert document.open_question is not None
    assert document.open_question.question == "Which port does the service use?"
    assert document.open_question.conflicts == [left.id, right.id]
    assert document.open_question.missing == "the deployed config"
    assert not [i for i in repository.fsck() if str(i["kind"]).startswith("open_question")]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--epistemic", "fact"), "epistemic"),
        (("--causal", "hypothesis"), "causal"),
        (
            (
                "--experience",
                json.dumps(
                    {
                        "task": "t",
                        "conditions": ["c"],
                        "actions": ["a"],
                        "review": {"reviewer": "human", "verdict": "accepted"},
                    }
                ),
            ),
            "experience",
        ),
        (("--conflicts", "ghost"), "missing memory: ghost"),
    ],
)
def test_add_open_question_gates(tmp_path: Path, extra: tuple[str, ...], message: str) -> None:
    repository = _vault(tmp_path)
    for dry in ((), ("--dry-run",)):
        result = _add_question(repository, *extra, *dry)
        assert result.exit_code == 2
        assert message in result.output
    assert _memories(repository) == []


def test_add_open_question_conflicts_must_share_the_tenant(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    same = repository.add("a", "x", tenant="team-a")
    other = repository.add("b", "y", tenant="team-b")
    before = _memories(repository)
    for dry in ((), ("--dry-run",)):
        result = _add_question(
            repository, "--tenant", "team-a", "--conflicts", f"{same.id},{other.id}", *dry
        )
        assert result.exit_code == 2
        assert "another tenant" in result.output
    # Without --tenant the question is in tenant "": both conflicts are elsewhere.
    result = _add_question(repository, "--conflicts", same.id)
    assert result.exit_code == 2 and "another tenant" in result.output
    assert _memories(repository) == before


def test_add_open_question_needs_type_and_question(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x")
    root = ("--root", str(repository.root))
    result = _run("add", "--title", "q", "--body", "b", "--question", "q?", *root)
    assert result.exit_code == 2 and "open_question requires type" in result.output
    result = _run("add", "--title", "q", "--body", "b", "--type", OPEN_QUESTION_TYPE, *root)
    assert result.exit_code == 2 and "requires an open_question block" in result.output
    result = _run("add", "--title", "q", "--body", "b", "--conflicts", plain.id, *root)
    assert result.exit_code == 2 and "need --question" in result.output
    assert _memories(repository) == [repository.root / "memories" / f"{plain.id}.md"]


def test_add_open_question_dry_run_writes_nothing(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x")
    events = len(repository.events())
    result = _add_question(repository, "--conflicts", plain.id, "--dry-run")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["open_question"]["conflicts"] == [plain.id]
    assert len(_memories(repository)) == 1
    assert len(repository.events()) == events


# --- update ------------------------------------------------------------------------


def test_update_open_question_block(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    left = repository.add("8000", "port 8000", tenant="team-a")
    right = repository.add("8080", "port 8080", tenant="team-a")
    created = _add_question(repository, "--tenant", "team-a", "--conflicts", left.id)
    question_id = created.stdout.strip()
    root = ("--root", str(repository.root))
    result = _run("update", question_id, "--question", "Which port, really?", *root)
    assert result.exit_code == 0, result.output
    block = repository.get(question_id).open_question
    assert block is not None
    assert block.question == "Which port, really?"
    assert block.conflicts == [left.id]  # kept
    result = _run("update", question_id, "--conflicts", f"{left.id},{right.id}", *root)
    assert result.exit_code == 0, result.output
    result = _run("update", question_id, "--missing", "a packet capture", "--dry-run", *root)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["valid"] is True
    block = repository.get(question_id).open_question
    assert block is not None
    assert block.conflicts == [left.id, right.id]
    assert block.missing == ""  # the dry run wrote nothing


def test_update_open_question_gates(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    plain = repository.add("plain", "x", tenant="team-a")
    other = repository.add("other", "y", tenant="team-b")
    created = _add_question(repository, "--tenant", "team-a", "--conflicts", plain.id)
    question_id = created.stdout.strip()
    before = (repository.root / "memories" / f"{question_id}.md").read_bytes()
    root = ("--root", str(repository.root))
    for dry in ((), ("--dry-run",)):
        result = _run("update", question_id, "--conflicts", other.id, *root, *dry)
        assert result.exit_code == 2 and "another tenant" in result.output
        result = _run("update", question_id, "--conflicts", "ghost", *root, *dry)
        assert result.exit_code == 2 and "missing memory" in result.output
        result = _run("update", question_id, "--conflicts", question_id, *root, *dry)
        assert result.exit_code == 2 and "itself" in result.output
        result = _run("update", question_id, "--epistemic", "fact", *root, *dry)
        assert result.exit_code == 2 and "epistemic" in result.output
        result = _run("update", plain.id, "--question", "q?", *root, *dry)
        assert result.exit_code == 2 and "open_question requires type" in result.output
    assert (repository.root / "memories" / f"{question_id}.md").read_bytes() == before


def test_update_resolved_by(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    left = repository.add("8000", "port 8000", tenant="team-a")
    elsewhere = repository.add("8080", "port 8080", tenant="team-b")
    answer = repository.add("port is 8080", "Config says 8080.", tenant="team-a")
    question_id = _add_question(repository, "--tenant", "team-a", "--conflicts", left.id)
    question_id = question_id.stdout.strip()
    root = ("--root", str(repository.root))

    result = _run("update", question_id, "--resolved-by", answer.id, "--title", "x", *root)
    assert result.exit_code == 2 and "cannot be combined" in result.output
    for dry in ((), ("--dry-run",)):
        result = _run("update", question_id, "--resolved-by", elsewhere.id, *root, *dry)
        assert result.exit_code == 2 and "another tenant" in result.output
        result = _run("update", question_id, "--resolved-by", "ghost", *root, *dry)
        assert result.exit_code == 2 and "missing memory" in result.output
        result = _run("update", left.id, "--resolved-by", answer.id, *root, *dry)
        assert result.exit_code == 2 and "Not an open question" in result.output

    result = _run("update", question_id, "--resolved-by", answer.id, "--dry-run", *root)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["resolved_by"] == answer.id
    assert repository.get(question_id).valid_to is None

    result = _run("update", question_id, "--resolved-by", answer.id, *root)
    assert result.exit_code == 0, result.output
    resolved = repository.get(question_id)
    assert resolved.open_question is not None
    assert resolved.open_question.resolved_by == answer.id
    assert resolved.valid_to is not None
    result = _run("update", question_id, "--resolved-by", answer.id, *root)
    assert result.exit_code == 2 and "already resolved" in result.output


def test_update_privacy(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    memory = repository.add("m", "x", privacy="local-private")
    root = ("--root", str(repository.root))
    result = _run("update", memory.id, "--privacy", "nonsense", *root)
    assert result.exit_code == 2
    result = _run("update", memory.id, "--privacy", "Shared", *root)
    assert result.exit_code == 0, result.output
    assert repository.get(memory.id).privacy == "shared"


def test_no_mcp_write_path_for_open_questions() -> None:
    """Creating, editing and resolving an open question stay human (CLI) only."""

    names = {tool["name"] for tool in [*TOOLS, MEMORY_USED_TOOL]}
    assert not {name for name in names if "question" in name or "resolve" in name}
    for tool in TOOLS:
        properties = tool["inputSchema"].get("properties", {})
        assert not {"open_question", "resolved_by", "conflicts"} & set(properties)


# --- --json readers ------------------------------------------------------------------


def test_list_show_and_search_json(tmp_path: Path) -> None:
    repository = _vault(tmp_path)
    first = repository.add("port", "The service uses port 8080.")
    repository.add("retry", "Wrap calls in a retry.")
    root = ("--root", str(repository.root))

    listed = _run("list", "--json", *root)
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stdout)
    assert [row["id"] for row in payload["memories"]] == sorted(
        row["id"] for row in payload["memories"]
    )
    assert {row["title"] for row in payload["memories"]} == {"port", "retry"}
    assert set(payload["memories"][0]) == {"id", "tier", "layer", "title", "stub"}
    assert payload["warnings"] == []

    shown = _run("show", first.id, "--json", *root)
    assert shown.exit_code == 0, shown.output
    document = json.loads(shown.stdout)
    assert document["id"] == first.id and document["body"] == "The service uses port 8080."

    assert _run("index", "rebuild", *root).exit_code == 0
    found = _run("index", "search", "port", "--json", *root)
    assert found.exit_code == 0, found.output
    result = json.loads(found.stdout)
    assert result["results"] == [{"id": first.id, "title": "port"}]
    assert result["warnings"] == [] and isinstance(result["metrics"], dict)


def test_default_outputs_keep_their_format(tmp_path: Path) -> None:
    """In-process guard; the opt-in harness check below compares bytes with the base ref."""

    repository = _vault(tmp_path)
    first = repository.add("port", "The service uses port 8080.")
    root = ("--root", str(repository.root))
    listed = _run("list", *root)
    assert listed.stdout == f"{first.id}\thot\tcurrent\tport\n"
    shown = _run("show", first.id, *root)
    path = repository.root / "memories" / f"{first.id}.md"
    assert shown.stdout == path.read_text(encoding="utf-8")
    _run("index", "rebuild", *root)
    lines = _run("index", "search", "port", *root).stdout.splitlines()
    assert lines[0] == f"{first.id}\tport"
    assert isinstance(json.loads(lines[1]), dict)


@pytest.mark.skipif(
    not os.environ.get("MEMFW_ARCH_BASE_REF"),
    reason="opt-in (slow): set MEMFW_ARCH_BASE_REF to compare against that ref",
)
def test_byte_identical_against_base_ref() -> None:
    from tests.harness import byte_identical

    if not byte_identical.git_available():
        pytest.skip("git is not available")
    results = byte_identical.compare(
        "tests.harness.cli_tidy_probe:cli_tidy_probe",
        [{"search": False}, {"search": True}],
        old_ref=os.environ["MEMFW_ARCH_BASE_REF"],
    )
    for result in results:
        assert result.identical, result.diff


# --- grouped help --------------------------------------------------------------------


def _top_level_names() -> set[str]:
    import typer

    names = {
        command.name or typer.main.get_command_name(command.callback.__name__)  # type: ignore[union-attr]
        for command in app.registered_commands
    }
    return names | {str(group.name) for group in app.registered_groups}


def test_every_top_level_entry_has_a_panel() -> None:
    names = _top_level_names()
    assert names == set(cli.HELP_PANELS)
    assert set(cli.HELP_PANELS.values()) == {"Daily", "Approval", "Ops", "Export"}


def test_help_shows_the_panels() -> None:
    result = runner.invoke(app, ["--help"], env={"COLUMNS": "120"})
    assert result.exit_code == 0
    for panel in ("Daily", "Approval", "Ops", "Export"):
        assert f"─ {panel} " in result.stdout
    assert "─ Commands " not in result.stdout
