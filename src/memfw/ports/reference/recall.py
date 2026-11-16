"""``InMemoryRecallProvider``: BM25-lite over documents held in memory. Stdlib only.

A second ``RecallProvider`` next to the built-in FTS, hybrid and multihop
providers. It needs no index, no SQLite and no memfw internals: give it
``{memory_id: text}`` (and, optionally, each id's tenant) or load it from a
vault's memory files with ``from_vault``. Useful as the recall backend of a
small adapter, as a test double that behaves like a real ranker, and as the
swap drill for the coordinator (``tests/test_swap_drills.py``).

Like every recall provider it returns refs and scores, never text.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..types import (
    MEMORY_ID_RE,
    Availability,
    Candidate,
    RecallBudget,
    check_name,
)
from ._vault import is_vault, iter_memories

#: Longest query accepted, in characters (a longer one raises ``ValueError``).
MAX_QUERY_CHARS = 512

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_K1 = 1.2
_B = 0.75


def _is_cjk(char: str) -> bool:
    return unicodedata.east_asian_width(char) in {"W", "F"} and char.isalpha()


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens; each CJK character is its own token."""

    tokens: list[str] = []
    for word in _WORD_RE.findall(unicodedata.normalize("NFKC", text).casefold()):
        run = ""
        for char in word:
            if _is_cjk(char):
                if run:
                    tokens.append(run)
                    run = ""
                tokens.append(char)
            else:
                run += char
        if run:
            tokens.append(run)
    return tokens


@dataclass(frozen=True, slots=True)
class _Doc:
    memory_id: str
    tenant: str
    terms: Counter[str]
    length: int


class InMemoryRecallProvider:
    """Ranks documents by BM25 (k1=1.2, b=0.75); ties break on memory id.

    ``documents`` maps a memory id to the text to index (title and body).
    ``tenants`` maps an id to its tenant (missing ids are in the empty
    tenant); ``search`` returns only ids of the caller's tenant (exact
    match). The coordinator filters again either way.
    """

    lexical = True

    def __init__(
        self,
        documents: Mapping[str, str],
        *,
        tenants: Mapping[str, str] | None = None,
        name: str = "inmemory",
    ) -> None:
        self.name = check_name(name, what="provider")
        self.projection_version = ""
        owners = dict(tenants or {})
        docs: list[_Doc] = []
        for memory_id in sorted(documents):
            if not isinstance(memory_id, str) or not MEMORY_ID_RE.fullmatch(memory_id):
                raise ValueError("document keys must be memory ids")
            text = documents[memory_id]
            if not isinstance(text, str):
                raise ValueError("document text must be a string")
            tokens = tokenize(text)
            docs.append(
                _Doc(memory_id, str(owners.get(memory_id, "")), Counter(tokens), len(tokens))
            )
        self._docs = tuple(docs)
        self._average = (sum(doc.length for doc in docs) / len(docs)) if docs else 0.0
        frequency: Counter[str] = Counter()
        for doc in docs:
            frequency.update(doc.terms.keys())
        total = len(docs)
        self._idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5))
            for term, count in frequency.items()
        }

    @classmethod
    def from_vault(cls, vault: Path | str, *, name: str = "inmemory") -> InMemoryRecallProvider:
        """Index every readable memory file (title + body) with its tenant.

        A snapshot: memories added later are not seen until a new provider is
        built. Unreadable files are left out.
        """

        root = Path(vault).expanduser()
        if not is_vault(root):
            raise ValueError("not a vault: vault.yaml is missing")
        documents: dict[str, str] = {}
        tenants: dict[str, str] = {}
        for memory_id, memory in iter_memories(root):
            if memory is None:
                continue
            documents[memory_id] = f"{memory.text('title')}\n{memory.body}"
            tenants[memory_id] = memory.text("tenant")
        return cls(documents, tenants=tenants, name=name)

    def __len__(self) -> int:
        return len(self._docs)

    def available(self) -> Availability:
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
        """At most ``limit`` (and ``budget.max_candidates``) ids, best first.

        ``as_of`` is not applied: documents carry no validity window here; the
        coordinator's policy enforces it.
        """

        del as_of
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise ValueError("query must be a string of at most 512 characters")
        if limit < 1:
            return []
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            return []
        scored: list[tuple[float, str]] = []
        for doc in self._docs:
            if doc.tenant != tenant:
                continue
            norm = 1 - _B + _B * (doc.length / self._average if self._average else 0.0)
            score = 0.0
            for term in terms:
                count = doc.terms.get(term, 0)
                if count:
                    score += self._idf[term] * count * (_K1 + 1) / (count + _K1 * norm)
            if score > 0:
                scored.append((score, doc.memory_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        top = scored[: min(limit, budget.max_candidates)]
        return [
            Candidate(
                ref=memory_id,
                ref_kind="memory_id",
                score=round(score, 6),
                provider=self.name,
                rank=rank,
                extra={"why": "bm25-lite"},
            )
            for rank, (score, memory_id) in enumerate(top, start=1)
        ]


__all__ = ["MAX_QUERY_CHARS", "InMemoryRecallProvider", "tokenize"]
