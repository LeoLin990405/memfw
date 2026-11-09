"""Alias-query class for the R0 harness.

This class is intentionally absent from ``CASE_TYPES``. The R2 mix, and the
committed ``r2_report.json``, stay on the six classes they were measured with.
Run this module to score alias recall against the FTS baseline.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from eval.retrieval_r0.metrics import evidence_recall
from memfw.core.repository import Repository
from memfw.index import MemoryIndex
from memfw.mcp.server import Gateway
from memfw.ontology.model import Concept, Label, merge_concepts, save_concept
from scripts.memfw_readonly_service import search_vault

PRIMARY_K = 10
_ALIAS_CALLS = 20
_MODEL_ENV = (
    "MEMFW_EMBEDDING_URL",
    "MEMFW_RERANK_URL",
    "MEMFW_JUDGE_URL",
    "MEMFW_REWRITE_URL",
    "MEMFW_TENANT",
    "MEMFW_ALLOWED_SOURCES",
    "MEMFW_ENTITY_ENABLED",
)


def measure() -> dict[str, Any]:
    """Score synthetic alias recall, ambiguity, and ``merged_into`` redirect."""

    saved = {key: os.environ.pop(key) for key in _MODEL_ENV if key in os.environ}
    try:
        recall = _recall()
        ambiguity = _ambiguity()
        redirect = _redirect()
    finally:
        os.environ.update(saved)
    gain = recall["alias_recall"] - recall["fts_recall"]
    return {
        "class": "alias_query",
        "in_case_types": False,
        "synthetic": True,
        "primary_k": PRIMARY_K,
        "alias_count": 5,
        "fts_recall": recall["fts_recall"],
        "alias_recall": recall["alias_recall"],
        "mcp_fts_recall": recall["mcp_fts_recall"],
        "mcp_hybrid_recall": recall["mcp_hybrid_recall"],
        "recall_gain": round(gain, 6),
        "distractor_excluded": recall["distractor_excluded"],
        "ambiguity_rate": ambiguity,
        "ambiguity_calls": _ALIAS_CALLS,
        "merged_redirect": redirect,
    }


def _recall() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="memfw-alias-") as directory:
        root = _recall_vault(Path(directory))
        evidence = ("mem-about", "mem-about-2", "mem-applies")
        queries = ("hammer", "mallet", "claw-hammer", "記憶", "pounder", "记忆", "記憶@vault")
        fts_scores: list[float] = []
        alias_scores: list[float] = []
        mcp_fts_scores: list[float] = []
        mcp_hybrid_scores: list[float] = []
        distractor_excluded = True
        index = MemoryIndex(root / "index.sqlite3")
        gateway = Gateway(root, tenant="alpha", entity_enabled=False, allowed_sources=())
        for query in queries:
            lexical = [
                item[0]
                for item in index.ranked_search(query, limit=PRIMARY_K, ranking="bm25")
            ]
            http = search_vault(root, query, limit=PRIMARY_K, tenant="alpha")
            mcp_fts = gateway.search(query, limit=PRIMARY_K, mode="fts")
            mcp_hybrid = gateway.search(query, limit=PRIMARY_K, mode="hybrid")
            fts_scores.append(evidence_recall(lexical, evidence, (), k=PRIMARY_K))
            alias_scores.append(
                evidence_recall([item["id"] for item in http["results"]], evidence, (), k=PRIMARY_K)
            )
            mcp_fts_scores.append(
                evidence_recall(
                    [item["id"] for item in mcp_fts["results"]], evidence, (), k=PRIMARY_K
                )
            )
            mcp_hybrid_scores.append(
                evidence_recall(
                    [item["id"] for item in mcp_hybrid["results"]], evidence, (), k=PRIMARY_K
                )
            )
            for payload in (http, mcp_fts, mcp_hybrid):
                ids = {item["id"] for item in payload["results"]}
                if "mem-other" in ids or "mem-beta" in ids:
                    distractor_excluded = False
                for item in payload["results"]:
                    if not str(item["why"]).startswith("alias("):
                        distractor_excluded = False
                    if "concept:tool:hammer" not in str(item["why"]):
                        distractor_excluded = False
        return {
            "fts_recall": _mean(fts_scores),
            "alias_recall": _mean(alias_scores),
            "mcp_fts_recall": _mean(mcp_fts_scores),
            "mcp_hybrid_recall": _mean(mcp_hybrid_scores),
            "distractor_excluded": distractor_excluded,
        }


def _ambiguity() -> float:
    held = 0
    with tempfile.TemporaryDirectory(prefix="memfw-alias-amb-") as directory:
        root = _ambiguity_vault(Path(directory))
        gateway = Gateway(root, tenant="alpha", entity_enabled=False, allowed_sources=())
        for _ in range(_ALIAS_CALLS):
            http = search_vault(root, "shared-name", limit=PRIMARY_K, tenant="alpha")
            mcp_fts = gateway.search("shared-name", limit=PRIMARY_K, mode="fts")
            mcp_hybrid = gateway.search("shared-name", limit=PRIMARY_K, mode="hybrid")
            if _ambiguous(http) and _ambiguous(mcp_fts) and _ambiguous(mcp_hybrid):
                held += 1
    return held / _ALIAS_CALLS


def _redirect() -> bool:
    with tempfile.TemporaryDirectory(prefix="memfw-alias-merge-") as directory:
        root = _redirect_vault(Path(directory))
        gateway = Gateway(root, tenant="alpha", entity_enabled=False, allowed_sources=())
        for query in ("New Item", "Old Item", "legacy-name"):
            http = search_vault(root, query, limit=PRIMARY_K, tenant="alpha")
            mcp = gateway.search(query, limit=PRIMARY_K, mode="hybrid")
            for payload in (http, mcp):
                ids = {item["id"] for item in payload["results"]}
                if ids != {"mem-old", "mem-new"}:
                    return False
                for item in payload["results"]:
                    if item["why"] != f"alias({query}→concept:item:new)":
                        return False
    return True


def _recall_vault(root: Path) -> Path:
    repository = Repository(root)
    repository.init()
    _add(repository, "mem-about", "Crate one", "crate number one", about="concept:tool:hammer")
    _add(repository, "mem-about-2", "Crate two", "crate number two", about="concept:tool:hammer")
    _add(
        repository,
        "mem-applies",
        "Crate three",
        "crate number three",
        applies_to=["concept:tool:hammer"],
    )
    _add(repository, "mem-other", "Other crate", "unrelated crate", about="concept:tool:other")
    _add(
        repository,
        "mem-beta",
        "Beta crate",
        "beta crate",
        about="concept:tool:hammer",
        tenant="beta",
    )
    MemoryIndex(repository.index_path).rebuild(repository)
    save_concept(
        root,
        Concept(
            oid="concept:tool:hammer",
            class_name="Concept",
            pref_label="hammer",
            alt_labels=(Label("mallet"), Label("claw-hammer"), Label("記憶")),
            same_as=("pounder",),
            status="active",
        ),
        apply=True,
    )
    save_concept(
        root,
        Concept(
            oid="concept:tool:other",
            class_name="Concept",
            pref_label="wedge",
            status="active",
        ),
        apply=True,
    )
    return root


def _ambiguity_vault(root: Path) -> Path:
    repository = Repository(root)
    repository.init()
    _add(repository, "mem-left", "Left note", "left note body", about="concept:widget:left")
    _add(repository, "mem-right", "Right note", "right note body", about="concept:widget:right")
    MemoryIndex(repository.index_path).rebuild(repository)
    pairs = (
        ("concept:widget:left", "Widget Left"),
        ("concept:widget:right", "Widget Right"),
    )
    for oid, label in pairs:
        save_concept(
            root,
            Concept(
                oid=oid,
                class_name="Concept",
                pref_label=label,
                alt_labels=(Label("shared-name"),),
                status="active",
            ),
            apply=True,
        )
    save_concept(
        root,
        Concept(
            oid="concept:widget:draft",
            class_name="Concept",
            pref_label="shared-name",
            status="candidate",
        ),
        apply=True,
    )
    return root


def _redirect_vault(root: Path) -> Path:
    repository = Repository(root)
    repository.init()
    _add(repository, "mem-old", "Old note", "old note body", about="concept:item:old")
    _add(repository, "mem-new", "New note", "new note body", about="concept:item:new")
    MemoryIndex(repository.index_path).rebuild(repository)
    save_concept(
        root,
        Concept(
            oid="concept:item:old",
            class_name="Concept",
            pref_label="Old Item",
            alt_labels=(Label("legacy-name"),),
            status="active",
        ),
        apply=True,
    )
    save_concept(
        root,
        Concept(
            oid="concept:item:new",
            class_name="Concept",
            pref_label="New Item",
            status="active",
        ),
        apply=True,
    )
    merge_concepts(root, "concept:item:old", "concept:item:new", apply=True)
    return root


def _add(
    repository: Repository,
    memory_id: str,
    title: str,
    body: str,
    *,
    about: str | None = None,
    applies_to: list[str] | None = None,
    tenant: str = "alpha",
) -> None:
    repository.add(title, body, memory_id=memory_id, tenant=tenant, source="manual")
    path = repository.root / "memories" / f"{memory_id}.md"
    extra: dict[str, Any] = {}
    if about is not None:
        extra["about"] = about
    if applies_to is not None:
        extra["applies_to"] = applies_to
    if extra:
        _tag(path, extra)


def _tag(path: Path, extra: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict):
        raise RuntimeError("memory frontmatter is not a mapping")
    meta.update(extra)
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n{parts[2]}", encoding="utf-8")


def _ambiguous(payload: dict[str, Any]) -> bool:
    if payload["results"]:
        return False
    rows = payload.get("ambiguity") or []
    if len(rows) != 1:
        return False
    row = rows[0]
    return (
        row.get("kind") == "duplicate_alias"
        and row.get("oids") == ["concept:widget:left", "concept:widget:right"]
    )


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def main() -> int:
    report = measure()
    out = Path(__file__).with_name("alias_report.json")
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"fts_recall={report['fts_recall']} alias_recall={report['alias_recall']} "
        f"gain={report['recall_gain']} ambiguity_rate={report['ambiguity_rate']} "
        f"merged_redirect={report['merged_redirect']}"
    )
    ok = (
        report["fts_recall"] == 0
        and report["alias_recall"] == 1
        and report["mcp_fts_recall"] == 1
        and report["mcp_hybrid_recall"] == 1
        and report["ambiguity_rate"] == 1
        and report["merged_redirect"] is True
        and report["distractor_excluded"] is True
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
