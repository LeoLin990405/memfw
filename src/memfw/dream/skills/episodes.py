"""Split transcript sessions into task episodes and normalise their steps.

An *episode* is one task inside a session: a user request and the actions
that follow it, up to the next user request that is not a tool result. A
*step* is one action, reduced to a stable signature so the same procedure
matches across sessions and agents:

* a shell command becomes its program plus leading sub-command words
  (``cd repo && uv run pytest -q`` -> ``cmd:uv run pytest``); every
  meaningful segment of a compound command is a step of its own;
* any other tool becomes ``tool:<name>``. Generic file, navigation and
  orchestration tools (read, edit, grep, wait for a sub-agent, ...) and
  read-only shell programs (``cat``, ``sed``, ``rg`` ...) carry no procedure
  on their own and are skipped.

Each step also keeps what a procedure needs, extracted locally (no model):
the agent's stated *intent* (the tool call's description, or the prose just
before the call), command *flags* (never values), the *kinds* of inputs
(``<file>.py``), and the *check* its result showed (``tests pass``, ``exit
0`` ...), from a fixed vocabulary.

Injected boilerplate (harness reminders, instruction-file echoes,
orchestrator templates) is stripped from user turns before they become a
request; a user turn that is only boilerplate, or only "continue", does not
open an episode. See :mod:`.boilerplate`.

Only the redacted snippets of the history index (or the redacted turns of
the conversation store) are read; raw transcripts are never opened here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..verify import context_words
from .boilerplate import BoilerplateFilter, fingerprint, has_content, strip_structural

_TOOL = re.compile(r"\[tool ([A-Za-z0-9_.:-]{1,64})\]\s?(.*)", re.DOTALL)
_COMMAND_KEY = re.compile(r"(?:^|;\s)(?:command|cmd|script):\s")
_JS_COMMAND = re.compile(r"""\bcmd\s*:\s*"((?:[^"\\]|\\.)*)"?""")
"""``tools.exec_command({cmd:"..."})`` as some agents wrap shell calls in a script."""
_DESCRIPTION = re.compile(r"(?:^|;\s)description:\s(.*?)(?=;\s[A-Za-z_][A-Za-z0-9_]{0,31}:\s|$)")
_NEXT_KEY = re.compile(r";\s[A-Za-z_][A-Za-z0-9_]{0,31}:\s")
_SHELL_WRAPPER = re.compile(r"^(?:bash|sh|zsh)(?:;\s*|\s+)-l?c(?:;\s*|\s+)")
_SEGMENT = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"^[a-z][a-z_.+-]{0,31}$")
_FLAG = re.compile(r"^(--?[a-z][a-z0-9-]{0,24})(?:=.*)?$")
_PLACES = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://\S+|(?:~|\.{1,2})?/[^\s'\"`;,)]+|\[REDACTED:[^\]]*\]"
)
"""URLs, paths and redaction markers: never request words."""
_GENERIC_NAME = re.compile(
    r"^(?:read|write|edit|glob|grep|ls|list|view|search|find|delete|create|move|copy|open)"
    r"(?:_[a-z0-9]+)*$"
)
"""File and navigation tools under other names (``glob_file_search``, ``edit_file_v2``)."""
_EXTENSION = re.compile(r"\.([a-z]{1,5})$")
_SKIP_PROGRAMS = frozenset({"cd", "pushd", "popd", "echo", "true", "export", "set", "source"})
READ_PROGRAMS = frozenset(
    "cat head tail sed awk rg grep egrep find fd ls ll nl wc sort uniq cut tr less more file "
    "stat du df tree jq yq printf sleep wait date pwd which type test diff cmp basename "
    "dirname realpath readlink xargs tee column fold od xxd hexdump strings env printenv "
    "true false exit timeout watch mkdir touch cp mv rm ln chmod".split()
)
"""Shell programs that only look at or move files: not a procedure step."""
SCRIPT_RUNNERS = frozenset({"python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh"})
"""Bare interpreters (``python3 - <<EOF``) are ad-hoc scripts; ``python -m X`` is a step."""
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
    "index search read export import doctor verify review dream venv".split()
)
"""Words kept after a program: sub-commands, never free arguments."""
_SHELL_TOOLS = frozenset(
    {
        "bash",
        "shell",
        "exec",
        "exec_command",
        "local_shell",
        "local_shell_call",
        "run_shell",
        "run_terminal_cmd",
        "terminal",
        "shell_command",
    }
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
        "todoread",
        "notebookread",
        "notebookedit",
        "apply_patch",
        "update_plan",
        "read_file",
        "read_file_v2",
        "write_file",
        "edit_file",
        "list_dir",
        "list_directory",
        "search_replace",
        "str_replace",
        "str_replace_editor",
        "create_file",
        "delete_file",
        "file_search",
        "codebase_search",
        "ripgrep_raw_search",
        "grep_search",
        "find",
        "search",
        "think",
        "codegraph",
        "codegraph_explore",
        "codegraph_search",
        "write_stdin",
        "view_image",
    }
)
"""Tools that only move around files; a step needs more than "read a file"."""
ORCHESTRATION_TOOLS = frozenset(
    {
        "task",
        "agent",
        "spawn_agent",
        "wait_agent",
        "close_agent",
        "send_message",
        "sendmessage",
        "followup_task",
        "taskoutput",
        "taskstop",
        "killshell",
        "bashoutput",
        "toolsearch",
        "tool_search",
        "getdynamictools",
        "skill",
        "exitplanmode",
        "enterplanmode",
        "askuserquestion",
        "monitor",
        "wait",
    }
)
"""Harness plumbing (sub-agents, tool discovery, waiting): not part of a procedure."""
_PREFIXES = frozenset({"sudo", "time", "env", "nohup", "exec", "command"})
MAX_SIGNATURE_WORDS = 3
MAX_SIGNATURES_PER_CALL = 4
MAX_FLAGS = 6
INTENT_CHARS = 160
CONTINUATIONS = frozenset(
    "continue go on keep going proceed carry on resume next ok okay yes y sure go "
    "继续 请继续 继续吧 接着 接着做 好 好的 可以 嗯".split()
)
"""User turns that resume the current task instead of starting a new one."""

CHECKS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("tests fail", "failed", re.compile(r"\b\d+ failed\b|\bFAILED\b|\bERRORS?\b.*\btest", re.I)),
    (
        "tests pass",
        "success",
        re.compile(r"\b\d+ passed\b|\bOK \(\d+ tests?\)|tests? passed", re.I),
    ),
    ("lint clean", "success", re.compile(r"All checks passed|\b0 problems\b|no lint", re.I)),
    ("type check clean", "success", re.compile(r"Success: no issues found", re.I)),
    ("lint errors", "failed", re.compile(r"Found \d+ errors?\b", re.I)),
    ("working tree clean", "success", re.compile(r"nothing to commit", re.I)),
    ("commit created", "success", re.compile(r"^\[[\w./-]+ [0-9a-f]{7,}\]", re.M)),
    ("pushed", "success", re.compile(r"->\s+\S+\s*$|Everything up-to-date", re.M)),
    (
        "dependencies installed",
        "success",
        re.compile(r"Successfully installed|(?:Resolved|Installed|Audited) \d+ packages", re.I),
    ),
    ("build succeeded", "success", re.compile(r"Successfully built|\bBuild succeeded\b", re.I)),
    ("error", "failed", re.compile(r"Traceback \(most recent call last\)|^\s*error:", re.I | re.M)),
    (
        "non-zero exit",
        "failed",
        re.compile(r"exit(?: code|=|_code)[:= ]*\s*[1-9]\d*\b|Exit code [1-9]", re.I),
    ),
    ("exit 0", "success", re.compile(r"exit(?: code|=|_code)[:= ]*\s*0\b|shell-exit=0", re.I)),
)
"""``(check, outcome, pattern)`` in priority order; the first match names the check."""


INSPECTION = frozenset(
    "cmd:git status|cmd:git log|cmd:git diff|cmd:git show|cmd:git branch|cmd:git remote|"
    "cmd:git rev-parse|cmd:git fetch|cmd:git ls-files|cmd:git worktree list|cmd:git stash list|"
    "cmd:gh pr view|cmd:gh pr list|cmd:gh pr status|cmd:gh pr diff|cmd:gh pr checks|"
    "cmd:gh issue view|cmd:gh issue list|cmd:gh run list|cmd:gh run view|cmd:git|cmd:gh|"
    "tool:web_search|tool:websearch|tool:web_fetch|tool:webfetch".split("|")
)
"""Steps that only look around: a procedure needs at least one step that does something."""


CODE_WORDS = frozenset(
    "const let var import from def class function return if then fi else elif for while do "
    "done case esac print async await try except with lambda".split()
)
"""First words of source code, not of a shell command."""
_PLUMBING_NAME = re.compile(
    r"^(?:(?:get|update|create|list|set|claim|complete)_(?:goal|plan|step|current_step|todo|"
    r"todos|mcp_tools|tools|task|tasks)|team_[a-z_]+|[a-z_]*_agents?|interrupt_[a-z_]+)$"
)
"""Goal, plan, team and sub-agent plumbing under other names."""


@dataclass(frozen=True)
class Call:
    """One meaningful action of a tool call, before it becomes a :class:`Step`."""

    signature: str
    flags: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()


@dataclass
class Step:
    signature: str
    ref: str
    position: float
    """Relative position inside the episode (0 = first action, 1 = last)."""
    intent: str = ""
    flags: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    check: str = ""
    outcome: str = ""
    """``success`` / ``failed`` / ``""`` (no result seen or nothing recognised)."""


@dataclass
class Episode:
    source: str
    session_id: str
    agent: str
    ref: str
    """Ref of the request entry that opened the episode."""
    timestamp: str
    request: str
    """The request with injected boilerplate stripped."""
    words: frozenset[str] = frozenset()
    steps: list[Step] = field(default_factory=list)
    intent_words: frozenset[str] = frozenset()
    """Content words of the agent's stated intents: what it actually did."""

    @property
    def session_key(self) -> tuple[str, str]:
        return (self.source, self.session_id)

    @property
    def signatures(self) -> frozenset[str]:
        return frozenset(step.signature for step in self.steps)

    @property
    def day(self) -> str:
        return self.timestamp[:10]

    @property
    def task_key(self) -> str:
        """Same request text on the same day, same task: retries and fan-outs count once."""

        printed = fingerprint(self.request)[:200]
        if not printed:
            return f"session:{self.source}:{self.session_id}"
        return f"{self.day}:{printed}"

    @property
    def outcome(self) -> str:
        """``failed`` when the last check failed, ``success`` when one passed, else ``unknown``."""

        seen = [step.outcome for step in self.steps if step.outcome]
        if not seen:
            return "unknown"
        if seen[-1] == "failed":
            return "failed"
        return "success" if "success" in seen else "unknown"


def _command(rest: str) -> str | None:
    match = _COMMAND_KEY.search(rest)
    if match:
        value = rest[match.end() :]
        following = _NEXT_KEY.search(value)
        if following:
            value = value[: following.start()]
        return _SHELL_WRAPPER.sub("", value.strip()).strip()
    script = _JS_COMMAND.search(rest)
    if script:
        value = script.group(1).replace('\\"', '"').replace("\\n", "\n")
        return _SHELL_WRAPPER.sub("", value.strip()).strip()
    return None


def _description(rest: str) -> str:
    match = _DESCRIPTION.search(rest)
    return match.group(1).strip() if match else ""


def _segment_call(segment: str) -> Call | None:
    """The call one shell segment makes, or None when it is not a procedure step."""

    tokens = segment.strip().strip("()").split()
    while tokens and (_ENV.match(tokens[0]) or tokens[0] in _PREFIXES):
        tokens.pop(0)
    if not tokens or tokens[0] in _SKIP_PROGRAMS:
        return None
    program = tokens[0].rsplit("/", 1)[-1].lower()
    if program not in KNOWN_PROGRAMS and not _PROGRAM.match(program):
        raise ValueError("not a plain program name")
    if program in READ_PROGRAMS:
        return None
    words = [program]
    rest = tokens[1:]
    while program == "git" and len(rest) >= 2 and rest[0] in {"-C", "-c"}:
        rest = rest[2:]
    if program in SCRIPT_RUNNERS:
        if len(rest) >= 2 and rest[0] == "-m" and _PROGRAM.match(rest[1]):
            words += ["-m", rest[1]]
            rest = rest[2:]
        else:
            return None
    for token in rest:
        if len(words) >= MAX_SIGNATURE_WORDS:
            break
        if token in SUBCOMMANDS or token in KNOWN_PROGRAMS:
            words.append(token)
            continue
        break
    flags: list[str] = []
    inputs: list[str] = []
    for token in rest:
        flag = _FLAG.match(token)
        if flag and flag.group(1) not in flags and len(flags) < MAX_FLAGS:
            flags.append(flag.group(1))
            continue
        if token.startswith("-") or "=" in token:
            continue
        extension = _EXTENSION.search(token.rstrip("'\"`,;"))
        if extension and ("/" in token or "." in token[:-1]):
            kind = f"<file>.{extension.group(1)}"
            if kind not in inputs:
                inputs.append(kind)
    return Call(signature=" ".join(words), flags=tuple(flags), inputs=tuple(inputs[:4]))


def command_calls(command: str) -> list[Call]:
    """The meaningful calls of a shell command, in order (at most a few).

    Arguments are never kept: sub-command words come from a fixed vocabulary
    (``run``, ``build``, ``commit``, ...) or name a known program (``uv run
    pytest``), so host, user, branch or container names cannot leak. A program
    name with digits (``deploy-host7.sh``) makes the whole command unusable.
    Flags keep their names only (``--wheel``), inputs only their file kind.
    """

    calls: list[Call] = []
    for segment in _SEGMENT.split(command.strip()):
        if "<<" in segment:
            # The rest is a here-document body, not commands.
            try:
                call = _segment_call(segment.split("<<", 1)[0])
            except ValueError:
                return calls
            if call and call.signature not in {item.signature for item in calls}:
                calls.append(call)
            break
        try:
            call = _segment_call(segment)
        except ValueError:
            if not calls:
                return []
            break
        if call is None:
            continue
        program = call.signature.split()[0]
        if program in CODE_WORDS:
            # Source code passed as a command (a script body), not shell.
            break
        if calls and program not in KNOWN_PROGRAMS:
            # Later segments must name a known program: free words there are
            # usually script text split on a newline, not commands.
            continue
        if call.signature not in {item.signature for item in calls}:
            calls.append(call)
        if len(calls) >= MAX_SIGNATURES_PER_CALL:
            break
    return calls


def command_signature(command: str) -> str | None:
    """``program [sub-command words]`` of the first meaningful command, lower case."""

    calls = command_calls(command)
    return calls[0].signature if calls else None


def tool_calls(text: str) -> list[Call]:
    """The steps one ``[tool NAME] SUMMARY`` line stands for."""

    match = _TOOL.search(text)
    if not match:
        return []
    name = match.group(1).lower()
    rest = match.group(2)
    if name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1] if name.count("__") >= 2 else name
    command = _command(rest)
    if command is not None or name in _SHELL_TOOLS:
        calls = command_calls(command if command is not None else rest)
        return [Call(f"cmd:{call.signature}", call.flags, call.inputs) for call in calls]
    if (
        name in GENERIC_TOOLS
        or name in ORCHESTRATION_TOOLS
        or _GENERIC_NAME.match(name)
        or _PLUMBING_NAME.match(name)
    ):
        return []
    return [Call(f"tool:{name}")]


def step_signature(text: str) -> str | None:
    """The first signature of an action entry, or None when it is not a step."""

    calls = tool_calls(text)
    return calls[0].signature if calls else None


def classify_result(text: str) -> tuple[str, str]:
    """``(check, outcome)`` a tool result shows, from the fixed :data:`CHECKS` vocabulary."""

    sample = str(text or "")[:4000]
    for check, outcome, pattern in CHECKS:
        if pattern.search(sample):
            return check, outcome
    return "", ""


def _intent(prose: str) -> str:
    """The last sentence of the prose before a tool call (the agent's stated intent)."""

    lines = [line.strip(" -*#>") for line in prose.strip().splitlines() if line.strip(" -*#>")]
    if not lines:
        return ""
    last = lines[-1]
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+", last) if part.strip()]
    value = sentences[-1] if sentences else last
    return value[:INTENT_CHARS].rstrip(" :.")


def is_continuation(text: str) -> bool:
    value = re.sub(r"[\s.!。！,，~]+", " ", text.lower()).strip()
    return bool(value) and (value in CONTINUATIONS or len(value) <= 3 and not has_content(value))


@dataclass
class _Pending:
    signature: str
    ref: str
    intent: str
    flags: tuple[str, ...]
    inputs: tuple[str, ...]
    check: str = ""
    outcome: str = ""


def _finish(episode: Episode | None, actions: list[_Pending], out: list[Episode]) -> None:
    if episode is None:
        return
    total = max(1, len(actions) - 1)
    intent_words: set[str] = set()
    for index, action in enumerate(actions):
        episode.steps.append(
            Step(
                signature=action.signature,
                ref=action.ref,
                position=index / total,
                intent=action.intent,
                flags=action.flags,
                inputs=action.inputs,
                check=action.check,
                outcome=action.outcome,
            )
        )
        intent_words.update(context_words(action.intent))
    episode.intent_words = frozenset(intent_words)
    if episode.steps:
        out.append(episode)


def segment(
    source: str,
    session_id: str,
    entries: Iterable[Mapping[str, Any]],
    boilerplate: BoilerplateFilter | None = None,
) -> list[Episode]:
    """Episodes of one session, from its index entries in file order.

    A user entry opens a new episode unless it directly answers a tool call
    (Claude-style transcripts store tool results as user turns), is only
    injected boilerplate, or only says "continue". ``boilerplate`` is the
    corpus-aware filter; without it only the static harness markers are
    stripped.
    """

    episodes: list[Episode] = []
    current: Episode | None = None
    actions: list[_Pending] = []
    awaiting: list[_Pending] = []
    """Steps whose tool result has not been seen yet, oldest first."""
    pending = 0
    """Tool calls still waiting for their result (parallel calls answer one by one)."""

    def _result(text: str) -> None:
        if awaiting:
            action = awaiting.pop(0)
            action.check, action.outcome = classify_result(text)

    for entry in entries:
        text = str(entry.get("snippet") or "")
        role = str(entry.get("role") or "")
        calls = text.count("[tool ")
        if role == "user" and pending == 0:
            request = boilerplate.strip(text) if boilerplate else strip_structural(text)
            if not re.search(r"\w", request):
                if boilerplate is not None:
                    boilerplate.note("requests_injected")
                continue
            if is_continuation(request) and current is not None:
                if boilerplate is not None:
                    boilerplate.note("requests_continuation")
                continue
            _finish(current, actions, episodes)
            actions = []
            awaiting = []
            current = Episode(
                source=source,
                session_id=session_id,
                agent=str(entry.get("agent") or ""),
                ref=str(entry.get("ref") or ""),
                timestamp=str(entry.get("timestamp") or ""),
                request=request,
                words=frozenset(context_words(_PLACES.sub(" ", request))),
            )
            continue
        if role == "user":
            pending -= 1
            _result(text)
            continue
        if role == "tool" and not calls:
            pending = max(0, pending - 1)
            _result(text)
        elif role in {"assistant", "tool"}:
            # Prose from the assistant ends its turn: nothing is pending any more.
            pending = pending + calls if calls else 0
            if not calls:
                awaiting = []
        if current is None or "[tool " not in text:
            continue
        pieces = text.split("[tool ")
        prose = pieces[0]
        for number, line in enumerate(pieces[1:]):
            match = _TOOL.search("[tool " + line)
            rest = match.group(2) if match else ""
            # The prose before a batch of calls states the intent of the first one only.
            intent = _description(rest) or (_intent(prose) if number == 0 else "")
            found = tool_calls("[tool " + line)
            for call in found:
                if actions and actions[-1].signature == call.signature:
                    continue
                ref = str(entry.get("ref") or "")
                actions.append(_Pending(call.signature, ref, intent, call.flags, call.inputs))
            # One result answers one tool call: it checks the call's last command.
            awaiting.append(actions[-1] if found and actions else _Pending("", "", "", (), ()))
    _finish(current, actions, episodes)
    return episodes
