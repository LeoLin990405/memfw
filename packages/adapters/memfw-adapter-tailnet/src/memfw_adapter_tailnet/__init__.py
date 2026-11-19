"""memfw-adapter-tailnet: the tailnet's machine list as reviewable host facts.

Reads ``tailscale status --json`` (an exported file, or one bounded run of the
CLI), keeps a few fields per machine, diffs them against the previous run and
stages one ``host-fact`` candidate per new, gone or changed machine through
the ``tailnet`` :class:`~.ingestor.TailnetIngestor`. ``report.json`` (counts and
fixed codes, no names) feeds alerting: machines on the operator's allowlist
that stay offline, and machines the allowlist does not know. Discovery never
edits the allowlist and never decides where data is sent. Off by default.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

from .ingestor import DEFAULT_PRIVACY, NAME, SOURCE, Settings, TailnetIngestor, factory
from .scan import (
    ALERT_CODES,
    CHANGE_CODES,
    Change,
    ScanResult,
    Snapshot,
    alert_counts,
    build_scan,
    change_items,
    diff,
    load_allowlist,
    load_snapshot,
)
from .source import STATUS_ARGS, child_env, read_input, run_tailscale
from .status import (
    Host,
    Status,
    TailnetError,
    dns_parts,
    host_id,
    parse_status,
    parse_status_bytes,
)


def adapter_spec() -> Any:
    """The registry entry (``memfw.adapters`` entry point ``tailnet``)."""

    from ._ports import AdapterSpec

    return AdapterSpec(name=NAME, kind="ingestor", factory=factory, version=__version__)


__all__ = [
    "ALERT_CODES",
    "CHANGE_CODES",
    "DEFAULT_PRIVACY",
    "NAME",
    "SOURCE",
    "STATUS_ARGS",
    "Change",
    "Host",
    "ScanResult",
    "Settings",
    "Snapshot",
    "Status",
    "TailnetError",
    "TailnetIngestor",
    "__version__",
    "adapter_spec",
    "alert_counts",
    "build_scan",
    "change_items",
    "child_env",
    "diff",
    "dns_parts",
    "factory",
    "host_id",
    "load_allowlist",
    "load_snapshot",
    "parse_status",
    "parse_status_bytes",
    "read_input",
    "run_tailscale",
]
