"""The on-disk bm25s projection: where it lives, when it is stale, how it is built.

The index is a disposable projection **outside the vault**, in memfw's local
data directory and keyed by the vault the same way the usage store is
(``memfw.usage``): ``~/.local/share/memfw/adapters/bm25s/<key>/``, where
``<key>`` is the first 24 hex digits of the sha256 of the vault's resolved
path. Directories are 0700, every file 0600; a symlinked
directory is refused. Deleting it loses nothing: the next search rebuilds it.

What it holds, per build ``idx-<fingerprint>/``:

* bm25s's own score matrix and vocabulary (``*.index.npy``,
  ``vocab.index.json``, ``params.index.json``). The vocabulary keys are
  **hashed tokens** (``blake2b``, 8 bytes), so no word of any memory is
  stored in clear; the corpus itself is never saved;
* ``docs.json``: the indexed memory ids and their tenants, in matrix order;
* ``manifest.json``, written last: format, fingerprint, versions, counts.

Only memories whose privacy level the configured clearance admits are
indexed (default ``public,shared``); ``unknown`` never is. The level is read
with the L0 rules (``memfw.ports.privacy.effective_level``) from the
fail-closed reference reader; a file that reader cannot parse is left out.

The **fingerprint** covers the index format, the tokenizer, the bm25s
version and parameters, the clearance, ``vault.yaml`` and every memory file
(id, size, mtime, ctime, inode): any write to the vault changes it, and a
search that sees a new fingerprint rebuilds (or loads the build another
process already wrote).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from ._ports import (
    LEGACY_PRIVACY_FALLBACK,
    LEGACY_PRIVACY_KEY,
    MemoryFile,
    admits,
    effective_level,
    iter_memories,
    legacy_default,
    memory_ids,
)

#: Bump when the files or their meaning change: older builds are then ignored.
INDEX_FORMAT: Final = 1
#: The tokenizer the stored hashes came from (see ``tokenize``).
TOKENIZER: Final = "jieba-lcut-nfkc-casefold-v1"
#: Under the home directory: memfw's local data directory (as the usage store).
DATA_DIR: Final = Path(".local") / "share" / "memfw" / "adapters" / "bm25s"
MANIFEST: Final = "manifest.json"
DOCS: Final = "docs.json"

_WORD = re.compile(r"\w", re.UNICODE)
_LEGACY_LINE = re.compile(rf"^{LEGACY_PRIVACY_KEY}:[ \t]*(.*?)[ \t]*$")


class StoreError(OSError):
    """The data directory is not a private directory this adapter can use."""


# --- tokens -------------------------------------------------------------------

_segment: Callable[[str], list[str]] | None = None


def _segmenter() -> Callable[[str], list[str]]:
    """jieba's precise mode, as memfw's own unicode61 FTS segments text."""

    global _segment
    if _segment is None:
        import jieba  # type: ignore[import-untyped]

        _segment = lambda text: jieba.lcut(text, cut_all=False)  # noqa: E731
    return _segment


def tokenize(text: str) -> list[str]:
    """NFKC + casefold, jieba-segmented; only segments with a word character are kept."""

    folded = unicodedata.normalize("NFKC", text).casefold()
    return [token for token in _segmenter()(folded) if _WORD.search(token)]


def hashed(tokens: Iterable[str]) -> list[str]:
    """The stored form of a token: 16 hex chars of blake2b, never the word itself."""

    return [hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest() for token in tokens]


# --- vault reading ----------------------------------------------------------


def vault_key(root: Path | str) -> str:
    """The first 24 hex digits of sha256 of the vault's resolved path (``memfw.usage``)."""

    resolved = str(Path(root).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def legacy_privacy(vault: Path) -> str:
    """The vault's ``legacy_privacy_default`` from a line scan of ``vault.yaml``.

    Fails closed: an unreadable file, a value this scan does not understand or
    several lines (the strictest would be a guess) give ``local-private``.
    """

    try:
        text = (vault / "vault.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return LEGACY_PRIVACY_FALLBACK
    found = [match.group(1) for line in text.splitlines() if (match := _LEGACY_LINE.match(line))]
    if len(found) != 1:
        return LEGACY_PRIVACY_FALLBACK
    value = found[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return legacy_default(value)


def privacy_level(memory: MemoryFile, legacy: str) -> str:
    """The level an exit acts on (L0 rules); a present empty ``privacy`` is ``unknown``."""

    raw = memory.front.get("privacy")
    privacy = raw if isinstance(raw, str) else (None if raw is None else "")
    return effective_level(
        privacy,
        memory.tags(),
        present="privacy" in memory.front,
        legacy=legacy,
        sensitivity=memory.front.get("sensitivity"),
    )


def fingerprint(vault: Path, *, settings: Sequence[str]) -> str:
    """Changes whenever a memory file, ``vault.yaml`` or a setting that shapes the index does."""

    digest = hashlib.sha256()
    digest.update(json.dumps([INDEX_FORMAT, TOKENIZER, *settings]).encode("utf-8"))

    def add(name: str, path: Path) -> None:
        try:
            info = os.lstat(path)
        except OSError:
            digest.update(f"{name}\0missing\n".encode())
            return
        digest.update(
            f"{name}\0{info.st_size}\0{info.st_mtime_ns}\0{info.st_ctime_ns}\0{info.st_ino}\n".encode()
        )

    add("vault.yaml", vault / "vault.yaml")
    for memory_id in memory_ids(vault):
        add(memory_id, vault / "memories" / f"{memory_id}.md")
    return digest.hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Corpus:
    """What one build indexes: ids, tenants and hashed tokens, plus what it left out."""

    ids: tuple[str, ...]
    tenants: tuple[str, ...]
    tokens: tuple[list[str], ...]
    skipped: dict[str, int] = field(default_factory=dict)


def read_corpus(vault: Path, clearance: frozenset[str]) -> Corpus:
    """Every readable memory the clearance admits (title + body), in id order."""

    legacy = legacy_privacy(vault)
    ids: list[str] = []
    tenants: list[str] = []
    tokens: list[list[str]] = []
    skipped = {"unreadable": 0, "privacy": 0}
    for memory_id, memory in iter_memories(vault):
        if memory is None:
            skipped["unreadable"] += 1
            continue
        if not admits(clearance, privacy_level(memory, legacy)):
            skipped["privacy"] += 1
            continue
        ids.append(memory_id)
        tenants.append(memory.text("tenant"))
        tokens.append(hashed(tokenize(f"{memory.text('title')}\n{memory.body}")))
    return Corpus(tuple(ids), tuple(tenants), tuple(tokens), skipped)


# --- the data directory -----------------------------------------------------


def default_data_dir(home: Path | None = None) -> Path:
    return (home if home is not None else Path.home()) / DATA_DIR


def private_dir(path: Path) -> None:
    """Create ``path`` (and parents) 0700; refuse a symlink; tighten one we own to 0700."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise StoreError("bm25s data directory is not a directory")
    info = os.lstat(path)
    getuid = getattr(os, "getuid", None)
    if stat.S_IMODE(info.st_mode) != 0o700 and (getuid is None or info.st_uid == getuid()):
        os.chmod(path, 0o700)


def _write_private(path: Path, payload: Any) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(slots=True)
class Built:
    """One loaded build: the bm25s model (None for an empty corpus) and its documents."""

    fingerprint: str
    model: Any
    ids: tuple[str, ...]
    tenants: tuple[str, ...]
    skipped: dict[str, int]
    masks: dict[str, Any] = field(default_factory=dict)

    def mask(self, tenant: str) -> Any:
        """1.0 for this tenant's documents, 0.0 for the others (exact match), cached."""

        cached = self.masks.get(tenant)
        if cached is None:
            import numpy as np

            cached = np.fromiter((owner == tenant for owner in self.tenants), dtype="float32")
            self.masks[tenant] = cached
        return cached


def _model_params(k1: float, b: float) -> dict[str, Any]:
    return {"k1": k1, "b": b, "method": "lucene"}


def build(corpus: Corpus, *, k1: float, b: float, fingerprint_value: str) -> Built:
    """Index ``corpus`` in memory with bm25s (numpy backend, no progress bars)."""

    model = None
    if corpus.ids:
        import bm25s  # type: ignore[import-untyped,unused-ignore]

        model = bm25s.BM25(**_model_params(k1, b), backend="numpy")
        model.index(list(corpus.tokens), show_progress=False)
    return Built(fingerprint_value, model, corpus.ids, corpus.tenants, dict(corpus.skipped))


def save(built: Built, directory: Path, *, bm25s_version: str) -> Path:
    """Write ``built`` as ``directory/idx-<fingerprint>`` (atomic rename); return its path.

    When another process already wrote the same build, its copy is kept. Older
    builds of this vault are removed (best effort).
    """

    private_dir(directory)
    final = directory / f"idx-{built.fingerprint}"
    if (final / MANIFEST).is_file():
        return final
    temporary = Path(tempfile.mkdtemp(prefix=".tmp-", dir=directory))
    try:
        os.chmod(temporary, 0o700)
        if built.model is not None:
            built.model.save(str(temporary), corpus=None, show_progress=False)
            for written in temporary.iterdir():
                os.chmod(written, 0o600)
        _write_private(temporary / DOCS, {"ids": list(built.ids), "tenants": list(built.tenants)})
        _write_private(
            temporary / MANIFEST,
            {
                "format": INDEX_FORMAT,
                "fingerprint": built.fingerprint,
                "tokenizer": TOKENIZER,
                "bm25s": bm25s_version,
                "documents": len(built.ids),
                "skipped": built.skipped,
            },
        )
        try:
            os.rename(temporary, final)
        except OSError:
            if not (final / MANIFEST).is_file():
                raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    for stale in directory.glob("idx-*"):
        if stale != final and not stale.is_symlink() and stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    return final


def load(directory: Path, fingerprint_value: str) -> Built | None:
    """The saved build for ``fingerprint_value``, or None (missing, other format, unreadable)."""

    final = directory / f"idx-{fingerprint_value}"
    if directory.is_symlink() or final.is_symlink() or not (final / MANIFEST).is_file():
        return None
    try:
        manifest = json.loads((final / MANIFEST).read_text(encoding="utf-8"))
        docs = json.loads((final / DOCS).read_text(encoding="utf-8"))
        if manifest.get("format") != INDEX_FORMAT or manifest.get("tokenizer") != TOKENIZER:
            return None
        if manifest.get("fingerprint") != fingerprint_value:
            return None
        ids = tuple(str(item) for item in docs["ids"])
        tenants = tuple(str(item) for item in docs["tenants"])
        if len(ids) != len(tenants) or len(ids) != manifest.get("documents"):
            return None
        model = None
        if ids:
            import bm25s  # type: ignore[import-untyped,unused-ignore]

            model = bm25s.BM25.load(str(final), mmap=False, show_progress=False)
            if int(model.scores["num_docs"]) != len(ids):
                return None
        skipped = manifest.get("skipped")
        return Built(
            fingerprint_value,
            model,
            ids,
            tenants,
            dict(skipped) if isinstance(skipped, dict) else {},
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


__all__ = [
    "DATA_DIR",
    "INDEX_FORMAT",
    "TOKENIZER",
    "Built",
    "Corpus",
    "StoreError",
    "build",
    "default_data_dir",
    "fingerprint",
    "hashed",
    "legacy_privacy",
    "load",
    "private_dir",
    "privacy_level",
    "read_corpus",
    "save",
    "tokenize",
    "vault_key",
]
