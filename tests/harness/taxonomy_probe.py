"""Probe for the K1/K2 byte-identical check (tests/test_ontology_taxonomy_relations.py).

Uses only APIs that exist before K1, so it runs against an older tree too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from memfw.core.repository import Repository


def _concept(root: Path, slug: str, oid: str, **extra: Any) -> Path:
    meta: dict[str, Any] = {
        "oid": oid,
        "class": "Concept",
        "pref_label": slug.replace("-", " "),
        "alt_labels": [],
        "same_as": [],
        "status": "active",
        "merged_into": None,
    }
    meta.update(extra)
    path = root / "concepts" / "Concept" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip()
    path.write_text(f"---\n{dumped}\n---\n\n{slug}\n", encoding="utf-8")
    return path


def taxonomy_probe(payload: dict[str, Any], workdir: Path) -> str:
    """Lint, show, list, alias expansion and topics for a fixed vault."""
    from memfw.ontology import lint_concepts as lint
    from memfw.ontology import load_concepts as load
    from memfw.ontology.cli import app
    from memfw.ontology.expand import matching_memories
    from memfw.runtime.context_sources import concept_catalogue

    repository = Repository(workdir / "vault")
    repository.init()
    root = repository.root
    _concept(root, "food", "concept:food", applies_to=["concept:fruit"])
    extra = {"broader": ["concept:food"]} if payload.get("broader") else {}
    _concept(root, "fruit", "concept:fruit", alt_labels=["fruit"], **extra)
    _concept(root, "old", "concept:old", merged_into="concept:fruit", colour="x")
    repository.add("Apple", "an apple", memory_id="mem-fruit", about=["concept:fruit"])
    repository.add("Bread", "bread", memory_id="mem-food", about=["concept:food"])
    out: dict[str, Any] = {}
    review = lint(load(root))
    if payload.get("broader"):
        # The broader-only items are new; everything else must match.
        review = [item for item in review if not str(item["kind"]).startswith("broader_")]
        # Before K1 a broader key was an unknown relation.
        review = [item for item in review if item.get("detail") != "broader"]
    out["review"] = review
    runner = CliRunner()
    if not payload.get("broader"):
        out["list"] = runner.invoke(app, ["list", "--vault", str(root)]).output
        for oid in ("concept:food", "concept:fruit", "concept:old"):
            out[oid] = runner.invoke(app, ["show", oid, "--vault", str(root)]).output
    out["alias"] = [matching_memories(root, term) for term in ("food", "fruit")]
    catalogue = concept_catalogue(root)
    out["topics"] = [catalogue.labels, catalogue.canonical]
    from memfw.index import MemoryIndex
    from memfw.index.readonly import search_vault

    MemoryIndex(repository.index_path).rebuild(repository)

    out["search"] = [search_vault(root, term, limit=5) for term in ("food", "fruit", "apple")]
    from datetime import UTC, datetime

    from memfw.okf.export import export_okf

    bundle = workdir / "okf"
    export_okf(root, bundle, now=datetime(2026, 1, 1, tzinfo=UTC))
    out["okf"] = {
        path.relative_to(bundle).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    }
    text = json.dumps(out, sort_keys=True, default=str).replace(str(workdir), "<w>")
    # Write times differ between runs; mask them and the content hashes over them.
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+(?:\+00:00|Z)", "<t>", text)
    return re.sub(r"sha256:[0-9a-f]{64}", "<h>", text)
