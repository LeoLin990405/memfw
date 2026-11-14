"""Detect and strip injected boilerplate before skill distillation.

Many "user" turns in agent transcripts are not what a person asked for: the
harness prepends system reminders, echoes AGENTS.md / CLAUDE.md, reports
background tasks, embeds tool schemas, and orchestrators inject the same
long prompt (contracts, goal templates, review rubrics) into every worker
session in front of the real task. Skill candidates named after that text
are noise.

Two layers, both deterministic and local:

* :func:`strip_structural` removes generic, publicly known harness markers
  (tag blocks such as ``<system-reminder>``, instruction-file echoes,
  notification prefixes, JSON tool schemas) and unwraps request wrappers
  (``<user_query>`` ...) so the request inside is kept.
* :class:`BoilerplateFilter` is corpus-aware. Pass 1 (:meth:`observe`) counts
  in how many distinct sessions and agents each normalised paragraph and
  each long line or sentence appears; pass 2 (:meth:`strip`) removes the ones repeated
  verbatim across many sessions. A template that differs only in ids, paths
  or numbers still matches (see :func:`fingerprint`).

Nothing here reads raw transcripts; callers hand in the redacted text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable

_BLOCK_TAGS = (
    "system-reminder",
    "system_reminder",
    "environment_context",
    "user_instructions",
    "INSTRUCTIONS",
    "permissions instructions",
    "functions",
    "background-task-finished",
    "task-notification",
    "local-command-stdout",
    "local-command-stderr",
    "command-message",
    "turn_aborted",
    "persisted-output",
)
"""Harness blocks, matched case-sensitively in the exact spelling harnesses emit.

A person's own ``<instructions>`` or ``<tools>`` text is not one of them.
"""
_UNWRAP_TAGS = (
    "user_query",
    "user_request",
    "goal_round",
    "command-args",
    "command-name",
)
"""Request wrappers: the tags go, the request inside stays. Generic words such
as ``<task>`` or ``<request>`` are left alone: a person may type them."""
_BLOCKS = [
    re.compile(
        rf"<{re.escape(tag)}(?:\s[^>]*)?>.*?(?:</{re.escape(tag)}\s*>|\Z)",
        re.DOTALL,
    )
    for tag in _BLOCK_TAGS
]
_UNWRAP = re.compile(
    r"</?(?:" + "|".join(re.escape(tag) for tag in _UNWRAP_TAGS) + r")(?:\s[^>]*)?>"
)
_INJECTED_PREFIX = re.compile(
    r"^\s*(?:"
    r"#\s*(?:AGENTS|CLAUDE)\.md\b"
    r"|Contents of \S*(?:CLAUDE|AGENTS)\.md"
    r"|\[SYSTEM NOTIFICATION"
    r"|The following is the Codex agent history"
    r"|Caveat: The messages below were generated"
    r")",
    re.IGNORECASE,
)
_INSTRUCTIONS_END = re.compile(r"</INSTRUCTIONS\s*>")
_INJECTED_EXACT = frozenset(
    {
        "tool loaded.",
        "[request interrupted by user]",
        "[request interrupted by user for tool use]",
    }
)
_SCHEMA_KEYS = frozenset({"parameters", "properties", "input_schema", "inputSchema", "$schema"})
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?。！？])\s+(?=\S)")
_CONTENT_WORD = re.compile(r"[A-Za-z]{2,}|[一-鿿]")

_FP_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S+")
_FP_REDACTED = re.compile(r"\[REDACTED:[^\]]*\]|<[a-z]+>")
_FP_PATH = re.compile(r"(?:~|\.{1,2})?/[^\s'\"`;,)]+|[A-Za-z]:\\\S+")
_FP_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_FP_HEX = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{6,}\b", re.I)
_FP_NUMBER = re.compile(r"\d+")
_FP_SPACE = re.compile(r"\s+")

DIGEST_BYTES = 12


def _is_schema(paragraph: str) -> bool:
    text = paragraph.strip()
    if '"type": "object"' in text and '"properties"' in text:
        return True
    if not text or text[0] not in "[{":
        return False
    try:
        value = json.loads(text)
    except ValueError:
        return False
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict) and (
            _SCHEMA_KEYS & set(item) or _SCHEMA_KEYS & set(item.get("function") or {})
        ):
            return True
    return False


def _structural(text: str) -> tuple[str, int]:
    """Residual text and the number of injected pieces removed."""

    removed = 0
    value = str(text or "")
    stripped = value.strip()
    if stripped.lower() in _INJECTED_EXACT:
        return "", 1
    if _INJECTED_PREFIX.match(value):
        ends = list(_INSTRUCTIONS_END.finditer(value))
        end = ends[-1] if ends else None
        if end is None:
            return "", 1
        value = value[end.end() :]
        removed += 1
    for pattern in _BLOCKS:
        value, count = pattern.subn("\n\n", value)
        removed += count
    value = _UNWRAP.sub("\n", value)
    kept = []
    for paragraph in paragraphs(value):
        if _is_schema(paragraph) or paragraph.strip().lower() in _INJECTED_EXACT:
            removed += 1
            continue
        kept.append(paragraph)
    return "\n\n".join(kept), removed


def strip_structural(text: str) -> str:
    """``text`` without generic harness markers (static patterns only)."""

    return _structural(text)[0]


def paragraphs(text: str) -> list[str]:
    """Blank-line separated paragraphs, each with trailing space trimmed."""

    out = []
    for part in _PARAGRAPH.split(str(text or "")):
        lines = [line.rstrip() for line in part.strip("\n").splitlines()]
        joined = "\n".join(lines).strip()
        if joined:
            out.append(joined)
    return out


def _units(paragraph: str) -> list[str]:
    """Lines, split into sentences: the pieces a template is built from."""

    out: list[str] = []
    for line in paragraph.splitlines():
        out.extend(part for part in _SENTENCE.split(line) if part.strip())
    return out


def fingerprint(text: str) -> str:
    """Normalised form used to match the same template with other ids or paths."""

    value = str(text or "").lower()
    value = _FP_URL.sub(" url ", value)
    value = _FP_REDACTED.sub(" x ", value)
    value = _FP_PATH.sub(" path ", value)
    value = _FP_UUID.sub(" id ", value)
    value = _FP_HEX.sub(" id ", value)
    value = _FP_NUMBER.sub("0", value)
    value = _FP_SPACE.sub(" ", value)
    return value.strip(" \t.,;:!?-*#>`'\"()[]{}")


def _digest(text: str) -> bytes:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=DIGEST_BYTES).digest()


def has_content(text: str) -> bool:
    """At least two content words (or two CJK characters)."""

    return len(_CONTENT_WORD.findall(text or "")) >= 2


class BoilerplateFilter:
    """Corpus-aware stripping of text injected verbatim into many sessions."""

    def __init__(
        self,
        *,
        min_sessions: int = 5,
        min_agents: int = 2,
        solo_sessions: int = 15,
        min_chars: int = 40,
    ) -> None:
        if min_sessions < 2:
            raise ValueError("min_sessions must be at least 2")
        self.min_sessions = min_sessions
        self.min_agents = max(1, min_agents)
        self.solo_sessions = max(min_sessions, solo_sessions)
        self.min_chars = min_chars
        self._sessions: dict[bytes, set[int]] = {}
        self._agents: dict[bytes, set[str]] = {}
        self._session_ids: dict[tuple[str, str], int] = {}
        self._repeated: set[bytes] | None = None
        self.stats = {
            "texts_seen": 0,
            "texts_stripped": 0,
            "texts_emptied": 0,
            "paragraphs_removed_structural": 0,
            "paragraphs_removed_repeated": 0,
            "pieces_removed_repeated": 0,
            "repeated_fingerprints": 0,
            "requests_injected": 0,
            "requests_continuation": 0,
            "requests_template_only": 0,
        }

    # -- pass 1 -------------------------------------------------------------

    def _pieces(self, residual: str) -> Iterable[str]:
        for paragraph in paragraphs(residual):
            print_ = fingerprint(paragraph)
            if len(print_) >= self.min_chars:
                yield print_
            units = _units(paragraph)
            if len(units) > 1:
                for unit in units:
                    unit_print = fingerprint(unit)
                    if len(unit_print) >= self.min_chars:
                        yield unit_print

    def observe(self, session_key: tuple[str, str], agent: str, text: str) -> None:
        """Count one user or system text of ``session_key`` (pass 1)."""

        self._repeated = None
        session = self._session_ids.setdefault(tuple(session_key), len(self._session_ids))
        cap = self.solo_sessions + 1
        for piece in set(self._pieces(strip_structural(text))):
            key = _digest(piece)
            sessions = self._sessions.setdefault(key, set())
            if len(sessions) < cap:
                sessions.add(session)
            agents = self._agents.setdefault(key, set())
            if len(agents) <= self.min_agents:
                agents.add(agent or "unknown")

    def observe_all(self, items: Iterable[tuple[tuple[str, str], str, str]]) -> None:
        for session_key, agent, text in items:
            self.observe(session_key, agent, text)

    def _qualifies(self, key: bytes) -> bool:
        sessions = len(self._sessions.get(key, ()))
        if sessions < self.min_sessions:
            return False
        return len(self._agents.get(key, ())) >= self.min_agents or sessions >= self.solo_sessions

    def _repeated_set(self) -> set[bytes]:
        if self._repeated is None:
            self._repeated = {key for key in self._sessions if self._qualifies(key)}
            self.stats["repeated_fingerprints"] = len(self._repeated)
        return self._repeated

    def repeated(self, text: str) -> bool:
        """Whether ``text`` (a paragraph or line) is repeated across the corpus."""

        print_ = fingerprint(text)
        return len(print_) >= self.min_chars and _digest(print_) in self._repeated_set()

    # -- pass 2 -------------------------------------------------------------

    def _strip(self, text: str) -> tuple[str, dict[str, int]]:
        counts = {"structural": 0, "paragraphs": 0, "lines": 0}
        residual, counts["structural"] = _structural(text)
        repeated = self._repeated_set()
        kept: list[str] = []
        for paragraph in paragraphs(residual):
            print_ = fingerprint(paragraph)
            if len(print_) >= self.min_chars and _digest(print_) in repeated:
                counts["paragraphs"] += 1
                continue
            units = _units(paragraph)
            if len(units) > 1:
                keep: list[str] = []
                dropped_chars = 0
                dropped = 0
                for unit in units:
                    unit_print = fingerprint(unit)
                    if len(unit_print) >= self.min_chars and _digest(unit_print) in repeated:
                        dropped_chars += len(unit)
                        dropped += 1
                        continue
                    keep.append(unit)
                if dropped and (
                    dropped_chars >= 0.8 * max(1, len(paragraph))
                    and not has_content(" ".join(keep))
                ):
                    counts["paragraphs"] += 1
                    continue
                if dropped:
                    counts["lines"] += dropped
                    paragraph = "\n".join(keep).strip()
            if paragraph:
                kept.append(paragraph)
        return "\n\n".join(kept), counts

    def strip(self, text: str) -> str:
        """The residual of ``text`` after structural and repeated text is removed (pass 2)."""

        residual, counts = self._strip(text)
        self.stats["texts_seen"] += 1
        self.stats["paragraphs_removed_structural"] += counts["structural"]
        self.stats["paragraphs_removed_repeated"] += counts["paragraphs"]
        self.stats["pieces_removed_repeated"] += counts["lines"]
        if any(counts.values()):
            self.stats["texts_stripped"] += 1
            if not residual:
                self.stats["texts_emptied"] += 1
        return residual

    def is_injected(self, text: str) -> bool:
        """True when nothing with content survives stripping (counters untouched)."""

        return not has_content(self._strip(text)[0])

    def note(self, counter: str) -> None:
        """Count one event a caller saw (an injected or a "continue" request)."""

        self.stats[counter] = self.stats.get(counter, 0) + 1

    def report(self) -> dict[str, int]:
        self._repeated_set()
        return dict(self.stats)
