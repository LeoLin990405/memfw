from __future__ import annotations

from pathlib import Path

import pytest

from memfw.dream.budget import (
    MAX_BYTES,
    MAX_LINES,
    BudgetReport,
    budget_candidates,
    check_budget,
    injected_files,
)


def _remaining(report: BudgetReport) -> tuple[int, int]:
    lines, size = report.lines, report.bytes
    for item in report.proposals:
        keep_bytes = len(str(item["keep"]).encode("utf-8")) + 1
        lines += 1 - int(item["lines"])
        size += keep_bytes - int(item["bytes"])
    return lines, size


def test_memory_md_under_budget(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# Index\n- one\n- two\n", encoding="utf-8")
    (report,) = check_budget(tmp_path, {})
    assert report == BudgetReport(path="MEMORY.md", lines=3, bytes=20, over=False, proposals=[])
    assert budget_candidates([report]) == []


def test_no_memory_md_no_reports(tmp_path: Path) -> None:
    assert check_budget(tmp_path, {}) == []


def test_long_file_gets_demote_proposals_that_fit(tmp_path: Path) -> None:
    parts = ["# Index", "- pointer line"]
    for name, count in (("Alpha", 120), ("Beta", 80), ("Gamma", 40), ("Delta", 6)):
        parts.append(f"## {name}")
        parts.extend(f"{name.lower()} detail {index}" for index in range(count - 1))
    path = tmp_path / "MEMORY.md"
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    before = path.read_bytes()

    (report,) = check_budget(tmp_path, {})
    assert report.lines == 248
    assert report.over
    assert report.proposals[0]["section"] == "Alpha"
    assert all(item["action"] == "demote" for item in report.proposals)
    lines, size = _remaining(report)
    assert lines <= MAX_LINES and size <= MAX_BYTES

    (candidate,) = budget_candidates([report])
    assert candidate.operation == "promote"
    assert candidate.action == "demote"
    assert candidate.memory_ids == []
    assert candidate.oid == "injected-MEMORY.md"
    assert candidate.changes == {"file": "MEMORY.md", "proposals": report.proposals}
    assert str(MAX_LINES) in candidate.reason and "248" in candidate.reason
    assert path.read_bytes() == before


def test_headingless_file_demotes_tail(tmp_path: Path) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("".join(f"line {index}\n" for index in range(250)), encoding="utf-8")
    (report,) = check_budget(tmp_path, {})
    assert report.over
    (proposal,) = report.proposals
    assert proposal["section"].startswith("lines ")
    lines, size = _remaining(report)
    assert lines <= MAX_LINES and size <= MAX_BYTES


def test_byte_limit_alone_triggers(tmp_path: Path) -> None:
    path = tmp_path / "MEMORY.md"
    body = ["# Notes", "short"]
    body += ["## Big", "x" * (MAX_BYTES + 100)]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    (report,) = check_budget(tmp_path, {})
    assert report.lines == 4
    assert report.over
    assert [item["section"] for item in report.proposals] == ["Big"]
    lines, size = _remaining(report)
    assert size <= MAX_BYTES and lines <= MAX_LINES
    assert len(budget_candidates([report])) == 1


def test_config_injected_files(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "MEMORY.md").write_text("index\n", encoding="utf-8")
    (vault / "agents").mkdir()
    (vault / "agents" / "AGENTS.md").write_text("agents\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "CLAUDE.md").write_text("claude\n", encoding="utf-8")
    (vault / "linked.md").symlink_to(outside / "CLAUDE.md")
    config = {
        "injected_files": [
            "agents/AGENTS.md",
            "missing.md",
            "linked.md",
            "agents",
            str(outside / "CLAUDE.md"),
            "MEMORY.md",
            "agents/AGENTS.md",
        ]
    }
    files = injected_files(vault, config)
    assert files == [
        vault / "MEMORY.md",
        vault / "agents" / "AGENTS.md",
        outside / "CLAUDE.md",
    ]
    reports = check_budget(vault, config)
    measured = [report.path for report in reports if report.skipped is None]
    assert measured == ["MEMORY.md", "agents/AGENTS.md", "CLAUDE.md"]
    # Symlinks and directories are reported as skipped, never followed.
    assert {report.skipped for report in reports if report.skipped} <= {
        "symlink",
        "not a regular file",
    }
    assert any(report.skipped == "symlink" for report in reports)
    for report in reports:
        assert not Path(report.path).is_absolute()
        assert str(tmp_path) not in report.path


def test_symlinked_memory_md_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "real.md"
    target.write_text("x\n", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "MEMORY.md").symlink_to(target)
    assert injected_files(vault, {}) == []


@pytest.mark.parametrize("value", ["MEMORY.md", ["ok.md", 3], {"a": "b"}])
def test_injected_files_must_be_list_of_strings(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError):
        injected_files(tmp_path, {"injected_files": value})
