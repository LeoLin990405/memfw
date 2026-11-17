"""Read-only Obsidian projection of a vault (O1a): ``memfw project obsidian``.

The output is a folder an Obsidian vault can open, rebuilt from the memfw
vault on every run and never read back:

* ``memories/<id>.md``: one page per memory. Front matter carries ``id``,
  ``title``, ``type``, ``epistemic``, ``status``, ``privacy``, ``tenant``,
  ``valid_from``/``valid_to``, ``tags`` and, when the U4 corroboration
  projection exists and a lookup is injected, ``corroboration.count``. The
  body is the memory body.
* ``questions/<id>.md``: the same page for ``type: open-question`` memories,
  plus the question, what is missing, its conflicts and its answer.
* ``concepts/<name>.md``: one page per ``about`` value used by an exported
  memory, linking back to those memories, with K1 ``broader``/``narrower``
  links between concept pages that exist.
* ``memfw-views/*.base``: Obsidian Bases views (memories, open questions,
  disputed).
* ``.memfw-obsidian/memfw-projection.json``: the sha256 of every generated file.

**Coexisting with other generators.** Another tool may write into the same
folder (a private board script owns ``boards/``, ``agents/``, ...,
``.memfw-obsidian/manifest.json`` and ``.memfw-obsidian/tmp/``). This
projector is disjoint from it: its own marker (``generated:
memfw-projection``), manifest (:data:`MANIFEST_PATH`), temp folder
(:data:`TMP_DIR`) and page folders (:data:`FOLDERS`); it never reads, writes
or deletes anything else.

Links are Obsidian wikilinks: ``about`` → ``[[concepts/<name>]]``,
``superseded_by`` and its inverse ``supersedes``, and an open question's
``conflicts`` and ``resolved_by``. A link is written only when its target is
exported too, so a page never names a memory the clearance hides.

**Privacy.** A memory's class is the strictest of core's readings
(:func:`memfw.projection.tag_privacy_class`,
:func:`memfw.projection.classify_privacy` over its ``privacy:`` tags, and the
fail-closed front matter reader in ``memfw.ports.reference``, which also reads
top-level ``privacy``/``sensitivity`` keys), case-insensitive. ``unknown``
(any value memfw does not recognise) is never exported, whatever the
clearance. A memory in class ``none`` is ``public`` when every declared
privacy value is ``public`` and ``shared`` otherwise (undeclared included).
The clearance is a set of ``public``, ``shared``, ``local-private``; the
default is ``public,shared``. Hidden memories leave no trace: no page, no
link, no backlink, no count, no Bases row, and no concept page that only they
use.

**Tenant.** One tenant per run (default ``""``), exact match.

**Determinism.** Same vault + same settings → byte-identical output: sorted
ids, no run timestamps in any file, stable file names. Unchanged files are
not rewritten (their mtime stays). Concept file names come from an allowlist
(``[A-Za-z0-9_-]``); anything else is replaced and a short hash of the
original keeps names unique.

**Rebuild.** Only files listed in the previous manifest are deleted or
overwritten. A listed file whose bytes no longer match the manifest was
edited by hand: it is reported in ``ObsidianResult.edited`` and then
overwritten (or deleted). An unlisted file at a path the projector needs is
never overwritten: the run is refused before anything is written.

**Safety.** The vault is only read (symlinked memory and concept files are
skipped). The output may not be the vault, inside it, contain it, or be a
symlink; no generated folder may be a symlink; files are written through a
temporary name and ``os.replace``, so no symlink is followed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from ..core.epistemic import OPEN_QUESTION_TYPE
from ..core.errors import IntegrityError
from ..core.markdown_parse import parse_memory_markdown
from ..core.models import MemoryDocument
from ..ontology.model import Concept, load_concepts
from ..ontology.taxonomy import broader_of, narrower_index
from ..ports.clock import Clock, SystemClock
from ..ports.reference._vault import MemoryFile, iter_memories, strictest
from ..ports.types import Availability, Manifest, ProjectionPolicy
from ..projection import classify_privacy, tag_privacy_class

#: Generator marker: the ``generated`` property of every page and the
#: manifest's ``generator``. Distinct from any other generator writing into the
#: same folder (e.g. a private board script using ``memfw-obsidian``).
GENERATED: Final = "memfw-projection"
#: Shared metadata folder; this projector touches only its own names in it.
MANIFEST_DIR: Final = ".memfw-obsidian"
MANIFEST_PATH: Final = f"{MANIFEST_DIR}/memfw-projection.json"
#: Temporary files (never inside a page folder); stale ones are removed at start.
TMP_DIR: Final = f"{MANIFEST_DIR}/tmp-projection"
MANIFEST_SCHEMA: Final = 1
MEMORIES: Final = "memories"
QUESTIONS: Final = "questions"
CONCEPTS: Final = "concepts"
BOARDS: Final = "memfw-views"
FOLDERS: Final = (MEMORIES, QUESTIONS, CONCEPTS, BOARDS)

CLEARANCE_LEVELS: Final = ("public", "shared", "local-private")
DEFAULT_CLEARANCE: Final = frozenset({"public", "shared"})

NOTICE: Final = (
    "> [!warning] Generated by memfw (memfw-projection). This file will be"
    " overwritten on the next refresh; change the memory in memfw instead."
)
BASE_NOTICE: Final = (
    f"# generated: {GENERATED}\n"
    "# Generated by memfw. This file will be overwritten on the next refresh.\n"
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_REL = re.compile(
    r"^(?:memories|questions|concepts)/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.md$"
    r"|^memfw-views/[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.base$"
)
#: Temporary names this projector writes (and, before they moved to ``TMP_DIR``,
#: wrote inside page folders): removed at the start of every run.
_TMP_NAME = re.compile(r"^\.?[A-Za-z0-9_.-]+\.[0-9a-f]{16}\.tmp$")
#: Extra protected output roots, ``os.pathsep``-separated.
PROTECTED_ENV: Final = "MEMFW_OBSIDIAN_PROTECTED"
_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9_-]+")
_TAG_UNSAFE = re.compile(r"[^\w/-]+")
_NAME_MAX = 80
_HIDDEN_TAG_PREFIXES: Final = ("fleet_path:", "sensitivity:", "privacy:")

#: Ids → corroboration count. Injected by the caller (the CLI wires the U4
#: projection with the share policy); ``None`` means "no projection".
CorroborationLookup = Callable[[Sequence[str]], Mapping[str, int]]


def parse_clearance(value: str | Iterable[str] | None) -> frozenset[str]:
    """``public``/``shared``/``local-private`` from a comma list; ``None`` → default.

    Case-insensitive. Any other token (``unknown`` included) or an empty value
    raises ``ValueError``: a typo never widens or silently narrows access.
    """

    if value is None:
        return DEFAULT_CLEARANCE
    tokens = value.split(",") if isinstance(value, str) else [str(item) for item in value]
    cleaned = {token.strip().lower() for token in tokens if token.strip()}
    if not cleaned:
        raise ValueError("clearance is empty")
    unknown = sorted(cleaned - set(CLEARANCE_LEVELS))
    if unknown:
        raise ValueError(
            f"unknown clearance: {', '.join(unknown)} (use {', '.join(CLEARANCE_LEVELS)})"
        )
    return frozenset(cleaned)


def privacy_level(document: MemoryDocument, memory: MemoryFile) -> str:
    """``public``, ``shared``, ``local-private`` or ``unknown`` (strictest reading wins)."""

    declared: list[str] = [
        tag.split(":", 1)[1] for tag in document.tags if tag.lower().startswith("privacy:")
    ]
    front = memory.front.get("privacy")
    if isinstance(front, str):
        declared.append(front)
    elif isinstance(front, list):
        declared.extend(front)
    tag_class = tag_privacy_class(document.tags)
    classes = [tag_class, memory.privacy_class()]
    classes.extend(classify_privacy(value, tag_class=tag_class)[0] for value in declared)
    found = strictest(classes)
    if found != "none":
        return found
    values = [value.strip().lower() for value in declared]
    if values and all(value == "public" for value in values):
        return "public"
    return "shared"


def safe_name(raw: str) -> str:
    """A deterministic file stem from ``[A-Za-z0-9_-]``; never empty, never ``..``."""

    if _SAFE_NAME.fullmatch(raw) and len(raw) <= _NAME_MAX:
        return raw
    base = _UNSAFE_RUN.sub("-", raw).strip("-_")[:_NAME_MAX].strip("-_") or "concept"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"


@dataclass(frozen=True)
class ObsidianResult:
    """What one run did. ``manifest`` is the port's view of it."""

    manifest: Manifest
    written: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    stale_tmp: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "projector": self.manifest.projector,
            "projected": self.manifest.projected,
            "files": len(self.manifest.files),
            "written": len(self.written),
            "unchanged": len(self.unchanged),
            "deleted": len(self.deleted),
            "edited": list(self.edited),
            "stale_tmp": len(self.stale_tmp),
            "content_sha256": self.manifest.content_sha256,
        }


@dataclass
class _Entry:
    document: MemoryDocument
    privacy: str
    page: str  # "memories/<id>" or "questions/<id>"
    supersedes: list[str] = field(default_factory=list)


class ObsidianProjector:
    """``Projector`` (L0 port): vault → an Obsidian folder. See the module docstring."""

    name = "obsidian"

    def __init__(
        self,
        *,
        clearance: str | Iterable[str] | None = None,
        tenant: str = "",
        corroboration: CorroborationLookup | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(tenant, str):
            raise ValueError("tenant must be a string")
        self.clearance = parse_clearance(clearance)
        self.tenant = tenant
        self.corroboration = corroboration
        self.clock: Clock = clock or SystemClock()

    def available(self) -> Availability:
        return Availability.ok()

    def export(self, vault: Path, out: Path, policy: ProjectionPolicy) -> Manifest:
        return self.build(vault, out, policy).manifest

    # ------------------------------------------------------------------ build

    def build(
        self, vault: Path, out: Path, policy: ProjectionPolicy | None = None
    ) -> ObsidianResult:
        policy = policy or ProjectionPolicy(f"obsidian-v{MANIFEST_SCHEMA}")
        root = _check_vault(Path(vault).expanduser())
        target = _check_out(Path(out).expanduser(), root)
        entries, skipped = self._select(root)
        files = self._render(root, entries)
        previous = _read_manifest(target)
        result = _apply(target, files, previous, self._manifest_bytes(policy, files))
        digest = hashlib.sha256()
        for relative in sorted(files):
            digest.update(relative.encode("utf-8") + b"\x00")
            digest.update(hashlib.sha256(files[relative]).digest())
        manifest = Manifest(
            projector=self.name,
            policy_version=policy.version,
            projected=len(entries),
            skipped={key: value for key, value in skipped.items() if value},
            content_sha256=digest.hexdigest(),
            generated_at=self.clock.now(),
            files=tuple(sorted(files)),
        )
        return ObsidianResult(manifest, *result)

    def _select(self, root: Path) -> tuple[dict[str, _Entry], dict[str, int]]:
        skipped = {"unreadable": 0, "tenant": 0, "privacy": 0}
        entries: dict[str, _Entry] = {}
        for memory_id, memory in iter_memories(root):
            document = _parse(memory_id, memory)
            if memory is None or document is None:
                skipped["unreadable"] += 1
                continue
            if document.tenant != self.tenant:
                skipped["tenant"] += 1
                continue
            level = privacy_level(document, memory)
            if level == "unknown" or level not in self.clearance:
                skipped["privacy"] += 1
                continue
            folder = QUESTIONS if document.type == OPEN_QUESTION_TYPE else MEMORIES
            entries[memory_id] = _Entry(document, level, f"{folder}/{memory_id}")
        for memory_id, entry in entries.items():
            successor = entry.document.superseded_by
            if successor and successor in entries and successor != memory_id:
                entries[successor].supersedes.append(memory_id)
        return entries, skipped

    def _render(self, root: Path, entries: dict[str, _Entry]) -> dict[str, bytes]:
        concepts = _concepts(root)
        names: dict[str, str] = {}
        backlinks: dict[str, list[str]] = {}
        for memory_id, entry in entries.items():
            for oid in _unique(entry.document.about):
                names.setdefault(oid, safe_name(oid))
                backlinks.setdefault(oid, []).append(memory_id)
        taken: dict[str, str] = {}
        for oid in sorted(backlinks):  # two raw values with one safe stem: re-hash
            if taken.setdefault(names[oid], oid) != oid:
                names[oid] = f"{names[oid]}-{hashlib.sha256(oid.encode()).hexdigest()[:10]}"
                taken[names[oid]] = oid
        counts: Mapping[str, int] | None = None
        if self.corroboration is not None:
            counts = self.corroboration(sorted(entries))
        files: dict[str, bytes] = {}
        for memory_id in sorted(entries):
            entry = entries[memory_id]
            count = None if counts is None else int(counts.get(memory_id, 0))
            files[f"{entry.page}.md"] = _memory_page(entry, entries, names, concepts, count)
        narrower = narrower_index(concepts)
        for oid in sorted(backlinks):
            # Hierarchy links only between concept pages this board has, so a
            # concept used only by hidden memories is never named.
            concept = concepts.get(oid)
            parents = (broader_of(concept) or ()) if concept is not None else ()
            hierarchy = (
                [_link(f"{CONCEPTS}/{names[item]}") for item in _unique(parents) if item in names],
                [
                    _link(f"{CONCEPTS}/{names[item]}")
                    for item in sorted(set(narrower.get(oid, ())))
                    if item in names
                ],
            )
            files[f"{CONCEPTS}/{names[oid]}.md"] = _concept_page(
                oid, concept, sorted(backlinks[oid]), entries, hierarchy
            )
        for name, text in BOARD_FILES.items():
            files[f"{BOARDS}/{name}.base"] = (BASE_NOTICE + text).encode("utf-8")
        return files

    def _manifest_bytes(self, policy: ProjectionPolicy, files: Mapping[str, bytes]) -> bytes:
        payload = {
            "generator": GENERATED,
            "schema": MANIFEST_SCHEMA,
            "projector": self.name,
            "policy_version": policy.version,
            "clearance": sorted(self.clearance),
            "tenant": self.tenant,
            "files": {name: hashlib.sha256(files[name]).hexdigest() for name in sorted(files)},
        }
        return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


# ------------------------------------------------------------------- reading


def _check_vault(root: Path) -> Path:
    if not root.is_dir():
        raise ValueError("vault root is not a directory")
    if not (root / "vault.yaml").is_file() and not (root / "events.jsonl").is_file():
        raise ValueError("not a memfw vault: vault.yaml and events.jsonl are missing")
    if (root / ".pending.json").exists() or (root / ".event-append.pending.json").exists():
        raise ValueError("vault has a pending transaction; run `memfw recover` first")
    return root


def _check_out(out: Path, root: Path) -> Path:
    if out.is_symlink():
        raise ValueError("refusing an output directory that is a symlink")
    if out.exists() and not out.is_dir():
        raise ValueError("the output exists and is not a directory")
    resolved, vault = out.resolve(), root.resolve()
    if resolved == vault or vault in resolved.parents:
        raise ValueError("the output must not be inside the vault")
    if resolved in vault.parents:
        raise ValueError("the output must not contain the vault")
    reason = protected_reason(resolved)
    if reason is not None:
        raise ValueError(f"refusing a protected output directory: {reason}")
    return out


def protected_reason(path: Path, environ: Mapping[str, str] | None = None) -> str | None:
    """Why ``path`` (resolved) may not hold a board, or ``None``.

    Refused: ``/``; the home directory itself; ``~/Documents`` and
    ``~/Library/Mobile Documents`` and below (synced); any path containing
    ``knowledge-hub`` (any case); and every root listed in
    ``MEMFW_OBSIDIAN_PROTECTED`` (``os.pathsep``-separated) and below.
    """

    env = os.environ if environ is None else environ
    resolved = Path(path).resolve()
    if resolved == Path(resolved.anchor):
        return "filesystem root"
    if "knowledge-hub" in resolved.as_posix().lower():
        return "path contains knowledge-hub"
    home = Path(env.get("HOME") or Path.home()).expanduser().resolve()
    if resolved == home:
        return "home directory"
    roots = [home / "Documents", home / "Library" / "Mobile Documents"]
    roots.extend(
        Path(item).expanduser().resolve()
        for item in env.get(PROTECTED_ENV, "").split(os.pathsep)
        if item.strip()
    )
    for root in roots:
        if resolved == root or root in resolved.parents:
            return f"inside a protected root ({root.name})"
    return None


def _parse(memory_id: str, memory: MemoryFile | None) -> MemoryDocument | None:
    if memory is None:
        return None
    try:
        return parse_memory_markdown(memory.raw.decode("utf-8"), expected_id=memory_id)
    except (IntegrityError, UnicodeDecodeError, ValueError):
        return None


def _concepts(root: Path) -> dict[str, Concept]:
    """Concept metadata for labels; an unreadable concept tree adds none (pages still link)."""

    try:
        return load_concepts(root)
    except (OSError, ValueError, yaml.YAMLError):
        return {}


# ------------------------------------------------------------------- rendering


def _unique(values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        cleaned = value.strip() if isinstance(value, str) else ""
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _label(text: str) -> str:
    one = " ".join(text.split())
    return one.replace("|", "/").replace("[", "(").replace("]", ")")


def _link(page: str, label: str | None = None) -> str:
    return f"[[{page}|{_label(label)}]]" if label else f"[[{page}]]"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _tags(tags: Iterable[str]) -> list[str]:
    """memfw tags as Obsidian tags (``a:b`` → ``a/b``); join and privacy markers left out."""

    out: list[str] = []
    for tag in tags:
        if tag.lower().startswith(_HIDDEN_TAG_PREFIXES):
            continue
        cleaned = _TAG_UNSAFE.sub("_", tag.strip().lstrip("#").replace(":", "/")).strip("/_")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _front(data: Mapping[str, Any]) -> str:
    dumped = yaml.safe_dump(
        dict(data), sort_keys=False, allow_unicode=True, default_flow_style=False, width=10_000
    )
    return f"---\n{dumped}---\n"


def _concept_title(oid: str, concept: Concept | None) -> str:
    return concept.pref_label if concept is not None and concept.pref_label else oid


def _memory_page(
    entry: _Entry,
    entries: Mapping[str, _Entry],
    names: Mapping[str, str],
    concepts: Mapping[str, Concept],
    corroboration: int | None,
) -> bytes:
    document = entry.document
    about = [
        _link(f"{CONCEPTS}/{names[oid]}", _concept_title(oid, concepts.get(oid)))
        for oid in _unique(document.about)
    ]
    supersedes = [_link(entries[item].page) for item in sorted(entry.supersedes)]
    successor = document.superseded_by
    superseded_by = _link(entries[successor].page) if successor and successor in entries else None
    data: dict[str, Any] = {
        "generated": GENERATED,
        "id": document.id,
        "title": document.title,
        "type": document.type,
        "epistemic": document.epistemic,
        "status": document.status,
        "privacy": entry.privacy,
        "tenant": document.tenant,
        "valid_from": _iso(document.valid_from),
        "valid_to": _iso(document.valid_to),
        "tags": _tags(document.tags),
    }
    if corroboration is not None:
        data["corroboration"] = {"count": corroboration}
    data["about"] = about
    data["supersedes"] = supersedes
    data["superseded_by"] = superseded_by
    links: list[str] = []
    question = document.open_question
    if question is not None:
        conflicts = [
            _link(entries[item].page) for item in _unique(question.conflicts) if item in entries
        ]
        answer = question.resolved_by
        resolved_by = _link(entries[answer].page) if answer and answer in entries else None
        if answer and resolved_by is None:
            # Answered by a memory this board hides: show the question as still
            # open, without the resolution time, so nothing reveals the answer.
            data["valid_to"] = None
        data["question"] = question.question
        data["missing"] = question.missing or None
        data["conflicts"] = conflicts
        data["resolved_by"] = resolved_by
        data["open"] = resolved_by is None and data["valid_to"] is None
        links.append(f"**Question:** {_label(question.question)}")
        if question.missing:
            links.append(f"**Missing:** {_label(question.missing)}")
        if conflicts:
            links.append("**Conflicts:** " + ", ".join(conflicts))
        if resolved_by:
            links.append(f"**Resolved by:** {resolved_by}")
    if about:
        links.append("**About:** " + ", ".join(about))
    if supersedes:
        links.append("**Supersedes:** " + ", ".join(supersedes))
    if superseded_by:
        links.append(f"**Superseded by:** {superseded_by}")
    parts = [_front(data), NOTICE, "", f"# {_label(document.title)}", ""]
    if links:
        parts.extend(f"- {line}" for line in links)
        parts.append("")
    body = document.body.rstrip("\n")
    if body:
        parts.extend([body, ""])
    return "\n".join(parts).encode("utf-8")


def _concept_page(
    oid: str,
    concept: Concept | None,
    members: Sequence[str],
    entries: Mapping[str, _Entry],
    hierarchy: tuple[list[str], list[str]] = ([], []),
) -> bytes:
    broader, narrower = hierarchy
    data: dict[str, Any] = {
        "generated": GENERATED,
        "oid": oid,
        "label": _concept_title(oid, concept),
        "class": concept.class_name if concept is not None else None,
        "concept_status": concept.status if concept is not None else None,
        "memories": [_link(entries[item].page) for item in members],
        "memory_count": len(members),
        "broader": broader,
        "narrower": narrower,
    }
    parts = [_front(data), NOTICE, "", f"# {_label(_concept_title(oid, concept))}", ""]
    if broader:
        parts.extend(["- **Broader:** " + ", ".join(broader), ""])
    if narrower:
        parts.extend(["- **Narrower:** " + ", ".join(narrower), ""])
    parts.extend(f"- {_link(entries[item].page, entries[item].document.title)}" for item in members)
    parts.append("")
    return "\n".join(parts).encode("utf-8")


_MEMORY_COLUMNS = (
    "    order:\n"
    "      - file.name\n"
    "      - title\n"
    "      - type\n"
    "      - epistemic\n"
    "      - status\n"
    "      - privacy\n"
    "      - valid_from\n"
    "      - valid_to\n"
)


def _grouped(name: str, prop: str) -> str:
    return (
        f"  - type: table\n    name: {name}\n"
        f"    groupBy:\n      property: note.{prop}\n      direction: ASC\n" + _MEMORY_COLUMNS
    )


#: Bases views (``boards/<name>.base``). Filters on folder and properties only.
BOARD_FILES: Final[dict[str, str]] = {
    "memories": (
        "filters:\n  and:\n"
        '    - file.inFolder("memories")\n'
        f"    - 'generated == \"{GENERATED}\"'\n"
        "views:\n"
        + _grouped("By type", "type")
        + _grouped("By status", "status")
        + _grouped("By epistemic", "epistemic")
    ),
    "questions": (
        "filters:\n  and:\n"
        '    - file.inFolder("questions")\n'
        f"    - 'generated == \"{GENERATED}\"'\n"
        "views:\n"
        "  - type: table\n    name: Open questions\n"
        "    filters: 'open == true'\n"
        "    order:\n      - file.name\n      - question\n      - missing\n"
        "      - conflicts\n      - status\n"
        "  - type: table\n    name: All questions\n"
        "    order:\n      - file.name\n      - question\n      - open\n"
        "      - resolved_by\n      - valid_to\n"
    ),
    "disputed": (
        "filters:\n  and:\n"
        "    - or:\n"
        '        - file.inFolder("memories")\n'
        '        - file.inFolder("questions")\n'
        f"    - 'generated == \"{GENERATED}\"'\n"
        "    - 'status == \"disputed\"'\n"
        "views:\n" + _grouped("Disputed", "type")
    ),
}


# ------------------------------------------------------------------- writing


def _read_manifest(out: Path) -> dict[str, str]:
    """The previous run's ``path → sha256``; empty on a first run. Refuses a bad manifest."""

    directory = out / MANIFEST_DIR
    path = out / MANIFEST_PATH
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("refusing a manifest reached through a symlink")
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"previous manifest is unreadable: {MANIFEST_PATH}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"previous manifest is not a {GENERATED} manifest")
    files = payload.get("files")
    if payload.get("generator") != GENERATED or not isinstance(files, dict):
        raise ValueError(f"previous manifest is not a {GENERATED} manifest")
    for name, sha in files.items():
        if not isinstance(name, str) or not _SAFE_REL.fullmatch(name):
            raise ValueError("previous manifest lists an unsafe path; refusing to rebuild")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("previous manifest has a malformed sha256")
    return dict(files)


def _directory(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {path.name}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"not a directory: {path.name}")
    return path


def _write(path: Path, data: bytes, tmp_dir: Path) -> None:
    """Write via an ``O_EXCL | O_NOFOLLOW`` file in ``tmp_dir`` and ``os.replace``.

    The temporary file never sits in a page folder, so a crash cannot leave
    page content where git or Obsidian would pick it up.
    """

    stem = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    temporary = tmp_dir / f"{stem}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _current(path: Path) -> bytes | None:
    """A regular file's bytes; ``None`` when absent or a symlink (never followed)."""

    if path.is_symlink():
        return None
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"not a regular file: {path.name}")
    return path.read_bytes()


def _clean_stale_tmp(out: Path) -> list[str]:
    """Remove temp files an interrupted run left (ours only: ``TMP_DIR`` and legacy names)."""

    gone: list[str] = []
    places = [(out / TMP_DIR, TMP_DIR)] + [(out / folder, folder) for folder in FOLDERS]
    for directory, label in places:
        if directory.is_symlink() or not directory.is_dir():
            continue
        for item in sorted(directory.iterdir()):
            hidden_name = label == TMP_DIR or item.name.startswith(".")
            if not hidden_name or not _TMP_NAME.fullmatch(item.name):
                continue
            if item.is_symlink() or item.is_file():
                item.unlink()
                gone.append(f"{label}/{item.name}")
    return gone


def _apply(
    out: Path, files: Mapping[str, bytes], previous: Mapping[str, str], manifest: bytes
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    for folder in (*FOLDERS, MANIFEST_DIR, TMP_DIR):
        if (out / folder).is_symlink():
            raise ValueError(f"refusing to write through a symlink: {folder}")
    stale = tuple(_clean_stale_tmp(out)) if out.is_dir() else ()
    # Plan first: refuse before anything is written.
    edited: list[str] = []
    for name in sorted(previous):
        path = out / name
        if path.is_symlink():
            edited.append(name)
            continue
        data = _current(path)
        if data is not None and hashlib.sha256(data).hexdigest() != previous[name]:
            edited.append(name)
    for name in sorted(files):
        path = out / name
        if name in previous:
            continue
        if path.is_symlink() or (path.exists() and _current(path) != files[name]):
            raise ValueError(
                f"refusing to overwrite {name}: it exists and is not in the previous manifest"
            )
    # Act.
    _directory(out)
    _directory(out / MANIFEST_DIR)
    tmp_dir = _directory(out / TMP_DIR)
    manifest_path = out / MANIFEST_PATH
    written: list[str] = []
    unchanged: list[str] = []
    deleted: list[str] = []
    if any(name not in previous for name in files):
        # A crash mid-run must not leave new files the next run cannot claim.
        interim = {**previous}
        interim.update({name: hashlib.sha256(files[name]).hexdigest() for name in files})
        _write(manifest_path, _interim_manifest(manifest, interim), tmp_dir)
    for name in sorted(files):
        path = out / name
        _directory(path.parent)
        if not path.is_symlink() and _current(path) == files[name]:
            unchanged.append(name)
            continue
        _write(path, files[name], tmp_dir)
        written.append(name)
    for name in sorted(previous):
        if name in files:
            continue
        path = out / name
        if path.is_symlink() or path.is_file():
            path.unlink()
            deleted.append(name)
    if _current(manifest_path) != manifest:
        _write(manifest_path, manifest, tmp_dir)
    return tuple(written), tuple(unchanged), tuple(deleted), tuple(edited), stale


def _interim_manifest(final: bytes, files: Mapping[str, str]) -> bytes:
    payload = json.loads(final.decode("utf-8"))
    payload["files"] = dict(sorted(files.items()))
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


__all__ = [
    "BOARD_FILES",
    "CLEARANCE_LEVELS",
    "DEFAULT_CLEARANCE",
    "GENERATED",
    "MANIFEST_PATH",
    "PROTECTED_ENV",
    "TMP_DIR",
    "CorroborationLookup",
    "ObsidianProjector",
    "ObsidianResult",
    "parse_clearance",
    "privacy_level",
    "protected_reason",
    "safe_name",
]
