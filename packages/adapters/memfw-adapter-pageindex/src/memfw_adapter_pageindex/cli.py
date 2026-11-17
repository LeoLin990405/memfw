"""``memfw-pageindex``: build, import and list document trees (offline, explicit).

    memfw-pageindex build-md --root KH_ROOT --source kh:notes/a.md --index-dir DIR
                             [--privacy LEVEL] [--tenant T]
    memfw-pageindex import-tree --source att:ID|kh:docs/a.pdf --tree PAGEINDEX.json
                                --pages N --sha256 HEX --index-dir DIR
                                [--privacy LEVEL] [--tenant T] [--keep-summaries]
                                [--pageindex-version V]
    memfw-pageindex list --index-dir DIR

Nothing here calls a model or the network. ``import-tree`` reads a tree that
PageIndex produced outside memfw. Each command prints one JSON line per tree
(source, privacy, node count, file name); titles are never printed.
Errors print ``{"error": "<code>"}`` and exit 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .tree import (
    MAX_TREE_BYTES,
    TreeError,
    build_markdown_tree,
    import_pageindex_tree,
    iter_nodes,
    load_index,
    tree_file_name,
    write_tree,
)


def _summary(tree: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": tree["source"],
        "media": tree["media"],
        "privacy": tree["privacy"],
        "tenant": tree["tenant"],
        "nodes": sum(1 for _ in iter_nodes(tree["structure"])),
        "file": tree_file_name(tree["source"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memfw-pageindex", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    md = sub.add_parser("build-md", help="build a tree from a Markdown note's headings")
    md.add_argument("--root", required=True, type=Path, help="Knowledge-Hub root directory")
    md.add_argument("--source", required=True, help="kh:<relative path>.md")
    imp = sub.add_parser("import-tree", help="import PageIndex's tree of a PDF")
    imp.add_argument("--source", required=True, help="att:<id> or kh:<relative path>.pdf")
    imp.add_argument("--tree", required=True, type=Path, help="PageIndex output JSON")
    imp.add_argument("--pages", required=True, type=int, help="the PDF's page count")
    imp.add_argument("--sha256", required=True, help="the PDF's sha256")
    imp.add_argument("--keep-summaries", action="store_true")
    imp.add_argument("--pageindex-version", default="")
    for command in (md, imp):
        command.add_argument("--index-dir", required=True, type=Path)
        command.add_argument("--privacy", default="local-private")
        command.add_argument("--tenant", default="")
    listing = sub.add_parser("list", help="list stored trees (no titles)")
    listing.add_argument("--index-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            index = load_index(args.index_dir.absolute())
            for tree in index.trees:
                print(json.dumps(_summary(tree), ensure_ascii=False))
            if index.skipped:
                print(json.dumps({"skipped": dict(index.skipped)}), file=sys.stderr)
            return 0
        if args.command == "build-md":
            tree = build_markdown_tree(
                args.root, args.source, privacy=args.privacy, tenant=args.tenant
            )
        else:
            if args.tree.stat().st_size > MAX_TREE_BYTES:
                raise TreeError("too_large")
            data = json.loads(args.tree.read_text(encoding="utf-8"))
            tree = import_pageindex_tree(
                data,
                args.source,
                pages=args.pages,
                source_sha256=args.sha256.strip().lower(),
                privacy=args.privacy,
                tenant=args.tenant,
                keep_summaries=args.keep_summaries,
                pageindex_version=args.pageindex_version,
            )
        write_tree(args.index_dir.absolute(), tree)
        print(json.dumps(_summary(tree), ensure_ascii=False))
        return 0
    except TreeError as exc:
        print(json.dumps({"error": exc.code}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"error": "unreadable"}))
        return 1


__all__ = ["main"]
