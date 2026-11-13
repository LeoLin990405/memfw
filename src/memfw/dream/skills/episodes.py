"""Split transcript sessions into task episodes and normalise their steps.

An *episode* is one task inside a session: a user request and the actions
that follow it, up to the next user request that is not a tool result. A
*step* is one action, reduced to a stable signature so the same procedure
matches across sessions and agents:

* a shell command becomes its program plus leading sub-command words
  (``cd repo && uv run pytest -q`` -> ``cmd:uv run pytest``);
* any other tool becomes ``tool:<name>``. Generic file and navigation tools
  (read, edit, grep, ...) carry no procedure on their own and are skipped.

Only the redacted snippets of the history index are read; raw transcripts
are never opened here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..verify import context_words

_TOOL = re.compile(r"\[tool ([A-Za-z0-9_.:-]{1,64})\]\s?(.*)", re.DOTALL)
_COMMAND_KEY = re.compile(r"(?:^|;\s)(?:command|cmd|script):\s")
_NEXT_KEY = re.compile(r";\s[A-Za-z_][A-Za-z0-9_]{0,31}:\s")
_SHELL_WRAPPER = re.compile(r"^(?:bash|sh|zsh)(?:;\s*|\s+)-l?c(?:;\s*|\s+)")
_SEGMENT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"^[a-z][a-z_.+-]{0,31}$")
_SKIP_PROGRAMS = frozenset({"cd", "pushd", "popd", "echo", "true", "export", "set", "source"})
KNOWN_PROGRAMS = frozenset(
    "git gh uv uvx pip pipx poetry pdm hatch tox nox python python3 pytest ruff mypy black "
    "isort pre-commit npm npx pnpm yarn bun deno node tsc eslint prettier vitest jest cargo "
    "rustc go make just cmake docker docker-compose podman kubectl helm terraform ansible "
    "brew apt conda mamba memfw conductor alembic sqlite3 psql".split()
)
SUBCOMMANDS = frozenset(
    "run exec x add remove sync lock install uninstall update upgrade build test check lint "
    "format fmt clean init new create list ls view show status diff log fetch pull push "
    "commit merge rebase checkout switch branch tag stash reset restore cherry-pick revert "
    "clone worktree pr issue release workflow api auth publish deploy apply plan destroy "
    "start stop restart up down logs ps compose serve dev preview migrate upgrade downgrade "
    "index search read export import doctor verify review dream".split()
)
"""Words kept after a program: sub-commands, never free arguments."""
_SHELL_TOOLS = frozenset(
    {"bash", "shell", "exec_command", "local_shell", "local_shell_call", "run_shell", "terminal"}
)
GENERIC_TOOLS = frozenset(
    {
        "read",
        "write",
        "edit",
        "multiedit",
        "glob",
        "grep",
        "ls",
        "view",
        "todowrite",
        "notebookread",
        "notebookedit",
        "apply_patch",
        "update_plan",
        "read_file",
        "write_file",
        "list_dir",
    }
)
"""Tools that only move around files; a step needs more than "read a file"."""
_PREFIXES = frozenset({"sudo", "time", "env", "nohup", "exec", "command"})
MAX_SIGNATURE_WORDS = 3


@dataclass(frozen=True)
class Step:
    signature: str
    ref: str
    position: float
    """Relative position inside the episode (0 = first action, 1 = last)."""


@dataclass
class Episode:
    source: str
    session_id: str
    agent: str
    ref: str
    """Ref of the request entry that opened the episode."""
    timestamp: str
    request: str
    words: frozenset[str] = frozenset()
    steps: list[Step] = field(default_factory=list)

    @property
    def session_key(self) -> tuple[str, str]:
        return (self.source, self.session_id)

    @property
    def signatures(self) -> frozenset[str]:
        return frozenset(step.signature for step in self.steps)


def _command(rest: str) -> str | None:
    match = _COMMAND_KEY.search(rest)
    if not match:
        return None
    value = rest[match.end() :]
    following = _NEXT_KEY.search(value)
    if following:
        value = value[: following.start()]
    return _SHELL_WRAPPER.sub("", value.strip()).strip()


def command_signature(command: str) -> str | None:
    """``program [sub-command words]`` of the first meaningful command, lower case.

    Arguments are never kept: sub-command words come from a fixed vocabulary
    (``run``, ``build``, ``commit``, ...) or name a known program (``uv run
    pytest``), so host, user, branch or container names cannot leak. A program
    name with digits (``deploy-host7.sh``) is not a step.
    """

    for segment in _SEGMENT.split(command.strip()):
        tokens = segment.strip().strip("()").split()
        while tokens and (_ENV.match(tokens[0]) or tokens[0] in _PREFIXES):
            tokens.pop(0)
        if not tokens or tokens[0] in _SKIP_PROGRAMS:
            continue
        program = tokens[0].rsplit("/", 1)[-1].lower()
        if program not in KNOWN_PROGRAMS and not _PROGRAM.match(program):
            return None
        words = [program]
        for token in tokens[1:]:
            if len(words) >= MAX_SIGNATURE_WORDS:
                break
            if token in SUBCOMMANDS or token in KNOWN_PROGRAMS:
                words.append(token)
                continue
            break
        return " ".join(words)
    return None


def step_signature(text: str) -> str | None:
    """The signature of an action entry, or None when it is not a step."""

    match = _TOOL.search(text)
    if not match:
        return None
    name = match.group(1).lower()
    rest = match.group(2)
    command = _command(rest)
    if command is not None or name in _SHELL_TOOLS:
        signature = command_signature(command if command is not None else rest)
        return f"cmd:{signature}" if signature else None
    if name in GENERIC_TOOLS:
        return None
    return f"tool:{name}"


def _finish(episode: Episode | None, actions: list[tuple[str, str]], out: list[Episode]) -> None:
    if episode is None:
        return
    total = max(1, len(actions) - 1)
    for index, (signature, ref) in enumerate(actions):
        episode.steps.append(Step(signature=signature, ref=ref, position=index / total))
    if episode.steps:
        out.append(episode)


def segment(source: str, session_id: str, entries: Iterable[Mapping[str, Any]]) -> list[Episode]:
    """Episodes of one session, from its index entries in file order.

    A user entry opens a new episode unless it directly answers a tool call
    (Claude-style transcripts store tool results as user turns).
    """

    episodes: list[Episode] = []
    current: Episode | None = None
    actions: list[tuple[str, str]] = []
    pending = 0
    """Tool calls still waiting for their result (parallel calls answer one by one)."""
    for entry in entries:
        text = str(entry.get("snippet") or "")
        role = str(entry.get("role") or "")
        calls = text.count("[tool ")
        if role == "user" and pending == 0:
            _finish(current, actions, episodes)
            actions = []
            current = Episode(
                source=source,
                session_id=session_id,
                agent=str(entry.get("agent") or ""),
                ref=str(entry.get("ref") or ""),
                timestamp=str(entry.get("timestamp") or ""),
                request=text,
                words=frozenset(context_words(text)),
            )
            continue
        if role == "user":
            pending -= 1
            continue
        if role == "tool" and not calls:
            pending = max(0, pending - 1)
        elif role in {"assistant", "tool"}:
            # Prose from the assistant ends its turn: nothing is pending any more.
            pending = pending + calls if calls else 0
        if current is None:
            continue
        for line in text.split("[tool ")[1:] if "[tool " in text else []:
            signature = step_signature("[tool " + line)
            if signature and (not actions or actions[-1][0] != signature):
                actions.append((signature, str(entry.get("ref") or "")))
    _finish(current, actions, episodes)
    return episodes
