"""Attachment search and read for the MCP tools (MM2b): proxies first, originals last.

``attachment_search`` runs the recall coordinator with the built-in
``ProxyProvider`` alone (the memory recall assembly never includes it, so
memory search output does not change) and returns proxy segments with their
canonical locators. ``attachment_get`` returns one attachment's metadata and
the text of its active proxies, and, only on request, the original:

* never unless ``vault.yaml`` sets ``mcp.local_originals: true`` (default
  off). That switch is the operator's statement that this MCP server runs as a
  local stdio process for a local client; memfw does not guess it. The HTTP
  read-only service has no attachment route at all.
* the original's sha256 is re-computed from the same open file the bytes come
  from (opened without following a symlink or blocking, and checked to be the
  regular file ``lstat`` saw); a size or hash mismatch refuses it. A ``path``
  is verified at call time only: what a client reads from it later is not.

Both tools exist only with ``mcp.attachments: true``. Every result passes the
caller's tenant (attachments belong to the default tenant ``""``), source
allow-list (attachments have no source, so an allow-list hides them) and
privacy clearance (``memfw.ports.privacy``; the level is the strictest of the
proxy's and the attachment's), and the attachment must be active (not
detached, missing or purged: ``Repository.attachment_state_map``). A withheld
attachment, or a damaged one asked for under a clearance, reads as missing.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from memfw.core.errors import IntegrityError, MemfwError, NotFoundError
from memfw.ports.privacy import admits
from memfw.ports.proxies import Locator, ProxyFormatError

if TYPE_CHECKING:
    from memfw.attachments.proxies import ProxyRecord
    from memfw.attachments.records import AttachmentRecord
    from memfw.core.repository import Repository

    from .runtime import Runtime

#: ``vault.yaml`` ``mcp:`` section keys (``memfw.attachments.proxies`` validates them).
MCP_SECTION: Final = "mcp"
ATTACHMENTS_SWITCH: Final = "attachments"
LOCAL_ORIGINALS_SWITCH: Final = "local_originals"
MAX_SEARCH_LIMIT: Final = 50
MAX_GET_SEGMENTS: Final = 200
#: Largest byte range ``attachment_get`` returns inline.
MAX_ORIGINAL_BYTES: Final = 1024 * 1024
#: Largest original ``original=bytes`` serves: every call re-hashes the whole
#: file, so paging through a bigger one would hash it again and again.
MAX_BYTES_MODE_ORIGINAL: Final = 32 * 1024 * 1024
ORIGINAL_MODES: Final = ("path", "bytes")


@dataclass(frozen=True)
class ExitOptions:
    attachments: bool = False
    local_originals: bool = False


def exit_options(repository: Repository | None) -> ExitOptions:
    """The ``mcp:`` switches; anything missing, unreadable or not ``true`` is off."""

    if repository is None:
        return ExitOptions()
    try:
        config = repository.read_config()
    except (MemfwError, OSError, ValueError):
        return ExitOptions()
    section = config.get(MCP_SECTION)
    if not isinstance(section, Mapping):
        return ExitOptions()
    return ExitOptions(
        attachments=section.get(ATTACHMENTS_SWITCH) is True,
        local_originals=section.get(LOCAL_ORIGINALS_SWITCH) is True,
    )


def tools_enabled(runtime: Runtime) -> bool:
    try:
        repository: Repository | None = runtime.open_repository()
    except (MemfwError, OSError, ValueError):
        repository = None
    return exit_options(repository).attachments


def _visible_scope(runtime: Runtime) -> bool:
    # Attachments carry no tenant (default ``""``) and no source.
    return runtime.tenant == "" and not runtime.allowed_sources


def _admitted(runtime: Runtime, record: ProxyRecord | None, attachment: AttachmentRecord) -> bool:
    level = attachment.privacy_level if record is None else record.effective_level(attachment)
    return admits(runtime.clearance, level)


def _segment_json(index: int, segment: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"n": index, "text": segment.text}
    locator = segment.locator.to_json()
    if locator:
        data["locator"] = locator
    data["fragment"] = segment.locator.fragment()
    if segment.speaker is not None:
        data["speaker"] = segment.speaker
    if segment.confidence is not None:
        data["confidence"] = segment.confidence
    return data


def _proxy_meta(record: ProxyRecord, attachment: AttachmentRecord) -> dict[str, Any]:
    data: dict[str, Any] = {
        "proxy_id": record.id,
        "kind": record.kind,
        "coverage": record.coverage.to_json(),
        "partial": record.coverage.status == "partial",
        "status": record.status,
        "privacy": record.effective_level(attachment),
    }
    if record.producer is not None:
        data["producer"] = record.producer
    if record.kind == "transcript":
        data["speaker_labels"] = "unverified"
    return data


def attachment_search(
    runtime: Runtime, query: str, *, limit: int = 10, now: datetime | None = None
) -> dict[str, Any]:
    """Proxy segments matching ``query`` that the caller may see, best first."""

    from memfw.index.proxies import attachment_active, ensure_proxy_index, retention_ended
    from memfw.recall import Coordinator, FilterPolicy
    from memfw.recall.builtin import ProxyProvider, ProxySegmentResolver
    from memfw.recall.policy import GRANTABLE_PRIVACY

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query required")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    limit = min(limit, MAX_SEARCH_LIMIT)
    moment = now or datetime.now(UTC)
    repository = runtime.open_repository()
    empty: dict[str, Any] = {"results": [], "recall": {}}
    if not _visible_scope(runtime):
        return empty
    built = ensure_proxy_index(repository, now=moment)
    if built is None:
        return empty
    coordinator = Coordinator(
        [ProxyProvider(runtime.root, clearance=runtime.clearance, index_path=built.path)],
        resolvers={"attachment_segment": ProxySegmentResolver(built.path, tenant=runtime.tenant)},
        inflight=runtime.recall_inflight,
        total_timeout=runtime.total_timeout,
        clock=lambda: moment,
    )
    # Segments come back in ``result.segments`` (never as memory hits). The
    # coordinator checks tenant and privacy class; the MM0 clearance is
    # applied by the index and re-checked below against the files.
    policy = FilterPolicy(
        tenant=runtime.tenant,
        privacy_clearance=GRANTABLE_PRIVACY,
        check_privacy=False,
        check_status=False,
        drop_archived=False,
        temporal_mode="index",
    )
    result = coordinator.search(query, limit=limit, policy=policy)
    state = repository.proxy_state()
    proxies: dict[str, ProxyRecord | None] = {}
    attachments: dict[str, AttachmentRecord | None] = {}
    rows: list[dict[str, Any]] = []
    for hit in result.segments:
        # A segment hit's anchor is its ref, ``<prx-id>:<n>``.
        proxy_id, _, number = hit.anchor.partition(":")
        if not state.active(proxy_id):
            continue
        if proxy_id not in proxies:
            try:
                proxies[proxy_id] = repository.get_proxy(proxy_id)
            except (IntegrityError, NotFoundError):
                proxies[proxy_id] = None
        record = proxies[proxy_id]
        if record is None:
            continue
        owner = record.attachment_id
        if owner not in attachments:
            try:
                attachments[owner] = (
                    repository.get_attachment(owner)
                    if attachment_active(repository, owner)
                    else None
                )
            except (IntegrityError, NotFoundError):
                attachments[owner] = None
        attachment = attachments[owner]
        if (
            attachment is None
            or attachment.sha256 != record.source_sha256
            or retention_ended(attachment, moment.date())
            or not _admitted(runtime, record, attachment)
        ):
            continue
        index = int(number)
        if not 1 <= index <= len(record.segments):
            continue
        row = {
            "ref": f"{proxy_id}:{index}",
            "attachment_id": owner,
            "media_type": attachment.media_type,
            **_proxy_meta(record, attachment),
            "segment": _segment_json(index, record.segments[index - 1]),
            "score": hit.score,
        }
        rows.append(row)
    return {"results": rows, "recall": result.labels.as_dict()}


def _overlaps(wanted: Locator, have: Locator) -> bool:
    """Whether a stored segment's locator falls within the requested one."""

    if wanted.page is not None and have.page != wanted.page:
        return False
    if wanted.bbox is not None:
        if have.bbox is None:
            return False
        a, b = wanted.bbox, have.bbox
        if b[0] >= a[2] or a[0] >= b[2] or b[1] >= a[3] or a[1] >= b[3]:
            return False
    if wanted.t is not None or wanted.frame is not None:
        start, end = wanted.t if wanted.t is not None else (wanted.frame, wanted.frame)
        if have.t is not None:
            lo, hi = have.t
        elif have.frame is not None:
            lo = hi = have.frame
        else:
            return False
        assert start is not None and end is not None
        if hi < start or lo > end:
            return False
    return True


def attachment_get(
    runtime: Runtime,
    attachment_id: str,
    *,
    locator: str | None = None,
    original: str | None = None,
    offset: int = 0,
    length: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One attachment's metadata, its active proxies' segments, and the original on request."""

    from memfw.attachments.records import check_attachment_id
    from memfw.index.proxies import attachment_active, retention_ended

    identifier = check_attachment_id(attachment_id)
    moment = now or datetime.now(UTC)
    if original is not None and original not in ORIGINAL_MODES:
        raise ValueError("original must be path or bytes")
    wanted = Locator()
    if locator:
        try:
            wanted = Locator.parse(locator)
        except ProxyFormatError as exc:
            raise ValueError(str(exc)) from None
    repository = runtime.open_repository()
    missing = NotFoundError(f"Attachment not found: {identifier}")
    if not _visible_scope(runtime):
        raise missing
    try:
        attachment = repository.get_attachment(identifier)
    except NotFoundError:
        raise missing from None
    except IntegrityError:
        # A damaged record cannot say its level: only an unfiltered (local
        # operator) caller learns it is damaged; any clearance sees "not found".
        if runtime.clearance is None:
            raise
        raise missing from None
    if (
        not _admitted(runtime, None, attachment)
        or retention_ended(attachment, moment.date())
        or not attachment_active(repository, identifier)
    ):
        raise missing
    payload: dict[str, Any] = {
        "id": attachment.id,
        "media_type": attachment.media_type,
        "size": attachment.size,
        "sha256": attachment.sha256,
        "storage": attachment.storage,
        "privacy": attachment.privacy_level,
        "recorded_at": attachment.recorded_at,
        "retention": attachment.retention,
    }
    for key in ("original_name", "captured_at", "rights"):
        value = getattr(attachment, key)
        if value is not None:
            payload[key] = value
    proxies: list[dict[str, Any]] = []
    budget = MAX_GET_SEGMENTS
    truncated = False
    for _proxy_id, record, by in repository.proxy_records(identifier):
        if by is not None or isinstance(record, MemfwError):
            continue
        if record.source_sha256 != attachment.sha256 or not _admitted(runtime, record, attachment):
            continue
        segments = []
        for index, segment in enumerate(record.segments, start=1):
            if not _overlaps(wanted, segment.locator):
                continue
            if budget == 0:
                truncated = True
                break
            segments.append(_segment_json(index, segment))
            budget -= 1
        proxies.append({**_proxy_meta(record, attachment), "segments": segments})
    payload["proxies"] = proxies
    payload["truncated"] = truncated
    if original is not None:
        payload["original"] = _original(
            repository, attachment, mode=original, offset=offset, length=length
        )
    return payload


def _original(
    repository: Repository,
    attachment: AttachmentRecord,
    *,
    mode: str,
    offset: int,
    length: int | None,
) -> dict[str, Any]:
    if not exit_options(repository).local_originals:
        raise PermissionError(
            "originals are not served: set mcp.local_originals: true in vault.yaml "
            "(local stdio clients only)"
        )
    if attachment.storage != "embedded":
        raise ValueError("only embedded originals can be served")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if length is None:
        length = min(attachment.size - offset, MAX_ORIGINAL_BYTES) if mode == "bytes" else 0
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise ValueError("length must be a non-negative integer")
    if mode == "bytes" and length > MAX_ORIGINAL_BYTES:
        raise ValueError(f"at most {MAX_ORIGINAL_BYTES} bytes per call")
    if mode == "bytes" and attachment.size > MAX_BYTES_MODE_ORIGINAL:
        raise ValueError(
            f"originals over {MAX_BYTES_MODE_ORIGINAL} bytes are served by path, not bytes"
        )
    store = repository.attachment_blob_store()
    if store is None:
        raise IntegrityError("the vault has no blob store")
    path = store.path_for(attachment.sha256)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise IntegrityError("the original is missing; run memfw attach verify") from None
    if not stat.S_ISREG(info.st_mode):
        raise IntegrityError("the original is not a regular file; run memfw attach verify")
    changed = IntegrityError(
        "the original no longer matches its sha256; refused (run memfw attach verify)"
    )
    if info.st_size != attachment.size:
        raise changed
    digest = hashlib.sha256()
    captured = bytearray()
    position = 0
    end = offset + length
    # Never follow a symlink, never block on a FIFO swapped in after lstat.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        handle = os.open(path, flags)
    except OSError:
        raise IntegrityError("the original cannot be opened; run memfw attach verify") from None
    opened = os.fstat(handle)
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        info.st_dev,
        info.st_ino,
    ):
        os.close(handle)
        raise IntegrityError("the original changed while it was opened; refused")
    # One pass over one open file: the bytes returned are the bytes hashed.
    with os.fdopen(handle, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            if mode == "bytes" and position < end and position + len(chunk) > offset:
                captured += chunk[max(0, offset - position) : max(0, end - position)]
            position += len(chunk)
    if digest.hexdigest() != attachment.sha256 or position != attachment.size:
        raise changed
    result: dict[str, Any] = {"sha256": attachment.sha256, "size": position, "verified": True}
    if mode == "path":
        # Verified at this call only: a client that opens the path later reads
        # whatever is there then (use ``bytes`` for bytes that were hashed).
        result["path"] = str(path)
        result["verified_at_call_only"] = True
    else:
        result.update(
            offset=offset, length=len(captured), bytes_b64=base64.b64encode(captured).decode()
        )
    return result


__all__ = [
    "ATTACHMENTS_SWITCH",
    "LOCAL_ORIGINALS_SWITCH",
    "MCP_SECTION",
    "ExitOptions",
    "attachment_get",
    "attachment_search",
    "exit_options",
    "tools_enabled",
]
