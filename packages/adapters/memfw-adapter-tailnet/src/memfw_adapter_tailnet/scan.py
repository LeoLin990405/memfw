"""One scan: diff against the previous snapshot, candidates, alerts, report.

State lives in the operator's ``--state`` directory (``0700``, owned by the
operator), never in the vault:

* ``snapshot.json`` (``0600``): the hosts of the last *committed* run, by
  hashed host id, plus tombstones (``gone_since``) for hosts that left. It
  holds host names and tailnet IPs, so it is as private as the inventory.
* ``report.json`` (``0600``): counts and fixed codes only, no names, ids or
  addresses; the feed the alerting stage reads.
* ``.lock``: one scan at a time.

A run commits (advances the snapshot) only after its candidates were staged;
a run without a vault, or one that fails, leaves the snapshot as it was, so
the next run sees the same changes again. Candidates carry only identity
fields and the previous snapshot's ``taken_at``, so a retry against the same
snapshot stages byte-identical candidates (duplicates, not new files).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ._ports import ExtraValue, IngestItem
from .status import Host, Status, TailnetError, dns_parts, parse_time

SNAPSHOT_FILE = "snapshot.json"
REPORT_FILE = "report.json"
LOCK_FILE = ".lock"
SNAPSHOT_FORMAT = "memfw.tailnet.snapshot"
REPORT_FORMAT = "memfw.tailnet.report"
FACT_FORMAT = "memfw.tailnet.host-fact"
FORMAT_VERSION = 1
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_ALLOWLIST_BYTES = 1024 * 1024
MAX_ALLOWLIST_ENTRIES = 10_000
DEFAULT_OFFLINE_HOURS = 24.0
#: Tombstones older than this are forgotten; a host back after that is ``new``.
TOMBSTONE_DAYS = 180

#: Change codes, in the order they are listed on a candidate.
CHANGE_CODES: tuple[str, ...] = (
    "new",
    "returned",
    "gone",
    "renamed",
    "ip_changed",
    "os_changed",
    "tags_changed",
)
#: Alert codes ``report.json`` may carry.
ALERT_CODES: tuple[str, ...] = ("listed_offline", "listed_missing", "listed_ambiguous", "unknown")
_ENTRY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,252}$")
_HOST_ID = re.compile(r"^host-[0-9a-f]{24}$")
_TIME = "%Y-%m-%dT%H:%M:%SZ"


# -- state directory -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Change:
    host_id: str
    codes: tuple[str, ...]
    current: Host | None
    previous: Mapping[str, Any] | None


def _check_state_dir(state: Path) -> Path:
    """Create the directory ``0700``, or tighten an existing one we own.

    A directory owned by someone else cannot be made private by us: it is
    refused (``state_insecure``), as is a symlink.
    """

    directory = Path(state).expanduser()
    if directory.is_symlink():
        raise TailnetError("state_is_symlink")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(directory)
    except OSError as exc:
        raise TailnetError("state_unwritable") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise TailnetError("state_unwritable")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise TailnetError("state_insecure")
    if stat.S_IMODE(info.st_mode) & 0o077:
        try:
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise TailnetError("state_insecure") from exc
    return directory


@contextmanager
def state_lock(state: Path) -> Iterator[Path]:
    """The checked state directory, held exclusively for the block."""

    directory = _check_state_dir(state)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX: no lock, single operator
        yield directory
        return
    lock = directory / LOCK_FILE
    try:
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise TailnetError("state_unwritable") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TailnetError("state_locked") from exc
        yield directory
    finally:
        os.close(descriptor)


def write_private(path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON atomically with mode ``0600`` (temporary file, then rename)."""

    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise TailnetError("state_unwritable") from exc
    finally:
        temporary.unlink(missing_ok=True)


# -- snapshot --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The last committed run: hosts (and tombstones) by id, and when it was taken."""

    hosts: Mapping[str, Mapping[str, Any]]
    taken_at: str


def _str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _optional_time(value: object) -> bool:
    return value is None or (isinstance(value, str) and parse_time(value) is not None)


def _valid_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    texts = ("name", "dns_label", "dns_suffix_hash", "os")
    return (
        all(isinstance(record.get(key), str) for key in texts)
        and _str_list(record.get("ips"))
        and _str_list(record.get("tags"))
        and (record.get("online") is None or isinstance(record.get("online"), bool))
        and isinstance(record.get("self", False), bool)
        and all(
            _optional_time(record.get(key))
            for key in ("last_seen", "first_seen", "offline_since", "gone_since")
        )
    )


def load_snapshot(state: Path) -> Snapshot | None:
    """The previous committed run; ``None`` when there was none.

    Anything that is not exactly what :func:`write_snapshot` writes is refused
    with ``bad_snapshot`` rather than treated as a first run.
    """

    path = Path(state) / SNAPSHOT_FILE
    if path.is_symlink():
        raise TailnetError("bad_snapshot")
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise TailnetError("bad_snapshot")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise TailnetError("bad_snapshot") from exc
    if (
        not isinstance(document, dict)
        or document.get("format") != SNAPSHOT_FORMAT
        or document.get("version") != FORMAT_VERSION
        or not isinstance(document.get("hosts"), dict)
        or not isinstance(document.get("taken_at"), str)
        or parse_time(document["taken_at"]) is None
    ):
        raise TailnetError("bad_snapshot")
    hosts: dict[str, dict[str, Any]] = {}
    for key, record in document["hosts"].items():
        if not _HOST_ID.fullmatch(key) or not _valid_record(record):
            raise TailnetError("bad_snapshot")
        hosts[key] = record
    return Snapshot(hosts=hosts, taken_at=document["taken_at"])


def _identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": record.get("name", ""),
        "dns_label": record.get("dns_label", ""),
        "dns_suffix_hash": record.get("dns_suffix_hash", ""),
        "ips": list(record.get("ips") or []),
        "os": record.get("os", ""),
        "tags": list(record.get("tags") or []),
    }


def _gone(record: Mapping[str, Any]) -> bool:
    return record.get("gone_since") is not None


def _identity_codes(then: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    codes = []
    keys = ("name", "dns_label", "dns_suffix_hash")
    if tuple(then[k] for k in keys) != tuple(now[k] for k in keys):
        codes.append("renamed")
    if then["ips"] != now["ips"]:
        codes.append("ip_changed")
    if then["os"] != now["os"]:
        codes.append("os_changed")
    if then["tags"] != now["tags"]:
        codes.append("tags_changed")
    return codes


def diff(previous: Snapshot | None, status: Status) -> list[Change]:
    """New, returned, gone and changed hosts; unchanged hosts are not listed.

    ``online`` and ``last_seen`` never make a change on their own (they move
    every run); they feed the alerts instead. A host that left is reported
    ``gone`` once and kept as a tombstone; if it comes back it is
    ``returned`` (with what it looked like before), not ``new``.
    """

    before = previous.hosts if previous is not None else {}
    current = {host.host_id: host for host in status.hosts}
    changes: list[Change] = []
    for identifier in sorted(set(before) | set(current)):
        host, old = current.get(identifier), before.get(identifier)
        if host is None:
            if old is not None and not _gone(old):
                changes.append(Change(identifier, ("gone",), None, old))
            continue
        if old is None:
            changes.append(Change(identifier, ("new",), host, None))
            continue
        codes = _identity_codes(_identity(old), host.identity())
        if _gone(old):
            changes.append(Change(identifier, ("returned", *codes), host, old))
        elif codes:
            changes.append(Change(identifier, tuple(codes), host, old))
    return changes


def next_snapshot(
    previous: Snapshot | None, status: Status, now: datetime
) -> dict[str, dict[str, Any]]:
    """The hosts to remember, with ``offline_since`` carried and tombstones kept."""

    stamp = _stamp(now)
    before = previous.hosts if previous is not None else {}
    hosts: dict[str, dict[str, Any]] = {}
    for host in status.hosts:
        record: dict[str, Any] = host.as_dict()
        old = before.get(host.host_id) or {}
        record["first_seen"] = old.get("first_seen") or stamp
        record["gone_since"] = None
        if host.online is False:
            since = old.get("offline_since") if old.get("online") is False else None
            record["offline_since"] = since or host.last_seen or stamp
        else:
            record["offline_since"] = None
        hosts[host.host_id] = record
    horizon = now - timedelta(days=TOMBSTONE_DAYS)
    for identifier, old in before.items():
        if identifier in hosts:
            continue
        gone_since = old.get("gone_since") or stamp
        if _moment(gone_since) < horizon:
            continue
        hosts[identifier] = {**old, "gone_since": gone_since}
    return hosts


# -- candidates ------------------------------------------------------------------------


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:63] or "unnamed"


def change_items(changes: Sequence[Change], *, privacy: str, since: str | None) -> list[IngestItem]:
    """One host-fact candidate per change (``epistemic: fact``, ``causal: none``).

    Only identity fields go in: no ``online``, ``last_seen`` or wall-clock
    time. ``since`` is the previous snapshot's ``taken_at`` (``None`` on a
    first run), which stays the same until a run commits. So a retry after a
    crash between staging and the snapshot write produces the same candidate
    ids, and staging reports them as duplicates.
    """

    items: list[IngestItem] = []
    for change in changes:
        current = None
        if change.current is not None:
            current = {
                "host_id": change.host_id,
                **change.current.identity(),
                "self": change.current.is_self,
            }
        previous = None
        if change.previous is not None:
            previous = {"host_id": change.host_id, **_identity(change.previous)}
        name = str((current or previous or {}).get("name") or "unnamed")
        codes = "+".join(change.codes)
        body = {
            "format": FACT_FORMAT,
            "version": FORMAT_VERSION,
            "host_id": change.host_id,
            "since": since,
            "changes": list(change.codes),
            "host": current,
            "previous": previous,
        }
        digest = hashlib.sha256(
            json.dumps([codes, previous, current], sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        metadata: dict[str, ExtraValue] = {
            "adapter": "tailnet",
            "kind": "host-fact",
            "host_id": change.host_id,
            "changes": codes,
            "concept": f"host:{_slug(name)}",
            "privacy": privacy,
            "epistemic": "fact",
            "causal": "none",
        }
        title = f"tailnet host {codes.replace('+', ', ')}: {name}"[:120]
        items.append(
            IngestItem(
                title=title,
                body=json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2),
                external_ref=f"tailnet:{change.host_id}:{codes}:{digest}",
                metadata=metadata,
            )
        )
    return items


# -- allowlist and alerts --------------------------------------------------------------


def load_allowlist(path: Path) -> frozenset[str]:
    """Operator-maintained entries: one per line, ``#`` comments, case-insensitive.

    An entry is a hashed host id (``host-…``), a full MagicDNS name
    (``label.<tailnet suffix>``) or a bare first DNS label. The self-reported
    ``HostName`` is never matched: any machine can call itself anything.
    Scans only read this file.
    """

    source = Path(path).expanduser()
    try:
        if not source.is_file():
            raise TailnetError("allowlist_unreadable")
        with source.open("rb") as handle:
            data = handle.read(MAX_ALLOWLIST_BYTES + 1)
    except OSError as exc:
        raise TailnetError("allowlist_unreadable") from exc
    if len(data) > MAX_ALLOWLIST_BYTES:
        raise TailnetError("allowlist_too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TailnetError("bad_allowlist") from exc
    entries: set[str] = set()
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip().rstrip(".").lower()
        if not entry:
            continue
        if not _ENTRY.fullmatch(entry):
            raise TailnetError("bad_allowlist")
        entries.add(entry)
        if len(entries) > MAX_ALLOWLIST_ENTRIES:
            raise TailnetError("allowlist_too_large")
    return frozenset(entries)


def _entry_key(entry: str) -> tuple[str, ...]:
    if _HOST_ID.fullmatch(entry):
        return ("id", entry)
    if "." in entry:
        label, suffix_hash = dns_parts(entry)
        return ("fqdn", label, suffix_hash)
    return ("label", entry)


def _host_keys(identifier: str, record: Mapping[str, Any]) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = {("id", identifier)}
    label = str(record.get("dns_label") or "")
    if label:
        keys.add(("label", label))
        suffix_hash = str(record.get("dns_suffix_hash") or "")
        if suffix_hash:
            keys.add(("fqdn", label, suffix_hash))
    return keys


def alert_counts(
    hosts: Mapping[str, Mapping[str, Any]],
    allowlist: frozenset[str],
    *,
    now: datetime,
    offline_hours: float,
) -> dict[str, int]:
    """``listed_offline``, ``listed_missing``, ``listed_ambiguous`` and ``unknown``.

    Only present hosts count (tombstones do not). An entry that matches more
    than one host is ``listed_ambiguous`` and vouches for none of them.
    """

    present = {key: record for key, record in hosts.items() if not _gone(record)}
    host_keys = {key: _host_keys(key, record) for key, record in present.items()}
    matches: dict[str, list[str]] = {}
    for entry in allowlist:
        wanted = _entry_key(entry)
        matches[entry] = [key for key, keys in host_keys.items() if wanted in keys]
    vouched = {hits[0] for hits in matches.values() if len(hits) == 1}
    limit = timedelta(hours=offline_hours)
    listed_offline = 0
    for key in vouched:
        record = present[key]
        if record.get("online") is not False:
            continue
        since = parse_time(record.get("last_seen")) or parse_time(record.get("offline_since"))
        if since is not None and now - _moment(since) > limit:
            listed_offline += 1
    return {
        "listed_offline": listed_offline,
        "listed_missing": sum(1 for hits in matches.values() if not hits),
        "listed_ambiguous": sum(1 for hits in matches.values() if len(hits) > 1),
        "unknown": len(present) - len(vouched),
    }


# -- one run ---------------------------------------------------------------------------


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime(_TIME)


def _moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, _TIME).replace(tzinfo=UTC)


@dataclass(slots=True)
class ScanResult:
    """What a scan found. ``report`` is the names-free ``report.json`` payload."""

    status: Status
    changes: list[Change]
    items: list[IngestItem]
    snapshot: dict[str, dict[str, Any]]
    report: dict[str, Any] = field(default_factory=dict)


def build_scan(
    status: Status,
    previous: Snapshot | None,
    *,
    now: datetime,
    privacy: str,
    allowlist: frozenset[str] | None,
    offline_hours: float,
    mode: str,
) -> ScanResult:
    """Pure part of a scan: nothing is read or written here."""

    changes = diff(previous, status)
    since = previous.taken_at if previous is not None else None
    items = change_items(changes, privacy=privacy, since=since)
    snapshot = next_snapshot(previous, status, now)
    by_code = {code: sum(1 for c in changes if code in c.codes) for code in CHANGE_CODES}
    counts: dict[str, int | None] = {
        "hosts": len(status.hosts),
        "online": sum(1 for h in status.hosts if h.online is True),
        "offline": sum(1 for h in status.hosts if h.online is False),
        "skipped": status.skipped,
        "skipped_shared": status.skipped_shared,
        "skipped_location": status.skipped_location,
        "candidates": len(items),
        **by_code,
        "listed_offline": None,
        "listed_missing": None,
        "listed_ambiguous": None,
        "unknown": None,
    }
    alerts: list[str] = []
    if allowlist is not None:
        found = alert_counts(snapshot, allowlist, now=now, offline_hours=offline_hours)
        counts.update(found)
        alerts = [code for code in ALERT_CODES if found[code] > 0]
    report = {
        "format": REPORT_FORMAT,
        "version": FORMAT_VERSION,
        "generated_at": _stamp(now),
        "mode": mode,
        "first_run": previous is None,
        "allowlist": allowlist is not None,
        "offline_hours": offline_hours,
        "counts": counts,
        "alerts": alerts,
        "status": "alert" if alerts else "ok",
        "committed": False,
    }
    return ScanResult(status, changes, items, snapshot, report)


def error_report(code: str, *, now: datetime, mode: str) -> dict[str, Any]:
    return {
        "format": REPORT_FORMAT,
        "version": FORMAT_VERSION,
        "generated_at": _stamp(now),
        "mode": mode,
        "status": "error",
        "error": code,
        "alerts": [],
        "committed": False,
    }


def write_snapshot(state: Path, hosts: Mapping[str, Mapping[str, Any]], now: datetime) -> None:
    write_private(
        Path(state) / SNAPSHOT_FILE,
        {
            "format": SNAPSHOT_FORMAT,
            "version": FORMAT_VERSION,
            "taken_at": _stamp(now),
            "hosts": {key: dict(value) for key, value in sorted(hosts.items())},
        },
    )


def write_report(state: Path, report: Mapping[str, Any]) -> None:
    write_private(Path(state) / REPORT_FILE, report)


__all__ = [
    "ALERT_CODES",
    "CHANGE_CODES",
    "DEFAULT_OFFLINE_HOURS",
    "REPORT_FILE",
    "SNAPSHOT_FILE",
    "TOMBSTONE_DAYS",
    "Change",
    "ScanResult",
    "Snapshot",
    "alert_counts",
    "build_scan",
    "change_items",
    "diff",
    "error_report",
    "load_allowlist",
    "load_snapshot",
    "next_snapshot",
    "state_lock",
    "write_private",
    "write_report",
    "write_snapshot",
]
