"""Read ``tailscale status --json`` defensively into :class:`Host` records.

The JSON's schema differs across Tailscale versions, so nothing is assumed
beyond "an object". Each node (``Self`` and every value of ``Peer``) is read
through an allowlist of fields: ``ID``/``StableID`` (or, on old versions, the
node key) only to derive a hashed host id, ``ShareeNode`` and ``Location``
only to leave a node out, then ``HostName``, ``DNSName`` (first label kept,
suffix hashed), ``TailscaleIPs``, ``OS``, ``Online``, ``LastSeen`` and
``Tags``. Everything
else is never read, in particular the top-level ``User`` map (login names,
display names, profile pictures), ``CurrentTailnet``, ``UserID``, keys,
endpoints and relay details. A field of the wrong type is treated as missing;
a node with no usable id is skipped and counted.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: Largest ``tailscale status --json`` document read (file or live).
MAX_STATUS_BYTES = 8 * 1024 * 1024
#: Most nodes read from one document; the rest are counted as skipped.
MAX_NODES = 10_000
MAX_TEXT = 253
MAX_IPS = 16
MAX_TAGS = 64
#: Namespace for the hashed host id; changing it changes every id.
HOST_ID_NAMESPACE = b"memfw-adapter-tailnet/host-id/v1\x00"
#: Namespace for the hashed MagicDNS suffix (the suffix names the tailnet).
DNS_SUFFIX_NAMESPACE = b"memfw-adapter-tailnet/dns-suffix/v1\x00"
_FRACTION = re.compile(r"(\.\d{1,6})\d*")


class TailnetError(RuntimeError):
    """A refusal with a fixed, names-free code (``str(exc)`` is the code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Host:
    """One machine of the tailnet, reduced to the fields memfw keeps."""

    host_id: str
    name: str
    dns_label: str
    dns_suffix_hash: str
    ips: tuple[str, ...]
    os: str
    online: bool | None
    last_seen: str | None
    tags: tuple[str, ...]
    is_self: bool = False

    def identity(self) -> dict[str, Any]:
        """The fields a change is judged on (``online``/``last_seen`` excluded)."""

        return {
            "name": self.name,
            "dns_label": self.dns_label,
            "dns_suffix_hash": self.dns_suffix_hash,
            "ips": list(self.ips),
            "os": self.os,
            "tags": list(self.tags),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            **self.identity(),
            "online": self.online,
            "last_seen": self.last_seen,
            "self": self.is_self,
        }


@dataclass(frozen=True, slots=True)
class Status:
    hosts: tuple[Host, ...]
    skipped: int
    backend_state: str | None
    skipped_shared: int = 0
    skipped_location: int = 0


def host_id(node_id: str) -> str:
    """``host-`` plus 24 hex chars of SHA-256 over the namespaced node id."""

    digest = hashlib.sha256(HOST_ID_NAMESPACE + node_id.encode("utf-8")).hexdigest()
    return f"host-{digest[:24]}"


def dns_parts(dns_name: str) -> tuple[str, str]:
    """``(first label, hashed suffix)`` of a MagicDNS name; the suffix itself is dropped.

    The suffix (``<tailnet>.ts.net`` style, or a legacy domain) names the tailnet and
    can name its owner, so only ``sfx-`` plus 16 hex chars of its SHA-256 are kept:
    enough to see that a machine moved to another tailnet or domain.
    """

    name = dns_name.strip().rstrip(".").lower()
    if not name:
        return "", ""
    label, _, suffix = name.partition(".")
    if not suffix:
        return label, ""
    digest = hashlib.sha256(DNS_SUFFIX_NAMESPACE + suffix.encode("utf-8")).hexdigest()
    return label, f"sfx-{digest[:16]}"


def _text(raw: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    return cleaned.strip()[:limit]


def _node_id(node: dict[str, Any], key: object) -> str:
    for field in ("ID", "StableID"):
        value = node.get(field)
        if isinstance(value, str) and value.strip():
            return "id:" + value.strip()
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return "id:" + str(value)
    for value in (node.get("PublicKey"), key):  # old versions: keyed by node key only
        if isinstance(value, str) and value.strip():
            return "key:" + value.strip()
    return ""


def _ips(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    found: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for value in raw[: MAX_IPS * 4]:
        if not isinstance(value, str):
            continue
        try:
            found.add(ipaddress.ip_address(value.strip().split("%", 1)[0]))
        except ValueError:
            continue
    ordered = sorted(found, key=lambda ip: (ip.version, int(ip)))
    return tuple(str(ip) for ip in ordered[:MAX_IPS])


def _tags(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    tags = {_text(value, 128) for value in raw[: MAX_TAGS * 4] if isinstance(value, str)}
    return tuple(sorted(tag for tag in tags if tag))[:MAX_TAGS]


def parse_time(raw: object) -> str | None:
    """RFC 3339 (nanoseconds allowed) → ``YYYY-MM-DDTHH:MM:SSZ``; Go's zero time → ``None``."""

    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _FRACTION.sub(lambda m: m.group(1), raw.strip(), count=1)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    if moment.year < 1971:  # 0001-01-01T00:00:00Z means "never"
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _host(node: dict[str, Any], key: object, *, is_self: bool) -> Host | None:
    node_id = _node_id(node, key)
    if not node_id:
        return None
    dns_label, dns_suffix_hash = dns_parts(_text(node.get("DNSName")))
    name = _text(node.get("HostName")) or dns_label
    online = node.get("Online")
    return Host(
        host_id=host_id(node_id),
        name=name or "unnamed",
        dns_label=dns_label,
        dns_suffix_hash=dns_suffix_hash,
        ips=_ips(node.get("TailscaleIPs")),
        os=_text(node.get("OS"), 64),
        online=online if isinstance(online, bool) else None,
        last_seen=parse_time(node.get("LastSeen")),
        tags=_tags(node.get("Tags")),
        is_self=is_self,
    )


def parse_status(document: object, *, include_shared: bool = False) -> Status:
    """Hosts of one parsed status document. Raises :class:`TailnetError`.

    Peers shared in from another tailnet (``ShareeNode: true``) are left out and
    counted in ``skipped_shared`` unless ``include_shared``: they may be the
    owner's own machines in another tailnet, or a third party's. Peers with a
    ``Location`` (provider exit nodes such as Mullvad) are always left out and
    counted in ``skipped_location``.
    """

    if not isinstance(document, dict):
        raise TailnetError("bad_status_json")
    state = document.get("BackendState")
    backend_state = state if isinstance(state, str) else None
    if backend_state is not None and backend_state != "Running":
        # Stopped / NeedsLogin / Starting: the peer list is empty or stale, and a
        # diff against it would report every machine gone.
        raise TailnetError("backend_not_running")
    nodes: list[tuple[dict[str, Any], object, bool]] = []
    own = document.get("Self")
    if isinstance(own, dict):
        nodes.append((own, None, True))
    peers = document.get("Peer")
    if isinstance(peers, dict):
        nodes.extend((node, key, False) for key, node in peers.items() if isinstance(node, dict))
        skipped = sum(1 for node in peers.values() if not isinstance(node, dict))
    else:
        skipped = 0
    hosts: dict[str, Host] = {}
    shared = located = 0
    for index, (node, key, is_self) in enumerate(nodes):
        if index >= MAX_NODES:
            skipped += len(nodes) - MAX_NODES
            break
        if not is_self and node.get("Location") is not None:
            located += 1
            continue
        if not is_self and node.get("ShareeNode") is True and not include_shared:
            shared += 1
            continue
        host = _host(node, key, is_self=is_self)
        if host is None or host.host_id in hosts:
            skipped += 1
            continue
        hosts[host.host_id] = host
    ordered = tuple(sorted(hosts.values(), key=lambda h: h.host_id))
    return Status(
        hosts=ordered,
        skipped=skipped,
        backend_state=backend_state,
        skipped_shared=shared,
        skipped_location=located,
    )


def parse_status_bytes(data: bytes, *, include_shared: bool = False) -> Status:
    if len(data) > MAX_STATUS_BYTES:
        raise TailnetError("status_too_large")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise TailnetError("bad_status_json") from exc
    return parse_status(document, include_shared=include_shared)


__all__ = [
    "MAX_STATUS_BYTES",
    "Host",
    "Status",
    "TailnetError",
    "dns_parts",
    "host_id",
    "parse_status",
    "parse_status_bytes",
    "parse_time",
]
