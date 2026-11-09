"""Synthetic duplicate, conflict, and stale cases for a later dream.

The records are fixtures. They are not a vault, and they contain no host
names, addresses, or secrets. Expected merges, conflict candidates, and stale
marks are the gold labels. Nothing here writes a memory or calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Memory:
    memory_id: str
    about: str
    attribute: str
    value: str
    body: str
    recorded_at: str
    valid_to: str | None = None


@dataclass(frozen=True, slots=True)
class Conflict:
    about: str
    attribute: str
    memory_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    kind: str
    memories: tuple[Memory, ...]
    merges: tuple[tuple[str, str], ...]
    conflicts: tuple[Conflict, ...]
    stale: tuple[str, ...]


def cases() -> tuple[Case, ...]:
    """Return the fixed synthetic set. Order is part of the fixture."""

    return (
        _duplicate_lamp(),
        _duplicate_port(),
        _duplicate_three(),
        _conflict_color(),
        _different_attributes(),
        _stale_closed(),
        _stale_superseded(),
    )


def _duplicate_lamp() -> Case:
    kept = "mem-lamp-kept"
    duplicate = "mem-lamp-dup"
    other = "mem-desk"
    return Case(
        case_id="duplicate-lamp",
        kind="duplicate",
        memories=(
            Memory(
                kept,
                "concept:fixture:lamp",
                "color",
                "blue",
                "The lamp color is blue.",
                "2026-01-02T00:00:00+00:00",
            ),
            Memory(
                duplicate,
                "concept:fixture:lamp",
                "color",
                "blue",
                "Lamp colour is blue.",
                "2026-02-02T00:00:00+00:00",
            ),
            Memory(
                other,
                "concept:fixture:desk",
                "color",
                "blue",
                "The desk color is blue.",
                "2026-02-03T00:00:00+00:00",
            ),
        ),
        merges=((duplicate, kept),),
        conflicts=(),
        stale=(),
    )


def _duplicate_port() -> Case:
    return Case(
        case_id="duplicate-port",
        kind="duplicate",
        memories=(
            Memory(
                "mem-port-kept",
                "concept:fixture:service",
                "port",
                "80",
                "The service listens on port 80.",
                "2026-03-01T00:00:00+00:00",
            ),
            Memory(
                "mem-port-dup",
                "concept:fixture:service",
                "port",
                "80",
                "Service port is 80.",
                "2026-03-04T00:00:00+00:00",
            ),
        ),
        merges=(("mem-port-dup", "mem-port-kept"),),
        conflicts=(),
        stale=(),
    )


def _duplicate_three() -> Case:
    return Case(
        case_id="duplicate-three",
        kind="duplicate",
        memories=(
            Memory(
                "mem-tri-a",
                "concept:fixture:shelf",
                "count",
                "4",
                "The shelf holds four crates.",
                "2026-04-01T00:00:00+00:00",
            ),
            Memory(
                "mem-tri-b",
                "concept:fixture:shelf",
                "count",
                "4",
                "Four crates are on the shelf.",
                "2026-04-02T00:00:00+00:00",
            ),
            Memory(
                "mem-tri-c",
                "concept:fixture:shelf",
                "count",
                "4",
                "Crate count for the shelf is four.",
                "2026-04-03T00:00:00+00:00",
            ),
        ),
        merges=(("mem-tri-b", "mem-tri-a"), ("mem-tri-c", "mem-tri-a")),
        conflicts=(),
        stale=(),
    )


def _conflict_color() -> Case:
    return Case(
        case_id="conflict-color",
        kind="conflict",
        memories=(
            Memory(
                "mem-cluster-blue",
                "concept:fixture:cluster",
                "color",
                "blue",
                "The cluster color is blue.",
                "2026-05-01T00:00:00+00:00",
            ),
            Memory(
                "mem-cluster-green",
                "concept:fixture:cluster",
                "color",
                "green",
                "The cluster color is green.",
                "2026-05-01T00:00:00+00:00",
            ),
        ),
        merges=(),
        conflicts=(
            Conflict(
                "concept:fixture:cluster",
                "color",
                ("mem-cluster-blue", "mem-cluster-green"),
            ),
        ),
        stale=(),
    )


def _different_attributes() -> Case:
    return Case(
        case_id="different-attributes",
        kind="conflict",
        memories=(
            Memory(
                "mem-attr-color",
                "concept:fixture:badge",
                "color",
                "red",
                "The badge color is red.",
                "2026-05-02T00:00:00+00:00",
            ),
            Memory(
                "mem-attr-size",
                "concept:fixture:badge",
                "size",
                "small",
                "The badge size is small.",
                "2026-05-02T00:00:00+00:00",
            ),
        ),
        merges=(),
        conflicts=(),
        stale=(),
    )


def _stale_closed() -> Case:
    return Case(
        case_id="stale-closed",
        kind="stale",
        memories=(
            Memory(
                "mem-window-old",
                "concept:fixture:window",
                "start",
                "01:00",
                "The window used to start at 01:00.",
                "2026-01-01T00:00:00+00:00",
                "2026-04-01T00:00:00+00:00",
            ),
            Memory(
                "mem-window-now",
                "concept:fixture:window",
                "start",
                "02:00",
                "The window now starts at 02:00.",
                "2026-04-01T00:00:00+00:00",
            ),
        ),
        merges=(),
        conflicts=(),
        stale=("mem-window-old",),
    )


def _stale_superseded() -> Case:
    return Case(
        case_id="stale-superseded",
        kind="stale",
        memories=(
            Memory(
                "mem-policy-old",
                "concept:fixture:policy",
                "mode",
                "open",
                "The policy mode was open.",
                "2026-02-01T00:00:00+00:00",
            ),
            Memory(
                "mem-policy-new",
                "concept:fixture:policy",
                "mode",
                "closed",
                "The policy mode is closed.",
                "2026-07-01T00:00:00+00:00",
            ),
        ),
        merges=(),
        conflicts=(
            Conflict(
                "concept:fixture:policy",
                "mode",
                ("mem-policy-old", "mem-policy-new"),
            ),
        ),
        stale=("mem-policy-old",),
    )
