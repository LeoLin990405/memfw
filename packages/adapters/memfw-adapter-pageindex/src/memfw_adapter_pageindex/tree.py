"""Document trees: build (Markdown), import (PageIndex JSON), store and load.

A tree is the table of contents of one long document, in PageIndex's tree
format: a ``structure`` list of nodes, each with ``title``, ``node_id`` and
``nodes`` (children), plus ``line_num`` for Markdown or ``start_index`` /
``end_index`` (1-based physical pages) for a PDF, and an optional
``summary``. Node text is never stored: :func:`import_pageindex_tree` drops
``text`` and :func:`build_markdown_tree` never reads it into the tree.

Each stored tree is one JSON file (schema :data:`TREE_SCHEMA`) in an index
directory the operator names. The directory lives outside the vault; nothing
here reads or writes a vault.

Sources:

* ``kh:<relative path>.md``: a Knowledge-Hub note, built here from its
  Markdown headings (no model).
* ``kh:<relative path>.pdf`` or ``att:<attachment id>``: a PDF; its tree is
  produced by PageIndex outside memfw and imported with
  :func:`import_pageindex_tree`.

Refs (what search returns): ``kh:<path>#<heading>`` for a Markdown section
and ``<source>#page=<n>`` for a PDF section, ``n`` being the section's first
page (1-based). That is the page part of memfw's multimodal locator
(``#page=N[&bbox=...]``); PageIndex has no boxes, so no ``bbox`` is emitted.
Characters a path ref may not hold (space, quotes, angle brackets,
backslash, backtick, control characters) and ``%`` and ``#`` are
percent-encoded (UTF-8).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ._ports import PRIVACY_LEVELS, field_level, metadata_level, strictest

TREE_SCHEMA = "memfw-pageindex-tree/1"
#: Caps on one tree (a larger one is refused, not cut).
MAX_NODES = 5000
MAX_DEPTH = 12
MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 300
MAX_TREE_BYTES = 8 << 20
MAX_NOTE_BYTES = 16 << 20
MAX_REF_CHARS = 512

_ATT_ID = re.compile(r"^att-[A-Za-z0-9][A-Za-z0-9_-]{0,123}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TENANT = re.compile(r"^[A-Za-z0-9_.:@+-]{0,128}$")
_NODE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
_SPACE = re.compile(r"\s+")
_UNSAFE = frozenset("\"'<>`\\%#")


class TreeError(ValueError):
    """A tree, source or index entry was refused. ``code`` is a short code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# -- sources and refs -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """A parsed ``kh:`` or ``att:`` source. ``media`` is ``markdown`` or ``pdf``."""

    ref: str
    scheme: str
    path: str
    media: str


def parse_source(ref: str) -> Source:
    """Check a source ref; :class:`TreeError` (``bad_source``) when it is not one."""

    if not isinstance(ref, str) or len(ref) > 400 or any(ord(c) < 0x20 for c in ref):
        raise TreeError("bad_source")
    if ref.startswith("att:"):
        ident = ref[4:]
        if not _ATT_ID.fullmatch(ident):
            raise TreeError("bad_source")
        return Source(ref, "att", ident, "pdf")
    if ref.startswith("kh:"):
        rel = ref[3:]
        pure = PurePosixPath(rel)
        if (
            not rel
            or rel.startswith("/")
            or "\\" in rel
            or "#" in rel
            or any(part in ("", ".", "..") for part in rel.split("/"))
        ):
            raise TreeError("bad_source")
        suffix = pure.suffix.lower()
        if suffix == ".md":
            return Source(ref, "kh", rel, "markdown")
        if suffix == ".pdf":
            return Source(ref, "kh", rel, "pdf")
    raise TreeError("bad_source")


def encode_part(text: str) -> str:
    """Percent-encode what a memfw path ref may not hold (and ``%``, ``#``)."""

    out: list[str] = []
    for char in text:
        if char in _UNSAFE or char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            out.append(char)
    return "".join(out)


def source_prefix(source: Source) -> str:
    return f"{source.scheme}:{encode_part(source.path)}"


def node_ref(source: Source, node: Mapping[str, Any]) -> str | None:
    """The ref for one node, or ``None`` when it cannot be made (too long, no page)."""

    prefix = source_prefix(source)
    if source.media == "pdf":
        page = node.get("start_index")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            return None
        ref = f"{prefix}#page={page}"
    else:
        room = MAX_REF_CHARS - len(prefix) - 1
        # cut on character boundaries, before encoding: never half a character
        pieces: list[str] = []
        used = 0
        for char in str(node.get("title", "")):
            piece = encode_part(char)
            if used + len(piece) > room:
                break
            pieces.append(piece)
            used += len(piece)
        cut = "".join(pieces)
        if not cut:
            return None
        ref = f"{prefix}#{cut}"
    return ref if len(ref) <= MAX_REF_CHARS else None


# -- nodes --------------------------------------------------------------------------


def _clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise TreeError("bad_node")
    title = _SPACE.sub(" ", value).strip()[:MAX_TITLE_CHARS].strip()
    if not title:
        raise TreeError("bad_node")
    return title


def iter_nodes(nodes: Sequence[Mapping[str, Any]], depth: int = 0) -> Iterator[tuple[int, Any]]:
    for node in nodes:
        yield depth, node
        yield from iter_nodes(node.get("nodes", ()), depth + 1)


def _privacy(level: str) -> str:
    value = field_level(level)
    if value not in PRIVACY_LEVELS:
        raise TreeError("bad_privacy")
    return value


def _check_tenant(tenant: str) -> str:
    if not isinstance(tenant, str) or not _TENANT.fullmatch(tenant):
        raise TreeError("bad_tenant")
    return tenant


def _now(now: datetime | None) -> str:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise TreeError("naive_time")
    return moment.astimezone(UTC).isoformat()


# -- Markdown (built here, no model) ------------------------------------------------


def _open_inside(root: Path, rel: str) -> bytes:
    """Read ``root/rel``: no symlink anywhere below ``root``, must stay inside it."""

    base = root.resolve(strict=True)
    current = base
    for part in PurePosixPath(rel).parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise TreeError("source_missing") from None
        if stat.S_ISLNK(info.st_mode):
            raise TreeError("symlink_refused")
    if not current.resolve(strict=True).is_relative_to(base):
        raise TreeError("outside_root")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(current, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TreeError("not_a_file")
        if info.st_size > MAX_NOTE_BYTES:
            raise TreeError("too_large")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(MAX_NOTE_BYTES + 1)
    finally:
        os.close(fd)


#: The frontmatter keys that decide a note's privacy (``memfw.ports.privacy.metadata_level``).
PRIVACY_KEYS = ("privacy", "tags", "sensitivity")


#: A line that sets a frontmatter key deciding a note's privacy.
_PRIVACY_KEY_LINE_RE = re.compile(r"^(?:privacy|sensitivity|tags?)\s*:", re.IGNORECASE)
_PRIVACY_VIEW_KEYS = ("privacy", "sensitivity", "tags", "tag")
_MAX_CLOSE_CANDIDATES = 64


def _indented(line: str) -> bool:
    return line[:1] in (" ", "\t")


def _yaml_mapping(lines: Sequence[str]) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load("".join(lines)) or {}
    except (yaml.YAMLError, RecursionError, ValueError, TypeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _privacy_view(mapping: dict[str, Any] | None) -> list[Any] | None:
    if mapping is None:
        return None
    return [mapping.get(key) for key in _PRIVACY_VIEW_KEYS]


def frontmatter_close(lines: Sequence[str]) -> tuple[int | None, bool]:
    """``(index of the line closing the frontmatter, ambiguous)``: memfw's rule.

    A copy of ``memfw.notes.index.frontmatter_close`` (kept in parity by the
    tests; adapters never import memfw internals). A column-0 ``---`` closes
    the block. An indented first ``---`` may be YAML scalar content (a
    ``|``/``>`` block scalar line): the block then closes at the last
    candidate, up to the first column-0 one, whose text before it is still a
    YAML mapping. ``ambiguous`` (fail closed) when that reading and the one
    stopping at the first candidate differ on ``privacy``/``sensitivity``/
    ``tags``/``tag``, or a line setting one of them is left after the close
    and before the first column-0 candidate (or the end of the note).
    """

    candidates = [index for index in range(1, len(lines)) if lines[index].strip() == "---"]
    if not candidates:
        return None, False
    first = candidates[0]
    if not _indented(lines[first]):
        return first, False
    hard = next((index for index in candidates if not _indented(lines[index])), None)
    limit = hard if hard is not None else len(lines)
    short = _yaml_mapping(lines[1:first])
    end, chosen = first, short
    later = [index for index in candidates[1:] if index <= limit]
    for index in later[:_MAX_CLOSE_CANDIDATES]:
        loaded = _yaml_mapping(lines[1:index])
        if loaded is not None:
            end, chosen = index, loaded
    ambiguous = end != first and _privacy_view(short) != _privacy_view(chosen)
    if any(_PRIVACY_KEY_LINE_RE.match(line) for line in lines[end + 1 : limit]):
        ambiguous = True
    return end, ambiguous


def _frontmatter_block(text: str) -> tuple[str | None, int, bool]:
    """``(frontmatter text or None, first body line index, malformed)``.

    The same split as memfw's notes index (``memfw.notes.index.split_frontmatter``):
    the text must start with ``---`` and its first line be ``---`` (surrounding
    whitespace ignored); the block closes where :func:`frontmatter_close`
    says (a ``...`` line never closes it). Lines split like
    ``str.splitlines``. An opening line without a close, or an ambiguous
    close, is malformed: the notes index would read no frontmatter at all
    (or force ``local-private``), the privacy reader here fails closed.
    """

    if not text.startswith("---"):
        return None, 0, False
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, 0, False
    end, ambiguous = frontmatter_close(lines)
    if end is None:
        return None, 0, True
    return "".join(lines[1:end]), end + 1, ambiguous


def note_level(text: str, operator: str) -> str:
    """The note's level: the operator's ``privacy`` and the note's own keys, strictest wins.

    The frontmatter is parsed with ``yaml.safe_load`` (as memfw parses it) and
    the level is ``memfw.ports.privacy.metadata_level`` of that mapping (the
    ``privacy`` field, every ``privacy:*`` / ``sensitivity:*`` tag, a top-level
    ``sensitivity``), with the operator's level standing in for a missing
    ``privacy`` field. Fail closed to ``local-private``: a YAML error, an
    unclosed block, frontmatter that is not a mapping, a ``tags`` that is not a
    list, or any unrecognised value.
    """

    block, _, malformed = _frontmatter_block(text)
    if malformed:
        return "local-private"
    if block is None:
        return operator
    try:
        meta = yaml.safe_load(block)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        return "local-private"
    if meta is None:
        return operator  # an empty block
    if not isinstance(meta, dict) or not all(isinstance(k, str) for k in meta):
        return "local-private"
    tags = meta.get("tags")
    if tags is not None and not isinstance(tags, list):
        return "local-private"  # a scalar or mapping ``tags`` the kernel would not read
    level = strictest([operator, metadata_level(meta, legacy=operator)])
    return level if level in PRIVACY_LEVELS else "local-private"


def markdown_structure(text: str) -> list[dict[str, Any]]:
    """PageIndex-shaped nodes from ATX headings (outside fenced code), no text."""

    lines = text.splitlines()
    _, start, _ = _frontmatter_block(text)
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    fence: str | None = None
    counter = 0
    for number, line in enumerate(lines[start:], start + 1):
        opened = _FENCE.match(line)
        if opened:
            marker = opened.group(1)
            fence = None if fence == marker else (fence or marker)
            continue
        if fence is not None:
            continue
        match = _HEADING.match(line.strip())
        if not match:
            continue
        try:
            title = _clean_title(match.group(2))
        except TreeError:
            continue
        counter += 1
        if counter > MAX_NODES:
            raise TreeError("too_many_nodes")
        level = len(match.group(1))
        node: dict[str, Any] = {
            "title": title,
            "node_id": f"{counter:04d}",
            "line_num": number,
            "nodes": [],
        }
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1]["nodes"] if stack else roots).append(node)
        stack.append((level, node))
    return roots


def build_markdown_tree(
    root: Path,
    source_ref: str,
    *,
    privacy: str = "local-private",
    tenant: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """The tree of one ``kh:<path>.md`` note under ``root`` (the Knowledge-Hub root).

    ``privacy`` is the level the operator gives; the note's frontmatter can
    only make it stricter (:func:`note_level`: ``privacy``, ``privacy:*`` and
    ``sensitivity:*`` tags, ``sensitivity``; unreadable → ``local-private``).
    """

    source = parse_source(source_ref)
    if source.scheme != "kh" or source.media != "markdown":
        raise TreeError("not_markdown")
    raw = _open_inside(Path(root), source.path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise TreeError("not_utf8") from None
    level = note_level(text, _privacy(privacy))
    structure = markdown_structure(text)
    if not structure:
        raise TreeError("no_headings")
    return _tree(
        source,
        structure,
        privacy=level,
        tenant=tenant,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        producer={"tool": "memfw-adapter-pageindex", "version": _version()},
        now=now,
        pages=None,
    )


# -- PDF (imported from PageIndex) --------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _import_nodes(
    raw: Any, *, pages: int, keep_summaries: bool, depth: int, seen: set[str], count: list[int]
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise TreeError("bad_tree")
    if depth > MAX_DEPTH:
        raise TreeError("too_deep")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise TreeError("bad_node")
        count[0] += 1
        if count[0] > MAX_NODES:
            raise TreeError("too_many_nodes")
        node_id = item.get("node_id", f"{count[0]:04d}")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id) or node_id in seen:
            raise TreeError("bad_node_id")
        seen.add(node_id)
        start, end = item.get("start_index"), item.get("end_index", item.get("start_index"))
        if not _is_int(start) or not _is_int(end):
            raise TreeError("bad_page")
        assert isinstance(start, int) and isinstance(end, int)
        if not 1 <= start <= end <= pages:
            raise TreeError("page_out_of_range")
        node: dict[str, Any] = {
            "title": _clean_title(item.get("title")),
            "node_id": node_id,
            "start_index": start,
            "end_index": end,
        }
        summary = item.get("summary", item.get("prefix_summary"))
        if keep_summaries and isinstance(summary, str) and summary.strip():
            node["summary"] = _SPACE.sub(" ", summary).strip()[:MAX_SUMMARY_CHARS]
        node["nodes"] = _import_nodes(
            item.get("nodes", []),
            pages=pages,
            keep_summaries=keep_summaries,
            depth=depth + 1,
            seen=seen,
            count=count,
        )
        out.append(node)
    return out


def import_pageindex_tree(
    data: Any,
    source_ref: str,
    *,
    pages: int,
    source_sha256: str,
    privacy: str = "local-private",
    tenant: str = "",
    keep_summaries: bool = False,
    pageindex_version: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """A stored tree from PageIndex's PDF output (``{"structure": [...]}`` or the list).

    Validated, never repaired: page ranges must lie in ``1..pages``, node ids
    must be unique. ``text`` is always dropped; ``summary`` (or
    ``prefix_summary``) is kept, cut to 300 characters, only with
    ``keep_summaries`` (a summary is derived from the body and is sent to the
    search model). ``doc_name`` and ``doc_description`` are dropped.
    """

    source = parse_source(source_ref)
    if source.media != "pdf":
        raise TreeError("not_pdf")
    if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 100_000:
        raise TreeError("bad_pages")
    if not isinstance(source_sha256, str) or not _SHA256.fullmatch(source_sha256):
        raise TreeError("bad_sha256")
    structure = data.get("structure") if isinstance(data, Mapping) else data
    nodes = _import_nodes(
        structure, pages=pages, keep_summaries=keep_summaries, depth=0, seen=set(), count=[0]
    )
    if not nodes:
        raise TreeError("empty_tree")
    version = pageindex_version.strip()
    if version and not re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", version):
        raise TreeError("bad_version")
    return _tree(
        source,
        nodes,
        privacy=_privacy(privacy),
        tenant=tenant,
        source_sha256=source_sha256,
        producer={"tool": "pageindex", "version": version or "unknown"},
        now=now,
        pages=pages,
    )


def _version() -> str:
    from . import __version__

    return __version__


def _tree(
    source: Source,
    structure: list[dict[str, Any]],
    *,
    privacy: str,
    tenant: str,
    source_sha256: str,
    producer: dict[str, str],
    now: datetime | None,
    pages: int | None,
) -> dict[str, Any]:
    tree: dict[str, Any] = {
        "schema": TREE_SCHEMA,
        "source": source.ref,
        "media": source.media,
        "tenant": _check_tenant(tenant),
        "privacy": privacy,
        "source_sha256": source_sha256,
        "producer": producer,
        "built_at": _now(now),
    }
    if pages is not None:
        tree["pages"] = pages
    tree["structure"] = structure
    validate_tree(tree)
    return tree


# -- validation, storage ------------------------------------------------------------


#: The keys a stored tree and its nodes may hold (anything else is refused on load).
TREE_KEYS = frozenset(
    {
        "schema",
        "source",
        "media",
        "tenant",
        "privacy",
        "source_sha256",
        "producer",
        "built_at",
        "pages",
        "structure",
    }
)
NODE_KEYS = {
    "markdown": frozenset({"title", "node_id", "line_num", "summary", "nodes"}),
    "pdf": frozenset({"title", "node_id", "start_index", "end_index", "summary", "nodes"}),
}


def validate_tree(tree: Any) -> dict[str, Any]:
    """Check a stored tree (on write and on load) with the import rules.

    Known keys only, node ids matching the id rule and unique, titles already
    clean, summaries at most :data:`MAX_SUMMARY_CHARS`, Markdown nodes with a
    line number and PDF nodes with a page range inside ``pages``. Returns the
    tree; :class:`TreeError` otherwise.
    """

    if not isinstance(tree, dict) or tree.get("schema") != TREE_SCHEMA:
        raise TreeError("bad_schema")
    if set(tree) - TREE_KEYS:
        raise TreeError("unknown_key")
    source = parse_source(tree.get("source", ""))
    if tree.get("media") != source.media:
        raise TreeError("bad_media")
    _check_tenant(str(tree.get("tenant", "\x00")))
    if tree.get("privacy") not in PRIVACY_LEVELS:
        raise TreeError("bad_privacy")
    if not isinstance(tree.get("source_sha256"), str) or not _SHA256.fullmatch(
        tree["source_sha256"]
    ):
        raise TreeError("bad_sha256")
    producer = tree.get("producer")
    if not isinstance(producer, dict) or set(producer) != {"tool", "version"}:
        raise TreeError("bad_producer")
    if not all(isinstance(v, str) and len(v) <= 64 for v in producer.values()):
        raise TreeError("bad_producer")
    if not isinstance(tree.get("built_at"), str) or len(tree["built_at"]) > 64:
        raise TreeError("bad_time")
    structure = tree.get("structure")
    if not isinstance(structure, list) or not structure:
        raise TreeError("bad_tree")
    pages = tree.get("pages")
    if source.media == "pdf":
        if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 100_000:
            raise TreeError("bad_pages")
    elif "pages" in tree:
        raise TreeError("unknown_key")
    allowed = NODE_KEYS[source.media]
    seen: set[str] = set()
    total = 0
    for depth, node in iter_nodes(structure):
        total += 1
        if total > MAX_NODES or depth > MAX_DEPTH:
            raise TreeError("too_large")
        if not isinstance(node, dict) or set(node) - allowed:
            raise TreeError("bad_node")
        title = node.get("title")
        if _clean_title(title) != title:
            raise TreeError("bad_node")
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id) or node_id in seen:
            raise TreeError("bad_node_id")
        seen.add(node_id)
        if not isinstance(node.get("nodes"), list):
            raise TreeError("bad_node")
        summary = node.get("summary")
        if summary is not None and (
            not isinstance(summary, str) or len(summary) > MAX_SUMMARY_CHARS
        ):
            raise TreeError("bad_summary")
        if source.media == "pdf":
            start, end = node.get("start_index"), node.get("end_index")
            if not (_is_int(start) and _is_int(end)):
                raise TreeError("bad_page")
            assert isinstance(start, int) and isinstance(end, int) and isinstance(pages, int)
            if not 1 <= start <= end <= pages:
                raise TreeError("page_out_of_range")
        else:
            line = node.get("line_num")
            if not isinstance(line, int) or isinstance(line, bool) or line < 1:
                raise TreeError("bad_line")
    return tree


def tree_file_name(source_ref: str) -> str:
    return hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:32] + ".json"


def _index_dir(index_dir: Path, *, create: bool) -> Path:
    path = Path(index_dir)
    if not path.is_absolute():
        raise TreeError("index_dir_not_absolute")
    if create and not os.path.lexists(path):
        path.mkdir(mode=0o700, parents=True)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise TreeError("no_index") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TreeError("bad_index_dir")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise TreeError("index_dir_not_owned")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)  # an existing directory open to others is tightened
    return path


def write_tree(index_dir: Path, tree: dict[str, Any]) -> Path:
    """Store (or replace) a tree atomically, mode ``0600``. Returns its path."""

    validate_tree(tree)
    directory = _index_dir(index_dir, create=True)
    payload = json.dumps(tree, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
    if len(payload) > MAX_TREE_BYTES:
        raise TreeError("too_large")
    target = directory / tree_file_name(tree["source"])
    if os.path.islink(target):
        raise TreeError("symlink_refused")
    fd, tmp = tempfile.mkstemp(prefix=".tree-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


@dataclass(frozen=True)
class Index:
    """The trees of one index directory, in file-name order, plus skip counts."""

    trees: tuple[dict[str, Any], ...]
    skipped: Mapping[str, int]


def load_index(index_dir: Path) -> Index:
    """Every valid tree in ``index_dir``. A bad file is skipped and counted by code."""

    directory = _index_dir(index_dir, create=False)
    trees: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            if path.is_symlink() or not path.is_file():
                raise TreeError("not_a_file")
            if path.stat().st_size > MAX_TREE_BYTES:
                raise TreeError("too_large")
            tree = validate_tree(json.loads(path.read_text(encoding="utf-8")))
            if path.name != tree_file_name(tree["source"]):
                raise TreeError("misnamed")
            trees.append(tree)
        except TreeError as exc:
            skipped[exc.code] = skipped.get(exc.code, 0) + 1
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            # RecursionError: JSON nested too deeply for the parser; skip that tree
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
    return Index(tuple(trees), skipped)


__all__ = [
    "MAX_NODES",
    "TREE_SCHEMA",
    "Index",
    "Source",
    "TreeError",
    "build_markdown_tree",
    "encode_part",
    "import_pageindex_tree",
    "iter_nodes",
    "load_index",
    "markdown_structure",
    "node_ref",
    "parse_source",
    "tree_file_name",
    "validate_tree",
    "write_tree",
]
