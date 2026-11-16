"""Read one merged GitHub pull request as an experience-point draft (read-only).

The adapter shells out to ``gh pr view``. It never writes to GitHub, never
reads Conductor's code or database, and is not used by any test: tests inject a
recorded synthetic payload through :func:`draft_input_from_pull_request`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from ..core.errors import IntegrityError

# ``mergedBy`` is not in the minimum field list the task named; it is needed to
# fill the reviewer when a merged PR has no review records.
GH_FIELDS = "title,body,mergedAt,reviews,commits,files,url,mergedBy,author"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run_gh(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, ``gh`` owns its auth
        list(command),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def fetch_pull_request(repo: str, pr: int, *, runner: Runner | None = None) -> dict[str, Any]:
    """Read one PR with ``gh pr view --json``; raise on any non-zero exit."""

    if not repo or "/" not in repo:
        raise IntegrityError("--repo must be owner/name")
    command = ["gh", "pr", "view", str(int(pr)), "--repo", repo, "--json", GH_FIELDS]
    run = runner or _run_gh
    completed = run(command)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise IntegrityError(
            "gh pr view failed: " + (detail[0] if detail else f"exit {completed.returncode}")
        )
    try:
        data = json.loads(completed.stdout or "{}")
    except ValueError as exc:
        raise IntegrityError(f"gh pr view returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IntegrityError("gh pr view returned a non-object payload")
    return data


def _login(value: Any) -> str:
    if isinstance(value, dict):
        candidate = value.get("login")
        return str(candidate).strip() if candidate else ""
    return str(value or "").strip()


def _is_bot(author: Any) -> bool:
    """Whether a review author is a bot account, not a person."""

    if isinstance(author, dict):
        if author.get("is_bot") is True or author.get("isBot") is True:
            return True
        if str(author.get("type") or "").strip().lower() == "bot":
            return True
    return _login(author).lower().endswith("[bot]")


def _reviews(reviews: Sequence[Any], *, author: str) -> list[tuple[str, list[str]]]:
    """Non-bot reviewers other than the PR author, with the states they left.

    A comment is not a review verdict, so the state is kept next to the login
    instead of being flattened into an anonymous "reviewed by".
    """

    states: dict[str, list[str]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        reviewer = review.get("author")
        login = _login(reviewer)
        if not login or _is_bot(reviewer):
            continue
        if login.casefold() == author.casefold():
            # The author's own comment is not an independent review.
            continue
        state = str(review.get("state") or "").strip().upper() or "UNKNOWN"
        states.setdefault(login, [])
        if state not in states[login]:
            states[login].append(state)
    return list(states.items())


def draft_input_from_pull_request(
    data: dict[str, Any], *, repo: str, pr: int
) -> dict[str, Any]:
    """Convert recorded ``gh pr view`` JSON into the ``--from`` draft schema.

    An unmerged PR is refused: an experience point requires a finished task and
    a human verdict, and merging is the recorded human verdict this adapter has.
    """

    merged_at = str(data.get("mergedAt") or "").strip()
    if not merged_at:
        raise IntegrityError(
            "pull request is not merged; an experience point needs a finished task"
        )
    title = str(data.get("title") or "").strip()
    if not title:
        raise IntegrityError("pull request has no title")
    url = str(data.get("url") or "").strip() or f"https://github.com/{repo}/pull/{int(pr)}"
    commits = [item for item in (data.get("commits") or []) if isinstance(item, dict)]
    files = [item for item in (data.get("files") or []) if isinstance(item, dict)]
    pr_author = _login(data.get("author"))
    reviews = _reviews(data.get("reviews") or [], author=pr_author)
    reviewer_logins = [login for login, _states in reviews]
    merger = _login(data.get("mergedBy")) or pr_author
    scope_reviewed: list[str] = []
    not_reviewed: list[str] = []
    if reviews:
        reviewer = ", ".join(reviewer_logins)
        scope_reviewed.extend(
            f"pull request review by {login}: {', '.join(states)}"
            for login, states in reviews
        )
    else:
        reviewer = merger
        if not reviewer:
            raise IntegrityError("pull request has no review records and no known merger")
        scope_reviewed.append(
            f"no pull-request review records; merged by {merger}, so the reviewed scope is "
            "the merge decision only"
        )
        not_reviewed.append("no review covered the change itself")
    evidence = [f"pr:{repo}#{int(pr)}"]
    evidence.extend(
        f"commit:{str(item.get('oid') or '').strip()}"
        for item in commits
        if str(item.get("oid") or "").strip()
    )
    actions: list[str] = []
    if commits:
        actions.append(f"carried {len(commits)} commit(s) to merge")
    if files:
        names = [str(item.get("path") or "").strip() for item in files if item.get("path")]
        actions.append("changed " + ", ".join(names[:12]) + ("" if len(names) <= 12 else ", ..."))
    if not actions:
        actions.append("merged the pull request")
    return {
        "task": title,
        "conditions": [
            f"repo: {repo}",
            f"pull request: {url}",
            f"merged at: {merged_at}",
        ],
        "actions": actions,
        "evidence": evidence,
        "review": {
            "reviewer": reviewer,
            "scope_reviewed": scope_reviewed,
            "not_reviewed": not_reviewed,
            "verdict": "accepted",
        },
        "invalidated_when": [
            "the merge is reverted",
            "the changed files no longer carry this action",
        ],
        "title": f"Experience point: {title}",
    }


__all__ = [
    "GH_FIELDS",
    "draft_input_from_pull_request",
    "fetch_pull_request",
]
