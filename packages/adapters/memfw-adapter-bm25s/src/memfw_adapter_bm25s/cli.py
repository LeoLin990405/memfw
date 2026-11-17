"""``memfw-adapter-bm25s``: build the index ahead of the first search, or report on it.

    memfw-adapter-bm25s build  --vault PATH [--data-dir DIR] [--clearance LEVELS]
    memfw-adapter-bm25s status --vault PATH [--data-dir DIR] [--clearance LEVELS]

Both print one JSON object of counts (documents indexed, skipped by reason),
the fingerprint and the index directory; never an id or any text. ``build``
is what the first search does on its own; running it ahead keeps that search
inside its time budget on a large vault. Standard library only besides the
adapter's own dependencies.
"""

from __future__ import annotations

import argparse
import json
import sys

from .provider import DEFAULT_CLEARANCE, factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memfw-adapter-bm25s", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("build", "status"))
    parser.add_argument("--vault", required=True)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--clearance", default=DEFAULT_CLEARANCE)
    args = parser.parse_args(argv)
    settings: dict[str, object] = {"vault": args.vault, "clearance": args.clearance}
    if args.data_dir:
        settings["data_dir"] = args.data_dir
    try:
        provider = factory(settings)
    except ValueError as exc:
        print(f"memfw-adapter-bm25s: {exc}", file=sys.stderr)
        return 2
    availability = provider.available()
    if not availability.available:
        print(f"memfw-adapter-bm25s: unavailable: {availability.reason}", file=sys.stderr)
        return 1
    print(json.dumps(provider.status(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
