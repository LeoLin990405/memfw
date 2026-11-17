"""The GraphRAG ``Deriver``: an offline batch index over a projection export, read back.

Three steps, each its own call; only the middle one runs GraphRAG:

1. :meth:`GraphRagDeriver.stage_input` reads a generation written by
   ``memfw projection export`` (its ``manifest.json`` and
   ``files/memories/<id>.md``) and copies the documents that may leave the
   vault into ``<root>/input/<id>.v<version>.txt``. A document is copied only
   when the manifest classes it ``none``, its bytes match the manifest's
   ``content_sha256``, and the vault, read again now, still holds it as
   ``public`` or ``shared`` (:mod:`._select`). The input directory belongs to
   the adapter: any other ``*.txt`` there is removed.
2. :meth:`GraphRagDeriver.run_index` runs ``graphrag index --root <root>`` as a
   subprocess (pinned :data:`EXTERNAL_VERSION`), with a small environment
   (``PATH``, ``HOME``, locale, ``DO_NOT_TRACK=1`` plus the variable names the
   settings pass through), a timeout and its output in ``<root>/logs``.
   Before it starts, every URL in ``<root>/settings.yaml`` must be a loopback
   address, and at least one must be set (GraphRAG's default model endpoint
   is a cloud API), unless the adapter settings say ``cloud: true``. memfw
   never runs it on its own, and the tests never run it.
3. :meth:`GraphRagDeriver.derive` reads GraphRAG's output tables (CSV; set
   ``table_provider: {type: csv}`` in ``settings.yaml``) from
   ``<root>/output`` into a read-only ``DerivedView``: kind ``graph``
   (entities and relationships) or ``reports`` (community reports). Each row
   is traced back through ``text_unit_ids`` → ``text_units.document_id`` →
   ``documents.title`` (``<id>.v<version>.txt``) to memories; it is kept only
   when every memory behind it is in the request, still shareable and still at
   that version. The view cites exactly those memories; caps ``inference`` /
   causal ``association``. :func:`candidates` turns rows into staging
   candidates (``causal: none``), never memory.
"""

from __future__ import annotations

import csv
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._ports import (
    AdapterSpec,
    Availability,
    Citation,
    DerivedView,
    DeriveRequest,
    IngestItem,
    MemoryFile,
    read_memory,
)
from ._select import is_shareable, shareable_memories

__version__ = "0.1.0"

NAME = "graphrag"
#: The GraphRAG release whose CLI and output tables this adapter reads.
EXTERNAL_VERSION = "3.3.0"
SOURCE = "https://github.com/microsoft/graphrag"
LICENSE = "MIT"
KINDS: tuple[str, ...] = ("graph", "reports")
METHODS: tuple[str, ...] = ("standard", "fast")
VIEW_FORMAT = "memfw-graphrag-v1"
INPUT_DIR = "input"
OUTPUT_DIR = "output"
LOG_DIR = "logs"
INPUT_INDEX = ".memfw-input.json"
#: The input file name this adapter gives a memory, and so GraphRAG's document title.
TITLE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.v([0-9]{1,9})\.txt$")
_MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
#: A list cell as GraphRAG's CSV writer leaves it: a Python or NumPy repr.
_QUOTED_RE = re.compile(r"'([^'\\]*)'|\"([^\"\\]*)\"")
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"#,]+")
#: Variables every run gets from the parent, when set.
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
FORBIDDEN_OPTIONS = frozenset({"api_key", "key", "token", "authorization", "password"})
MAX_CELL_CHARS = 1 << 22
MAX_NAME_CHARS = 120
MAX_TYPE_CHARS = 40
MAX_DESCRIPTION_CHARS = 400


class GraphRagError(RuntimeError):
    """A step failed. ``code`` (e.g. ``index_failed``) is all it carries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Settings:
    """The adapter's settings (``adapters.graphrag`` in the operator's config)."""

    #: The GraphRAG project directory (holds settings.yaml; input/ and output/ inside).
    root: str = ""
    #: The ``graphrag`` executable (a name on PATH or an absolute path).
    command: str = "graphrag"
    method: str = "standard"
    timeout: float = 6 * 3600.0
    #: Allow a non-loopback model endpoint in settings.yaml.
    cloud: bool = False
    #: Names of extra environment variables the run may see (e.g. a local
    #: endpoint's key variable). Values are never in the settings.
    pass_env: tuple[str, ...] = ()
    max_memories: int = 200
    max_rows: int = 100
    max_report_chars: int = 2_000

    def __post_init__(self) -> None:
        if not isinstance(self.root, str) or len(self.root) > 1024:
            raise ValueError("bad_option")
        if not isinstance(self.command, str) or not self.command or len(self.command) > 1024:
            raise ValueError("bad_option")
        if self.method not in METHODS or not isinstance(self.cloud, bool):
            raise ValueError("bad_option")
        timeout = self.timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("bad_option")
        if not isinstance(self.pass_env, tuple) or not all(
            isinstance(name, str) and _ENV_NAME_RE.fullmatch(name) for name in self.pass_env
        ):
            raise ValueError("bad_option")
        for name in ("max_memories", "max_rows", "max_report_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("bad_option")

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any] | None) -> Settings:
        options = dict(settings or {})
        if FORBIDDEN_OPTIONS & {str(name).lower() for name in options}:
            raise ValueError("key_in_config")
        if set(options) - {item.name for item in fields(cls)}:
            raise ValueError("bad_option")
        if isinstance(options.get("pass_env"), list):
            options["pass_env"] = tuple(options["pass_env"])
        return replace(cls(), **options)


# -- settings.yaml gate ------------------------------------------------------------------


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint_check(settings_text: str, *, cloud: bool) -> str:
    """``""`` when GraphRAG's settings may run here, else a refusal code.

    A line scan, not a YAML parse: every URL anywhere in the file (comments
    included) must be http(s) to a loopback host, and at least one must be
    present, unless ``cloud`` is true. A ``${VAR}`` placeholder in a URL is a
    refusal too: its value is not known here.
    """

    if cloud:
        return ""
    urls = _URL_RE.findall(settings_text)
    if not urls:
        return "no_local_endpoint"
    for url in urls:
        if "${" in url:
            return "endpoint_placeholder"
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not _loopback(parts.hostname or ""):
            return "cloud_not_allowed"
    return ""


# -- reading GraphRAG's CSV tables (data, never trusted) ----------------------------------


def parse_list(cell: Any) -> list[str]:
    """The quoted items of a list cell (``"['a', 'b']"`` or NumPy's ``"['a' 'b']"``)."""

    if not isinstance(cell, str) or not cell.strip().startswith("["):
        return []
    return [a or b for a, b in _QUOTED_RE.findall(cell)]


def read_table(directory: Path, name: str) -> list[dict[str, str]]:
    """Rows of ``<directory>/<name>.csv``; ``[]`` when the table is missing."""

    path = directory / f"{name}.csv"
    if path.is_symlink() or not path.is_file():
        return []
    limit = csv.field_size_limit()
    csv.field_size_limit(max(limit, MAX_CELL_CHARS))
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise GraphRagError("bad_output") from exc
    finally:
        csv.field_size_limit(limit)


def _short(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


@dataclass(frozen=True)
class Output:
    """GraphRAG's tables, with every text unit mapped to the memory behind it."""

    unit_memory: Mapping[str, tuple[str, int]]
    entities: Sequence[Mapping[str, str]]
    relationships: Sequence[Mapping[str, str]]
    communities: Sequence[Mapping[str, str]]
    reports: Sequence[Mapping[str, str]]


def read_output(directory: Path) -> Output:
    documents: dict[str, tuple[str, int]] = {}
    for row in read_table(directory, "documents"):
        match = TITLE_RE.fullmatch(row.get("title", ""))
        if match and row.get("id"):
            documents[row["id"]] = (match.group(1), int(match.group(2)))
    units: dict[str, tuple[str, int]] = {}
    for row in read_table(directory, "text_units"):
        memory = documents.get(row.get("document_id", ""))
        if memory and row.get("id"):
            units[row["id"]] = memory
    return Output(
        units,
        read_table(directory, "entities"),
        read_table(directory, "relationships"),
        read_table(directory, "communities"),
        read_table(directory, "community_reports"),
    )


def _memories_of(
    unit_ids: Iterable[str], output: Output, current: Mapping[str, int]
) -> list[str] | None:
    """Memory ids behind ``unit_ids`` when every unit maps to a requested, current memory."""

    ids: list[str] = []
    seen_unit = False
    for unit_id in unit_ids:
        seen_unit = True
        memory = output.unit_memory.get(unit_id)
        if memory is None or current.get(memory[0]) != memory[1]:
            return None
        if memory[0] not in ids:
            ids.append(memory[0])
    return sorted(ids) if seen_unit else None


def graph_rows(
    output: Output, current: Mapping[str, int], *, max_rows: int
) -> list[dict[str, Any]]:
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in output.entities:
        sources = _memories_of(parse_list(item.get("text_unit_ids")), output, current)
        name = _short(item.get("title"), MAX_NAME_CHARS)
        if sources is None or not name:
            continue
        rows.setdefault(
            ("entity", name.lower()),
            {
                "type": "entity",
                "name": name,
                "entity_type": _short(item.get("type"), MAX_TYPE_CHARS).lower() or "unknown",
                "description": _short(item.get("description"), MAX_DESCRIPTION_CHARS),
                "sources": sources,
            },
        )
    for item in output.relationships:
        sources = _memories_of(parse_list(item.get("text_unit_ids")), output, current)
        source = _short(item.get("source"), MAX_NAME_CHARS)
        target = _short(item.get("target"), MAX_NAME_CHARS)
        if sources is None or not source or not target:
            continue
        rows.setdefault(
            ("relation", source.lower(), target.lower()),
            {
                "type": "relation",
                "source": source,
                "target": target,
                "description": _short(item.get("description"), MAX_DESCRIPTION_CHARS),
                "sources": sources,
            },
        )
    return [rows[key] for key in sorted(rows)][:max_rows]


def report_rows(
    output: Output, current: Mapping[str, int], *, max_rows: int, max_chars: int
) -> list[dict[str, Any]]:
    """Community reports whose whole community comes from requested, current memories."""

    community_units = {
        row.get("community", ""): parse_list(row.get("text_unit_ids"))
        for row in output.communities
        if row.get("community", "") != ""
    }
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in output.reports:
        community = item.get("community", "")
        sources = _memories_of(community_units.get(community, ()), output, current)
        title = _short(item.get("title"), MAX_NAME_CHARS)
        if sources is None or not title:
            continue
        level = item.get("level", "")
        rows.setdefault(
            (int(level) if level.isdigit() else 0, community, title.lower()),
            {
                "type": "report",
                "community": _short(community, 20),
                "level": int(level) if level.isdigit() else None,
                "title": title,
                "summary": _short(item.get("summary"), max_chars),
                "sources": sources,
            },
        )
    return [rows[key] for key in sorted(rows)][:max_rows]


def candidates(view: DerivedView) -> list[IngestItem]:
    """Staging candidates from a view this adapter built (``causal: none`` on each)."""

    if view.deriver != NAME:
        raise ValueError("not a graphrag view")
    payload = json.loads(view.content)
    if not isinstance(payload, dict) or payload.get("format") != VIEW_FORMAT:
        raise ValueError("not a graphrag view")
    cited = {citation.memory_id for citation in view.citations}
    items: list[IngestItem] = []
    for row in payload["rows"]:
        sources = list(row["sources"])
        if not set(sources) <= cited:
            raise ValueError("view cites less than its rows")
        if row["type"] == "entity":
            title, ref = f"entity: {row['name']}", f"{NAME}:entity:{row['name']}"
        elif row["type"] == "relation":
            title = f"relation: {row['source']} -> {row['target']}"
            ref = f"{NAME}:relation:{row['source']}->{row['target']}"
        else:
            title, ref = f"report: {row['title']}", f"{NAME}:report:{row['community']}"
        metadata: dict[str, str | int | float | bool | None] = {
            "deriver": NAME,
            "kind": view.kind,
            "external_version": EXTERNAL_VERSION,
            "sources": ",".join(sources),
            "source_count": len(sources),
            "epistemic": "inference",
            "causal": "none",
            "untrusted_model_output": True,
        }
        body = json.dumps(row, ensure_ascii=False, sort_keys=True)
        items.append(IngestItem(title[:120], body, ref[:300], metadata))
    return items


# -- the deriver ----------------------------------------------------------------------------

#: ``(argv, env, cwd, timeout, log path) -> exit status``; the one place a process starts.
ProcessRunner = Callable[[Sequence[str], Mapping[str, str], Path, float, Path], int]


def run_process(
    argv: Sequence[str], env: Mapping[str, str], cwd: Path, timeout: float, log: Path
) -> int:
    with log.open("ab") as out:
        try:
            done = subprocess.run(  # noqa: S603 - argv from operator settings, no shell
                list(argv),
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return -1
    return done.returncode


@dataclass(frozen=True)
class StageResult:
    """Memory ids copied into the input directory, and those held back."""

    staged: tuple[str, ...]
    skipped: tuple[str, ...]


class GraphRagDeriver:
    """``Deriver`` ``graphrag`` (kinds ``graph`` and ``reports``). See the module docstring."""

    def __init__(
        self,
        settings: Settings,
        *,
        refused: str = "",
        runner: ProcessRunner | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.name = NAME
        self.settings = settings
        self.refused = refused
        if not refused:
            if not settings.root:
                self.refused = "no_root"
            elif not Path(settings.root).is_absolute():
                self.refused = "bad_root"
        self._runner = runner or run_process
        self._environ = os.environ if environ is None else environ

    def __repr__(self) -> str:
        return f"GraphRagDeriver(root={self.settings.root!r}, refused={self.refused!r})"

    @property
    def root(self) -> Path:
        return Path(self.settings.root)

    def available(self) -> Availability:
        if self.refused:
            return Availability.missing(self.refused)
        if not self.root.is_dir() or self.root.is_symlink():
            return Availability.missing("no_root")
        return Availability.ok()

    def _ensure(self) -> None:
        availability = self.available()
        if not availability.available:
            raise GraphRagError(availability.reason)

    # -- 1. input -----------------------------------------------------------------------

    def stage_input(self, projection: Path, vault: Path) -> StageResult:
        """Copy the shareable documents of one projection generation into ``<root>/input``."""

        self._ensure()
        generation = Path(projection)
        root_vault = Path(vault).expanduser().resolve()
        root = self.root.resolve()
        if root == root_vault or root_vault in root.parents:
            raise GraphRagError("root_inside_vault")
        try:
            manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise GraphRagError("bad_projection") from exc
        documents = manifest.get("documents") if isinstance(manifest, dict) else None
        if not isinstance(documents, list):
            raise GraphRagError("bad_projection")
        wanted: dict[str, tuple[MemoryFile, bytes]] = {}
        skipped: list[str] = []
        for document in documents:
            memory_id = document.get("id") if isinstance(document, dict) else None
            if not isinstance(memory_id, str) or not _MEMORY_ID_RE.fullmatch(memory_id):
                continue
            meta = document.get("meta")
            privacy_class = meta.get("privacy_class") if isinstance(meta, dict) else None
            path = generation / "files" / "memories" / f"{memory_id}.md"
            memory = read_memory(root_vault, memory_id)
            try:
                data = b"" if path.is_symlink() else path.read_bytes()
            except OSError:
                data = b""
            ok = (
                privacy_class == "none"
                and data
                and hashlib.sha256(data).hexdigest() == document.get("content_sha256")
                and memory is not None
                and is_shareable(memory)
            )
            if not ok or memory is None or len(wanted) >= self.settings.max_memories:
                skipped.append(memory_id)
                continue
            wanted[memory_id] = (memory, data)
        input_dir = self.root / INPUT_DIR
        if input_dir.is_symlink():
            raise GraphRagError("bad_root")
        input_dir.mkdir(mode=0o700, exist_ok=True)
        names = {
            f"{mid}.v{memory.version() or 0}.txt": data for mid, (memory, data) in wanted.items()
        }
        for existing in input_dir.iterdir():
            if existing.name not in names and existing.suffix == ".txt":
                existing.unlink()
        for name, data in sorted(names.items()):
            target = input_dir / name
            if target.is_symlink():
                target.unlink()
            temp = input_dir / f".{name}.tmp"
            temp.write_bytes(data)
            os.replace(temp, target)
        index = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(names.items())}
        (input_dir / INPUT_INDEX).write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
        return StageResult(tuple(wanted), tuple(skipped))

    # -- 2. the batch run ------------------------------------------------------------------

    def command(self) -> list[str]:
        executable = self.settings.command
        resolved = executable if os.path.isabs(executable) else shutil.which(executable)
        if not resolved:
            raise GraphRagError("missing_dependency")
        return [resolved, "index", "--root", str(self.root), "--method", self.settings.method]

    def run_env(self) -> dict[str, str]:
        names = (*BASE_ENV, *self.settings.pass_env)
        env = {name: self._environ[name] for name in names if self._environ.get(name)}
        env["DO_NOT_TRACK"] = "1"
        return env

    def run_index(self) -> int:
        """Run ``graphrag index`` once, after the endpoint gate. Returns the exit status."""

        self._ensure()
        settings_file = self.root / "settings.yaml"
        try:
            text = settings_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise GraphRagError("no_settings") from exc
        refused = endpoint_check(text, cloud=self.settings.cloud)
        if refused:
            raise GraphRagError(refused)
        if not (self.root / INPUT_DIR / INPUT_INDEX).is_file():
            raise GraphRagError("no_input")
        logs = self.root / LOG_DIR
        logs.mkdir(mode=0o700, exist_ok=True)
        status = self._runner(
            self.command(), self.run_env(), self.root, float(self.settings.timeout),
            logs / "memfw-graphrag-index.log",
        )  # fmt: skip
        if status != 0:
            raise GraphRagError("index_timeout" if status == -1 else "index_failed")
        return status

    # -- 3. reading the output back --------------------------------------------------------

    def derive(self, vault: Path, request: DeriveRequest) -> DerivedView:
        if request.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        self._ensure()
        memories = {m.memory_id: m for m in shareable_memories(vault, request.memory_ids)}
        if not memories:
            raise ValueError("none of the requested memories exists or may leave the vault")
        current = {memory_id: memory.version() or 0 for memory_id, memory in memories.items()}
        output = read_output(self.root / OUTPUT_DIR)
        if request.kind == "graph":
            rows = graph_rows(output, current, max_rows=self.settings.max_rows)
        else:
            rows = report_rows(
                output,
                current,
                max_rows=self.settings.max_rows,
                max_chars=self.settings.max_report_chars,
            )
        if not rows:
            raise ValueError("no rows from the requested memories in the graphrag output")
        cited = {memory_id for row in rows for memory_id in row["sources"]}
        content = json.dumps(
            {
                "format": VIEW_FORMAT,
                "external_version": EXTERNAL_VERSION,
                "kind": request.kind,
                "rows": rows,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return DerivedView(
            deriver=self.name,
            kind=request.kind,
            content=content,
            citations=tuple(
                Citation(memory_id, memory.version())
                for memory_id, memory in memories.items()
                if memory_id in cited
            ),
            epistemic_cap="inference",
            causal_cap="association",
        )


def factory(settings: Mapping[str, Any] | None = None) -> GraphRagDeriver:
    """The deriver for ``adapters: {enabled: [graphrag], graphrag: {root: ...}}``.

    A bad setting still returns a deriver that reports ``available() == False``
    with the code (``bad_option``, ``key_in_config``, ``no_root``, ``bad_root``).
    """

    try:
        parsed = Settings.from_mapping(settings)
    except (TypeError, ValueError) as exc:
        code = str(exc) if str(exc) in {"bad_option", "key_in_config"} else "bad_option"
        return GraphRagDeriver(Settings(), refused=code)
    return GraphRagDeriver(parsed)


SPEC = AdapterSpec(
    name=NAME,
    kind="deriver",
    factory=factory,
    version=EXTERNAL_VERSION,
    source=SOURCE,
    license=LICENSE,
)

__all__ = [
    "EXTERNAL_VERSION",
    "KINDS",
    "NAME",
    "SPEC",
    "VIEW_FORMAT",
    "GraphRagDeriver",
    "GraphRagError",
    "Output",
    "ProcessRunner",
    "Settings",
    "StageResult",
    "__version__",
    "candidates",
    "endpoint_check",
    "factory",
    "graph_rows",
    "parse_list",
    "read_output",
    "read_table",
    "report_rows",
    "run_process",
]
