"""``memfw-adapter-qdrant``: write the vault's sendable memories to Qdrant, or check it.

    memfw-adapter-qdrant sync   --vault PATH [--url URL] [--collection NAME]
                                [--clearance public,shared] [--api-key-env NAME]
                                [--allow-remote]
    memfw-adapter-qdrant status --vault PATH [same options]

The embedder is memfw's: ``MEMFW_EMBEDDING_URL`` / ``_MODEL`` / ``_DIMENSION``,
as for the memfw server (see ``memfw_adapter_qdrant.embed``). Both commands
print one JSON object of counts and codes, never an id or any text. ``sync``
sends only memories whose privacy level is in the clearance (never
``local-private``) and deletes the points of memories that are no longer
sendable.
"""

from __future__ import annotations

import argparse
import json
import sys

from .embed import EmbedderError
from .http import QdrantError
from .provider import DEFAULT_CLEARANCE, DEFAULT_COLLECTION, DEFAULT_URL, factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memfw-adapter-qdrant", description=__doc__.split("\n")[0]
    )
    parser.add_argument("command", choices=("sync", "status"))
    parser.add_argument("--vault", required=True)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--clearance", default=DEFAULT_CLEARANCE)
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--allow-remote", action="store_true")
    args = parser.parse_args(argv)
    settings = {
        "vault": args.vault,
        "url": args.url,
        "collection": args.collection,
        "clearance": args.clearance,
        "api_key_env": args.api_key_env,
        "allow_remote": args.allow_remote,
    }
    try:
        provider = factory(settings)
    except ValueError as exc:
        print(f"memfw-adapter-qdrant: {exc}", file=sys.stderr)
        return 2
    if args.command == "status":
        availability = provider.available()
        print(json.dumps({"available": availability.available, "reason": availability.reason}))
        return 0 if availability.available else 1
    if provider.embedder is None:
        print("memfw-adapter-qdrant: no embedder (set MEMFW_EMBEDDING_URL)", file=sys.stderr)
        return 1
    from .sync import sync

    try:
        report = sync(provider)
    except (QdrantError, EmbedderError) as exc:
        print(f"memfw-adapter-qdrant: sync failed: {exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
