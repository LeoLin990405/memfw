from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import init_local_git  # noqa: E402


def make_project(root: Path, monkeypatch: pytest.MonkeyPatch, *, identity: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    config = root.parent / "gitconfig"
    if identity:
        config.write_text(
            "[user]\n\tname = Test User\n\temail = test@example.invalid\n"
            "[commit]\n\tgpgsign = false\n"
        )
    else:
        config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for directory in ("src/pkg", "tests", "scripts", "docs", "reports"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    files = {
        ".gitignore": (".venv/\n*.log\nreports/day6_runs/\nreports/*.zip\n"),
        "LICENSE": "license text\n",
        "README.md": "project summary\n",
        "pyproject.toml": "[project]\nname='fixture'\n",
        "uv.lock": "version = 1\n",
        "src/pkg/__init__.py": "VALUE = 1\n",
        "tests/test_fixture.py": "def test_ok(): assert True\n",
        "scripts/run.py": "print('ok')\n",
        "docs/setup.md": "Local setup.\n",
        "SOURCE_MANIFEST.json": "{}\n",
        "CHANGE_MEMORY.jsonl": "{}\n",
        "reports/result.json": "{}\n",
        "reports/synthetic.jsonl": "{}\n",
        "reports/result.md": "Evidence.\n",
        "reports/runtime.log": "ignored runtime output\n",
        "reports/archive.zip": "archive placeholder\n",
        "reports/query_repair_status.md": "Aggregate status metadata only.\n",
        "reports/query_repair_validation.txt": "Validation counts only.\n",
    }
    for name, value in files.items():
        (root / name).write_text(value)
    ignored_dir = root / "reports/day6_runs"
    ignored_dir.mkdir()
    (ignored_dir / "raw.json").write_text("{}\n")
    return root


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_default_dry_run_is_read_only_and_reports_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_project(tmp_path / "project", monkeypatch)
    groups, excluded = init_local_git._candidate_groups(root)

    assert init_local_git.run(root=root) == 0
    output = capsys.readouterr().out
    assert "Mode: dry-run" in output
    assert "package:" in output and "tests_and_experiments:" in output
    assert "docs_and_evidence:" in output
    assert "result.json" not in output
    assert "Aggregate status metadata only." not in output
    assert excluded >= 3
    assert not (root / ".git").exists()
    names = {path.as_posix() for group in groups for path in group.paths}
    assert "reports/synthetic.jsonl" in names
    assert "reports/query_repair_status.md" in names
    assert "reports/query_repair_validation.txt" in names
    assert not any(path.endswith((".zip", ".log")) for path in names)
    assert not any("day6_runs" in path for path in names)


def test_apply_creates_three_local_commits_and_clean_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path / "project", monkeypatch)

    assert init_local_git.run(root=root, apply=True) == 0
    messages = git(root, "log", "-3", "--format=%s").splitlines()
    assert messages == [
        "Add documentation and reproducibility evidence",
        "Add tests and experiment scripts",
        "Add package source and build configuration",
    ]
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "remote") == ""
    tracked = set(git(root, "ls-files").splitlines())
    assert "reports/synthetic.jsonl" in tracked
    assert "reports/runtime.log" not in tracked
    assert "reports/archive.zip" not in tracked
    assert "reports/query_repair_status.md" in tracked
    assert "reports/query_repair_validation.txt" in tracked
    assert "reports/day6_runs/raw.json" not in tracked


def test_existing_git_entry_refuses_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path / "project", monkeypatch)
    (root / ".git").mkdir()

    with pytest.raises(init_local_git.SetupError, match="already exists"):
        init_local_git.run(root=root, apply=True)
    assert not (root / ".git/config").exists()


def test_missing_identity_refuses_without_initializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path / "project", monkeypatch, identity=False)

    with pytest.raises(init_local_git.SetupError, match="user.name is not configured"):
        init_local_git.run(root=root, apply=True)
    assert not (root / ".git").exists()


def test_symlink_in_candidate_tree_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = make_project(tmp_path / "project", monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_text("not part of the repo\n")
    (root / "docs/external.md").symlink_to(outside)

    with pytest.raises(init_local_git.SetupError, match="symlink"):
        init_local_git.run(root=root)
    assert not (root / ".git").exists()


def test_ignored_candidate_refuses_without_force_adding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project(tmp_path / "project", monkeypatch)
    with (root / ".gitignore").open("a") as stream:
        stream.write("reports/result.json\n")

    with pytest.raises(init_local_git.SetupError, match="ignored by Git"):
        init_local_git.run(root=root, apply=True)
    assert (root / ".git").is_dir()
    assert "reports/result.json" not in git(root, "ls-files")
