"""``Bm25sProvider``: BM25 recall over a vault's memories with bm25s, in process.

A ``RecallProvider`` (``memfw.ports``) next to the built-in FTS: lexical,
``memory_id`` refs, scores and ``why: lexical`` only, never text. It reads the
vault's memory files with the fail-closed reference reader and keeps its
index outside the vault (``store``), so a search never writes the vault.

Filtering follows the built-in ``FtsProvider``: the tenant is the one in the
Markdown, matched exactly (the empty tenant matches only itself), and applied
before the page is cut; a file that cannot be read is left out; ``as_of``,
lifecycle and validity are the coordinator's policy, as for FTS (whose
``compat_fts`` policy this provider passes too). Privacy is stricter than
FTS: only memories the ``clearance`` setting admits are indexed at all
(default ``public,shared``), because the index lives outside the vault.

Settings (``vault.yaml`` ``runtime.recall.adapters[].options``; the runtime
adds ``vault``):

``vault``
    The vault root (required).
``data_dir``
    Where the index lives (default ``~/.local/share/memfw/adapters/bm25s``);
    a directory at or under the vault root is refused.
``clearance``
    Comma separated privacy levels to index (default ``public,shared``).
``name``, ``k1``, ``b``
    Provider name (default ``bm25s``) and the BM25 parameters (1.5, 0.75).

Unknown settings are refused, so a typo never silently runs with defaults.
"""

from __future__ import annotations

import importlib.util
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from . import store
from ._ports import (
    AdapterSpec,
    Availability,
    Candidate,
    RecallBudget,
    check_name,
    is_vault,
    parse_clearance,
)

__version__ = "0.1.0"
#: The bm25s release this adapter is pinned to and tested with.
BM25S_VERSION: Final = "0.3.16"
NAME: Final = "bm25s"
#: Longest query accepted, in characters (as ``search_vault``).
MAX_QUERY_CHARS: Final = 512
DEFAULT_CLEARANCE: Final = "public,shared"
#: Settings the runtime fills in itself; ``embedder`` is accepted and ignored.
INJECTED: Final = frozenset({"vault", "embedder"})
KNOWN: Final = frozenset({"vault", "embedder", "data_dir", "clearance", "name", "k1", "b"})
_LEVELS: Final = frozenset({"public", "shared", "local-private"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Checked settings (see the module docstring)."""

    vault: Path
    data_dir: Path | None = None
    clearance: frozenset[str] = frozenset({"public", "shared"})
    name: str = NAME
    k1: float = 1.5
    b: float = 0.75

    @classmethod
    def from_mapping(cls, settings: Mapping[str, Any]) -> Settings:
        unknown = sorted(set(settings) - KNOWN)
        if unknown:
            raise ValueError("unknown bm25s settings: " + ", ".join(unknown))
        vault = settings.get("vault")
        if not isinstance(vault, (str, Path)) or not str(vault).strip():
            raise ValueError("bm25s needs a vault path")
        data_dir = settings.get("data_dir")
        if data_dir is not None and (not isinstance(data_dir, (str, Path)) or not str(data_dir)):
            raise ValueError("data_dir must be a path")
        raw = settings.get("clearance", DEFAULT_CLEARANCE)
        if not isinstance(raw, str):
            raise ValueError("clearance must be a comma separated string")
        tokens = {token.strip().lower() for token in raw.split(",") if token.strip()}
        if not tokens or not tokens <= _LEVELS:
            raise ValueError("clearance must name public, shared or local-private")
        clearance = parse_clearance(raw)
        if clearance is None:  # unreachable: tokens is not empty
            raise ValueError("clearance must name at least one level")
        name = settings.get("name", NAME)
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        k1, b = settings.get("k1", 1.5), settings.get("b", 0.75)
        for label, value in (("k1", k1), ("b", b)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{label} must be a number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be finite and not negative")
        if b > 1:
            raise ValueError("b must be at most 1")
        return cls(
            vault=Path(vault).expanduser(),
            data_dir=Path(data_dir).expanduser() if data_dir is not None else None,
            clearance=clearance,
            name=check_name(name, what="provider"),
            k1=float(k1),
            b=float(b),
        )


def _missing_dependency() -> str | None:
    for module in ("bm25s", "numpy", "jieba"):
        if importlib.util.find_spec(module) is None:
            return module
    return None


class Bm25sProvider:
    """Lexical recall with bm25s over the memories the clearance admits."""

    lexical = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.name = settings.name
        self.root = settings.vault
        self.projection_version = ""
        self._lock = threading.Lock()
        self._built: store.Built | None = None
        index = self.index_dir().expanduser().resolve()
        vault = self.root.resolve()
        if index == vault or vault in index.parents:
            raise ValueError("data_dir must be outside the vault")

    # --- the index ------------------------------------------------------------

    def index_dir(self) -> Path:
        base = self.settings.data_dir or store.default_data_dir()
        return base / store.vault_key(self.root)

    def _fingerprint(self) -> str:
        settings = self.settings
        return store.fingerprint(
            self.root,
            settings=[
                BM25S_VERSION,
                repr(settings.k1),
                repr(settings.b),
                ",".join(sorted(settings.clearance)),
            ],
        )

    def refresh(self) -> store.Built:
        """The current build: the loaded one, the saved one, or a new one (saved).

        A vault that changed while it was read is indexed for this call but not
        saved; the next call reads it again.
        """

        with self._lock:
            before = self._fingerprint()
            built = self._built
            if built is not None and built.fingerprint == before:
                return built
            directory = self.index_dir()
            built = store.load(directory, before)
            if built is None:
                corpus = store.read_corpus(self.root, self.settings.clearance)
                built = store.build(
                    corpus, k1=self.settings.k1, b=self.settings.b, fingerprint_value=before
                )
                if self._fingerprint() == before:
                    store.save(built, directory, bm25s_version=BM25S_VERSION)
                else:
                    built.fingerprint = ""
            self._built = built
            self.projection_version = f"bm25s-{built.fingerprint[:16]}" if built.fingerprint else ""
            return built

    def status(self) -> dict[str, Any]:
        """Counts only (documents, skipped), never ids or text."""

        built = self.refresh()
        return {
            "provider": self.name,
            "documents": len(built.ids),
            "skipped": dict(built.skipped),
            "fingerprint": built.fingerprint,
            "index_dir": str(self.index_dir()),
        }

    # --- RecallProvider -------------------------------------------------------

    def available(self) -> Availability:
        if not is_vault(self.root):
            return Availability.missing("no_vault")
        missing = _missing_dependency()
        if missing is not None:
            return Availability.missing("missing_dependency")
        return Availability.ok()

    def search(
        self,
        query: str,
        *,
        limit: int,
        budget: RecallBudget,
        tenant: str,
        as_of: datetime | None,
    ) -> Sequence[Candidate]:
        """At most ``limit`` (and ``budget.max_candidates``) ids of ``tenant``, best first.

        ``as_of`` is not applied here; the coordinator's policy enforces it.
        Ties break on the memory id, so a repeated call gives the same order.
        """

        del as_of
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be a string of at most 512 characters")
        cap = min(int(limit), budget.max_candidates)
        if cap < 1 or not query.strip():
            return []
        built = self.refresh()
        if built.model is None:
            return []
        token_ids = built.model.get_tokens_ids(store.hashed(store.tokenize(query)))
        if not token_ids:
            return []
        scores = built.model.get_scores_from_ids(
            list(dict.fromkeys(token_ids)), weight_mask=built.mask(tenant)
        )
        ranked = sorted(
            (
                (float(scores[index]), built.ids[index])
                for index in scores.nonzero()[0]
                if scores[index] > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        return [
            Candidate(
                ref=memory_id,
                ref_kind="memory_id",
                score=round(score, 6),
                provider=self.name,
                rank=rank,
                extra={"why": "lexical"},
            )
            for rank, (score, memory_id) in enumerate(ranked[:cap], start=1)
        ]


def factory(settings: Mapping[str, Any] | None = None) -> Bm25sProvider:
    """``module:factory`` entry: ``memfw_adapter_bm25s:factory``."""

    return Bm25sProvider(Settings.from_mapping(settings or {}))


SPEC = AdapterSpec(
    name=NAME,
    kind="recall",
    factory=factory,
    requires=("bm25s", "numpy", "jieba"),
    version=BM25S_VERSION,
    source="https://github.com/xhluca/bm25s",
    license="MIT",
)


__all__ = [
    "BM25S_VERSION",
    "DEFAULT_CLEARANCE",
    "MAX_QUERY_CHARS",
    "NAME",
    "SPEC",
    "Bm25sProvider",
    "Settings",
    "__version__",
    "factory",
]
