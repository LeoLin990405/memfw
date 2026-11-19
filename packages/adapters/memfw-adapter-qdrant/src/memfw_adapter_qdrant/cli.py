"""``memfw-adapter-qdrant``: write the vault's sendable memories to Qdrant, or check it.

    memfw-adapter-qdrant sync   --vault PATH [--url URL] [--collection NAME]
                                [--clearance public,shared] [--api-key-env NAME]
                                [--allow-remote] [--embedding-timeout SECONDS]
                                [--embedding-batch-size N] [--max-seconds SECONDS]
    memfw-adapter-qdrant status --vault PATH [same options]

The embedder is memfw's: ``MEMFW_EMBEDDING_URL`` / ``_MODEL`` / ``_DIMENSION``,
as for the memfw server (see ``memfw_adapter_qdrant.embed``). Both commands
print one JSON object of counts and codes, never an id or any text. ``sync``
sends only memories whose privacy level is in the clearance (never
``local-private``) and deletes the points of memories that are no longer
sendable.

``--embedding-timeout`` / ``--embedding-batch-size`` override
``MEMFW_EMBEDDING_TIMEOUT`` / ``MEMFW_EMBEDDING_BATCH_SIZE`` (default 30 s and
16; a slow CPU-only host wants ``--embedding-batch-size 1 --embedding-timeout
120`` or more). ``sync`` writes each batch as soon as it is embedded; a batch
that fails is counted (``failed_requests``, ``remaining``) and the exit status
is 1, and the next run continues with what is missing. ``--max-seconds``
stops cleanly between batches once that much time is spent (also exit 1
when anything remains). Rule of thumb: batch size x seconds per memory must
stay below the timeout. Only one ``sync`` per Qdrant URL, collection and
vault runs at a time (a ``flock`` under ``$XDG_STATE_HOME`` or
``~/.local/state/memfw-adapter-qdrant``); a second one exits with status 3.
On a schedule, keep the interval at least ``--max-seconds`` plus one
``--embedding-timeout``.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import IO, Any

from .embed import EmbedderError
from .http import QdrantError
from .provider import DEFAULT_CLEARANCE, DEFAULT_COLLECTION, DEFAULT_URL, factory

#: Exit status when another ``sync`` of the same vault and collection is running.
LOCKED = 3


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
    parser.add_argument("--embedding-timeout", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--embedding-batch-size", type=int, default=None, metavar="N")
    parser.add_argument("--max-seconds", type=float, default=None, metavar="SECONDS")
    args = parser.parse_args(argv)
    settings: dict[str, Any] = {
        "vault": args.vault,
        "url": args.url,
        "collection": args.collection,
        "clearance": args.clearance,
        "api_key_env": args.api_key_env,
        "allow_remote": args.allow_remote,
    }
    if args.embedding_timeout is not None:
        settings["embedding_timeout_seconds"] = args.embedding_timeout
    if args.embedding_batch_size is not None:
        settings["embedding_batch_size"] = args.embedding_batch_size
    from .sync import batch_size_of, check_max_seconds, sync

    try:
        if args.max_seconds is not None:
            check_max_seconds(args.max_seconds)
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
    try:
        lock = acquire_lock(provider)
    except OSError as exc:  # e.g. ENOLCK on NFS, or an unwritable state directory
        reason = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        print(f"memfw-adapter-qdrant: cannot take the sync lock ({reason})", file=sys.stderr)
        return 1
    if lock is None:
        print("memfw-adapter-qdrant: another sync of this vault is running", file=sys.stderr)
        return LOCKED
    try:
        report = sync(provider, max_seconds=args.max_seconds)
    except (QdrantError, EmbedderError) as exc:
        print(f"memfw-adapter-qdrant: sync failed: {exc.code}", file=sys.stderr)
        return 1
    finally:
        lock.close()
    print(json.dumps(report.as_dict(), sort_keys=True))
    if not report.complete:
        print(_incomplete(report, batch_size_of(provider)), file=sys.stderr)
        return 1
    return 0


def _incomplete(report: Any, batch: int) -> str:
    line = (
        f"memfw-adapter-qdrant: sync incomplete: {report.remaining} remaining "
        f"({report.rejected} rejected by the endpoint), {report.failed_requests} failed requests"
        + (f", stopped: {report.stopped}" if report.stopped else "")
    )
    if report.failures.get("embedding_unreachable") and batch > 1:
        return (
            line + "; timeouts: lower --embedding-batch-size (batch x seconds per memory "
            "must stay below --embedding-timeout), then run it again"
        )
    if report.failures.get("embedding_unreachable"):
        return (
            line + "; timeouts: raise --embedding-timeout above the slowest memory or check "
            "the endpoint, then run it again"
        )
    return line + "; run it again to continue"


def lock_path(provider: Any) -> Path:
    """One lock per Qdrant URL, collection and vault key, outside the vault."""

    configured = os.environ.get("XDG_STATE_HOME") or ""
    # The XDG spec: a relative path is invalid and is ignored.
    base = configured if os.path.isabs(configured) else str(Path.home() / ".local" / "state")
    key = f"{provider.settings.url}|{provider.settings.collection}|{provider.vault_key}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return Path(base) / "memfw-adapter-qdrant" / f"sync-{digest}.lock"


def acquire_lock(provider: Any) -> IO[str] | _NoLock | None:
    """A held non-blocking ``flock``, or None when another ``sync`` holds it.

    The kernel drops the lock when the process ends, so a crashed run never
    leaves it held. Without ``fcntl`` (Windows) there is no lock.
    """

    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX
        return _NoLock()
    path = lock_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    except OSError:
        handle.close()
        raise
    return handle


class _NoLock:
    def close(self) -> None:
        return None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
