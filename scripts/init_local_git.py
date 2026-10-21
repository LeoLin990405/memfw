"""Initialize this checkout with a small, privacy-aware local Git history.

The default mode is a read-only dry run. Pass ``--apply`` to initialize Git
and create three local commits. This helper never configures identity, adds a
remote, pushes, or force-adds files.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_SUFFIXES = {".md", ".json", ".jsonl"}
TEST_SUFFIXES = {
    ".css",
    ".csv",
    ".gif",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".jsonl",
    ".md",
    ".png",
    ".py",
    ".svg",
    ".toml",
    ".txt",
    ".webp",
    ".yaml",
    ".yml",
}
SKIP_DIRS = {
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "localdata",
    "logs",
    "memories",
    "memfw-data",
    "model_cache",
    "models",
    "pilot-vault",
    "pilot_vault",
}
PRIVATE_NAMES = (
    ".env",
    "credentials",
    "events.jsonl",
    "events.state.json",
    "query_approval.json",
    "secrets",
)
ALLOWED_ROOT_ENTRIES = {
    ".cache",
    ".coverage",
    ".gitignore",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".github",
    "CHANGE_MEMORY.jsonl",
    "LICENSE",
    "README.md",
    "SOURCE_MANIFEST.json",
    "__pycache__",
    "data",
    "datasets",
    "docs",
    "htmlcov",
    "localdata",
    "logs",
    "memfw-data",
    "model_cache",
    "models",
    "pilot-vault",
    "pilot_vault",
    "private",
    "pyproject.toml",
    "reports",
    "scripts",
    "src",
    "tests",
    "uv.lock",
    "vault",
    "vaults",
}
ALLOWED_QUERY_REPORTS = {
    "reports/query_repair_status.md",
    "reports/query_repair_validation.txt",
}


@dataclass(frozen=True)
class Group:
    name: str
    paths: tuple[Path, ...]
    message: str


class SetupError(Exception):
    """A safe-to-display setup failure."""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SetupError("Git is not installed or is not available on PATH.") from exc
    if check and result.returncode:
        raise SetupError(f"Git command failed ({args[0]}), exit {result.returncode}.")
    return result


def _is_private_name(path: Path) -> bool:
    lowered = path.name.lower()
    if lowered.startswith("pilot_queries") or lowered.startswith(".env"):
        return True
    if any(part.lower() in SKIP_DIRS for part in path.parts):
        return True
    if any(marker in lowered for marker in PRIVATE_NAMES):
        return True
    if "query_repair" in lowered and path.as_posix() not in ALLOWED_QUERY_REPORTS:
        return True
    if lowered.endswith((".pem", ".key")):
        return True
    return "credential" in lowered or "secret" in lowered


def _walk_files(root: Path, base: Path) -> tuple[list[Path], int, list[str]]:
    """Return regular files under base without following symlinks."""
    found: list[Path] = []
    excluded = 0
    excluded_paths: list[str] = []

    def visit(directory: Path) -> None:
        nonlocal excluded
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise SetupError("Could not inspect candidate paths safely.") from exc
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root)
            if entry.is_symlink():
                raise SetupError(
                    "A symlink exists inside a candidate directory; refusing to continue."
                )
            if entry.is_dir(follow_symlinks=False):
                if entry.name == ".git":
                    raise SetupError("A nested .git directory exists in a candidate directory.")
                if entry.name.lower() in SKIP_DIRS or relative.as_posix() == "reports/day6_runs":
                    excluded += 1
                    excluded_paths.append(f"{relative.as_posix()}/")
                    continue
                visit(path)
            elif entry.is_file(follow_symlinks=False):
                if _is_private_name(relative):
                    excluded += 1
                    excluded_paths.append(relative.as_posix())
                    continue
                found.append(relative)
            else:
                excluded += 1

    if base.exists():
        if base.is_symlink():
            raise SetupError("A candidate directory is a symlink; refusing to continue.")
        if not base.is_dir():
            raise SetupError("A candidate path is not a directory.")
        visit(base)
    return found, excluded, excluded_paths


def _candidate_groups(root: Path) -> tuple[list[Group], int]:
    excluded = 0
    excluded_paths: list[str] = []
    package: list[Path] = []
    for name in (".gitignore", "LICENSE", "README.md", "pyproject.toml", "uv.lock"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            package.append(Path(name))
    source, n, omitted = _walk_files(root, root / "src")
    excluded += n
    excluded_paths.extend(omitted)
    package.extend(source)
    workflows, n, omitted = _walk_files(root, root / ".github")
    excluded += n
    excluded_paths.extend(omitted)
    package.extend(path for path in workflows if path.suffix.lower() in {".md", ".yml", ".yaml"})
    excluded += sum(path.suffix.lower() not in {".md", ".yml", ".yaml"} for path in workflows)

    tests, n, omitted = _walk_files(root, root / "tests")
    excluded += n
    excluded_paths.extend(omitted)
    scripts, n, omitted = _walk_files(root, root / "scripts")
    excluded += n
    excluded_paths.extend(omitted)
    code_group = [path for path in tests if path.suffix.lower() in TEST_SUFFIXES] + [
        path for path in scripts if path.suffix.lower() == ".py"
    ]
    excluded += len(tests) + len(scripts) - len(code_group)
    excluded_paths.extend(path.as_posix() for path in [*tests, *scripts] if path not in code_group)

    public: list[Path] = []
    for name in ("SOURCE_MANIFEST.json", "CHANGE_MEMORY.jsonl"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            public.append(Path(name))
    reports, n, omitted = _walk_files(root, root / "reports")
    excluded += n
    excluded_paths.extend(omitted)
    public.extend(
        path
        for path in reports
        if path.suffix.lower() in REPORT_SUFFIXES or path.as_posix() in ALLOWED_QUERY_REPORTS
    )
    excluded += sum(
        path.suffix.lower() not in REPORT_SUFFIXES and path.as_posix() not in ALLOWED_QUERY_REPORTS
        for path in reports
    )
    excluded_paths.extend(
        path.as_posix()
        for path in reports
        if path.suffix.lower() not in REPORT_SUFFIXES
        and path.as_posix() not in ALLOWED_QUERY_REPORTS
    )
    docs, n, omitted = _walk_files(root, root / "docs")
    excluded += n
    excluded_paths.extend(omitted)
    public.extend(path for path in docs if path.suffix.lower() == ".md")
    excluded += sum(path.suffix.lower() != ".md" for path in docs)
    excluded_paths.extend(path.as_posix() for path in docs if path.suffix.lower() != ".md")

    groups = [
        Group(
            "package",
            tuple(sorted(set(package))),
            "Add package source and build configuration",
        ),
        Group(
            "tests_and_experiments",
            tuple(sorted(set(code_group))),
            "Add tests and experiment scripts",
        ),
        Group(
            "docs_and_evidence",
            tuple(sorted(set(public))),
            "Add documentation and reproducibility evidence",
        ),
    ]
    if any(not group.paths for group in groups):
        raise SetupError("At least one required commit group has no eligible files.")
    all_paths = [path for group in groups for path in group.paths]
    if len(all_paths) != len(set(all_paths)):
        raise SetupError("Candidate files overlap between commit groups.")
    _verify_exclusions_are_ignored(root, excluded_paths)
    return groups, excluded


def _verify_exclusions_are_ignored(root: Path, paths: list[str]) -> None:
    """Use a temporary Git repository to check ignore rules before project init."""
    if not paths:
        return
    with tempfile.TemporaryDirectory(prefix="memfw-git-ignore-") as temp_name:
        temp_root = Path(temp_name)
        try:
            (temp_root / ".gitignore").write_bytes((root / ".gitignore").read_bytes())
        except OSError as exc:
            raise SetupError("Could not safely validate ignore rules.") from exc
        env = dict(os.environ)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        _git(temp_root, "init")
        for path in sorted(set(paths)):
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.excludesFile={os.devnull}",
                    "check-ignore",
                    "--quiet",
                    "--",
                    path,
                ],
                cwd=temp_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise SetupError(
                    "An excluded path is not covered by .gitignore; review it before setup."
                )


def _preflight(root: Path) -> tuple[list[Group], int]:
    if root.is_symlink() or not root.is_dir():
        raise SetupError("The fixed project root is missing or is a symlink.")
    git_dir = root / ".git"
    if git_dir.exists() or git_dir.is_symlink():
        raise SetupError(
            "A .git entry already exists; refusing to alter an initialized repository."
        )
    if root.parent.resolve() not in root.resolve().parents:
        raise SetupError("The fixed project root resolves outside its expected parent.")
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise SetupError("Could not inspect the project root safely.") from exc
    for entry in entries:
        if entry.is_symlink():
            raise SetupError("A symlink exists at the project root; refusing to continue.")
        if entry.name not in ALLOWED_ROOT_ENTRIES:
            raise SetupError("An unaccounted project-root path exists; review it before setup.")
    candidate_roots = {"src", "tests", "scripts", "reports", "docs", ".github"}
    tracked_root_files = {
        ".gitignore",
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "uv.lock",
        "SOURCE_MANIFEST.json",
        "CHANGE_MEMORY.jsonl",
    }
    untracked_root_paths = [
        f"{entry.name}/" if entry.is_dir(follow_symlinks=False) else entry.name
        for entry in entries
        if entry.name not in candidate_roots and entry.name not in tracked_root_files
    ]
    _verify_exclusions_are_ignored(root, untracked_root_paths)

    identity_name = _git(root, "config", "--get", "user.name", check=False)
    identity_email = _git(root, "config", "--get", "user.email", check=False)
    if identity_name.returncode or not identity_name.stdout.strip():
        raise SetupError("Git user.name is not configured. Configure it yourself, then retry.")
    if identity_email.returncode or not identity_email.stdout.strip():
        raise SetupError("Git user.email is not configured. Configure it yourself, then retry.")
    return _candidate_groups(root)


def _assert_not_ignored(root: Path, paths: tuple[Path, ...]) -> None:
    for path in paths:
        result = _git(root, "check-ignore", "--quiet", "--", path.as_posix(), check=False)
        if result.returncode == 0:
            raise SetupError("A selected candidate is ignored by Git; refusing to force-add it.")
        if result.returncode not in (1,):
            raise SetupError("Could not verify Git ignore rules safely.")


def run(root: Path = ROOT, apply: bool = False) -> int:
    groups, excluded = _preflight(root)
    print("Mode: apply" if apply else "Mode: dry-run")
    for group in groups:
        size = sum((root / path).stat().st_size for path in group.paths)
        print(f"{group.name}: {len(group.paths)} files, {size} bytes")
    print(f"Excluded by allowlist/privacy rules: {excluded} paths")
    print("Remote: none; push: none")
    if not apply:
        print("No changes made. Review the plan, then rerun with --apply to initialize Git.")
        return 0

    _git(root, "init")
    try:
        for group in groups:
            _assert_not_ignored(root, group.paths)
            _git(root, "add", "--", *(path.as_posix() for path in group.paths))
            _git(
                root,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "-m",
                group.message,
            )
            staged = _git(root, "diff", "--cached", "--quiet", check=False)
            if staged.returncode != 0:
                raise SetupError("A commit left staged changes; repository setup is partial.")
        status = _git(root, "status", "--porcelain", "--untracked-files=all")
        if status.stdout.strip():
            raise SetupError(
                "Repository has uncommitted files after the three commits; setup is partial."
            )
        remotes = _git(root, "remote")
        if remotes.stdout.strip():
            raise SetupError("A remote is configured; setup stopped without pushing.")
    except SetupError as exc:
        raise SetupError(
            f"{exc} Git initialization may be partial; no reset was attempted."
        ) from exc
    print("Created three local commits. Working tree is clean.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Initialize the fixed project root and create three local commits.",
    )
    args = parser.parse_args(argv)
    try:
        return run(apply=args.apply)
    except SetupError as exc:
        print(f"Setup refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
