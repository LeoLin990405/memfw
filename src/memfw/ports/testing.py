"""Reusable contract checks for port implementations. Stdlib only.

Each ``check_*`` function exercises one adapter and raises ``ContractError``
(an ``AssertionError``) on the first broken rule. Use them from any test
runner; they do not import pytest. An adapter's own test suite calls the
matching check with recorded fixtures, so upgrading the external project
re-runs the same contract.

    from memfw.ports.testing import check_recall_provider
    check_recall_provider(MyProvider(settings), queries=["nas1 cron"])
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from .clock import Clock, IdGen
from .context import ContextPack, compute_pack_version, pack_version_payload
from .protocols import Deriver, Ingestor, Projector, RecallProvider
from .staging import CANDIDATES_DIR
from .types import (
    Availability,
    Candidate,
    DerivedView,
    DeriveRequest,
    IngestItem,
    Manifest,
    ProjectionPolicy,
    RecallBudget,
    StageResult,
    validate_candidate,
)


class ContractError(AssertionError):
    """An adapter broke its port contract."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def tree_digest(root: Path, *, exclude: Sequence[str] = ()) -> dict[str, str]:
    """``relative path → sha256`` for every regular file under ``root``.

    Top-level names in ``exclude`` are skipped. Used to prove an adapter left
    a vault byte-identical (or changed only what it may change).
    """

    base = Path(root)
    out: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(base):
        rel_dir = Path(directory).relative_to(base)
        if rel_dir == Path("."):
            dirnames[:] = [name for name in dirnames if name not in exclude]
        for filename in filenames:
            rel = rel_dir / filename
            if rel_dir == Path(".") and filename in exclude:
                continue
            path = base / rel
            if path.is_symlink() or not path.is_file():
                continue
            out[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(out.items()))


def _check_common(adapter: object, protocol: type, what: str) -> Availability:
    _require(isinstance(adapter, protocol), f"{what} does not implement the protocol")
    name = getattr(adapter, "name", None)
    _require(isinstance(name, str) and bool(name), f"{what} needs a non-empty name")
    availability = adapter.available()  # type: ignore[attr-defined]
    if not isinstance(availability, Availability):
        raise ContractError("available() must return Availability")
    return availability


def _staged(vault: Path) -> dict[str, str]:
    directory = vault / CANDIDATES_DIR
    return tree_digest(directory) if directory.is_dir() else {}


def _timed_search(
    provider: RecallProvider,
    query: str,
    *,
    limit: int,
    budget: RecallBudget,
    tenant: str,
    as_of: datetime | None,
    allowance: float,
) -> object:
    """Run one search in a daemon thread; enforce the budget and the exception contract."""

    box: dict[str, object] = {}
    done = threading.Event()

    def run() -> None:
        try:
            box["value"] = provider.search(
                query, limit=limit, budget=budget, tenant=tenant, as_of=as_of
            )
        except BaseException as exc:  # noqa: BLE001 - classified below
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=run, name="memfw-contract-search", daemon=True).start()
    if not done.wait(budget.timeout_seconds * allowance + 0.5):
        raise ContractError("search() ran past its budget")
    error = box.get("error")
    if error is not None:
        if not isinstance(error, Exception):
            raise ContractError("search() raised a BaseException that is not an Exception")
        raise _ProviderRaised(error)
    return box.get("value")


class _ProviderRaised(Exception):
    def __init__(self, error: Exception) -> None:
        super().__init__("provider raised")
        self.error = error


def _checked_items(provider: RecallProvider, result: object, limit: int) -> list[Candidate]:
    _require(
        isinstance(result, Sequence) and not isinstance(result, str | bytes),
        "search() must return a sequence",
    )
    assert isinstance(result, Sequence)
    try:
        items = list(result)
    except Exception as exc:
        raise ContractError("search() returned a sequence that cannot be iterated") from exc
    _require(len(items) <= limit, "search() returned more than limit")
    refs: set[tuple[str, str]] = set()
    for position, item in enumerate(items, start=1):
        _require(type(item) is Candidate, "search() must return plain Candidate values")
        try:
            validate_candidate(item)
        except ValueError as exc:
            raise ContractError(f"candidate breaks the contract: {exc}") from exc
        _require(item.provider == provider.name, "candidate names another provider")
        _require(item.rank == position, "ranks must be 1..n in list order")
        key = (item.ref_kind, item.ref)
        _require(key not in refs, "search() returned the same ref twice")
        refs.add(key)
    return items


def check_recall_provider(
    provider: RecallProvider,
    *,
    queries: Sequence[str],
    limit: int = 5,
    tenant: str = "",
    as_of: datetime | None = None,
    budget: RecallBudget | None = None,
    require_available: bool = True,
    vault: Path | None = None,
    vault_exclude: Sequence[str] = (),
    tenant_of: Callable[[Candidate], str | None] | None = None,
    error_queries: Sequence[str] = (),
    budget_allowance: float = 2.0,
) -> dict[str, list[Candidate]]:
    """Check a recall provider and return its candidates per query.

    Rules:

    - candidates are plain ``Candidate`` values that pass
      ``validate_candidate`` (no subclass, no smuggled ``extra`` or ``ref``),
      name this provider, carry ranks 1..n in order, no ref twice, at most
      ``limit``;
    - a repeated call gives the same refs in the same order;
    - each call returns within ``budget.timeout_seconds * budget_allowance``
      (+0.5 s);
    - errors are ``Exception`` subclasses, never a bare ``BaseException``
      such as ``SystemExit``; queries in ``error_queries`` may raise, other
      queries must not;
    - with ``tenant_of``, every returned ref belongs to ``tenant`` (``None``
      means the ref is unknown, which also fails);
    - with ``vault``, the vault files (minus ``vault_exclude``) are
      byte-identical afterwards.
    """

    availability = _check_common(provider, RecallProvider, "recall provider")
    if not availability.available:
        _require(not require_available, f"provider unavailable: {availability.reason}")
        return {}
    before = tree_digest(vault, exclude=vault_exclude) if vault is not None else None
    chosen = budget or RecallBudget()
    seen: dict[str, list[Candidate]] = {}

    def search(query: str) -> object:
        return _timed_search(
            provider,
            query,
            limit=limit,
            budget=chosen,
            tenant=tenant,
            as_of=as_of,
            allowance=budget_allowance,
        )

    for query in queries:
        runs = []
        for _ in range(2):
            try:
                result = search(query)
            except _ProviderRaised as raised:
                raise ContractError("search() raised on an ordinary query") from raised.error
            items = _checked_items(provider, result, limit)
            if tenant_of is not None:
                for item in items:
                    _require(tenant_of(item) == tenant, "search() returned another tenant's ref")
            runs.append(items)
        _require(
            [(c.ref_kind, c.ref) for c in runs[0]] == [(c.ref_kind, c.ref) for c in runs[1]],
            "search() is not deterministic",
        )
        seen[query] = runs[0]
    for query in error_queries:
        try:
            _checked_items(provider, search(query), limit)
        except _ProviderRaised:
            pass
    if before is not None and vault is not None:
        _require(tree_digest(vault, exclude=vault_exclude) == before, "search() changed the vault")
    return seen


def check_projector(
    projector: Projector,
    *,
    vault: Path,
    out: Path,
    policy: ProjectionPolicy,
    vault_exclude: Sequence[str] = (),
) -> Manifest:
    """Read-only on the vault; manifest valid; two exports agree."""

    availability = _check_common(projector, Projector, "projector")
    _require(availability.available, f"projector unavailable: {availability.reason}")
    before = tree_digest(vault, exclude=vault_exclude)
    first = projector.export(vault, out / "first", policy)
    second = projector.export(vault, out / "second", policy)
    _require(tree_digest(vault, exclude=vault_exclude) == before, "export() changed the vault")
    for manifest, target in ((first, out / "first"), (second, out / "second")):
        _require(isinstance(manifest, Manifest), "export() must return a Manifest")
        _require(manifest.projector == projector.name, "manifest names another projector")
        _require(manifest.policy_version == policy.version, "manifest policy version differs")
        for name in manifest.files:
            _require((target / name).is_file(), "manifest lists a file that was not written")
    _require(first.content_sha256 == second.content_sha256, "export is not reproducible")
    _require(first.projected == second.projected, "export counts differ between runs")
    return first


def check_ingestor(
    ingestor: Ingestor,
    *,
    vault: Path,
    items: Sequence[IngestItem],
    proposal_id: str = "contract-proposal",
    source: str = "contract-test",
) -> StageResult:
    """Only ``_candidates/`` changes; proposal id and source required; idempotent."""

    availability = _check_common(ingestor, Ingestor, "ingestor")
    _require(availability.available, f"ingestor unavailable: {availability.reason}")
    _require(bool(items), "check_ingestor needs at least one item")
    before = tree_digest(vault, exclude=(CANDIDATES_DIR,))
    staged_before = _staged(vault)
    for bad_proposal, bad_source in (("", source), (proposal_id, "")):
        try:
            ingestor.stage(vault, items, proposal_id=bad_proposal, source=bad_source)
        except (ValueError, TypeError):
            pass
        else:
            raise ContractError("stage() accepted a missing proposal_id or source")
    _require(_staged(vault) == staged_before, "a refused stage() still wrote candidates")
    first = ingestor.stage(vault, items, proposal_id=proposal_id, source=source)
    _require(isinstance(first, StageResult), "stage() must return a StageResult")
    snapshot = tree_digest(vault / CANDIDATES_DIR)
    second = ingestor.stage(vault, items, proposal_id=proposal_id, source=source)
    _require(second.staged == (), "second stage() of the same items wrote new candidates")
    _require(
        sorted(second.candidate_ids) == sorted(first.candidate_ids),
        "stage() is not idempotent: candidate ids changed",
    )
    _require(tree_digest(vault / CANDIDATES_DIR) == snapshot, "second stage() changed files")
    _require(
        tree_digest(vault, exclude=(CANDIDATES_DIR,)) == before,
        "stage() wrote outside _candidates/",
    )
    return first


def check_deriver(
    deriver: Deriver,
    *,
    vault: Path,
    request: DeriveRequest,
    vault_exclude: Sequence[str] = (),
) -> DerivedView:
    """Read-only on the vault; cites only requested memories; capped claims."""

    availability = _check_common(deriver, Deriver, "deriver")
    _require(availability.available, f"deriver unavailable: {availability.reason}")
    before = tree_digest(vault, exclude=vault_exclude)
    view = deriver.derive(vault, request)
    _require(tree_digest(vault, exclude=vault_exclude) == before, "derive() changed the vault")
    _require(isinstance(view, DerivedView), "derive() must return a DerivedView")
    _require(view.deriver == deriver.name, "view names another deriver")
    _require(view.kind == request.kind, "view kind differs from the request")
    _require(view.read_only, "derived views are read-only")
    _require(
        set(view.cited_ids()) <= set(request.memory_ids),
        "view cites a memory that was not in the request",
    )
    return view


#: The memory id rule ``Repository`` enforces (``memfw.core.repository._ID_RE``).
_ID_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def check_clock(clock: Clock, *, samples: int = 32) -> list[datetime]:
    """``now()`` is timezone-aware and never goes backwards; returns the samples."""

    _require(samples >= 2, "check_clock needs at least two samples")
    _require(isinstance(clock, Clock), "clock does not implement Clock")
    seen: list[datetime] = []
    for _ in range(samples):
        value = clock.now()
        _require(isinstance(value, datetime), "now() must return a datetime")
        _require(
            value.tzinfo is not None and value.utcoffset() is not None,
            "now() must be timezone-aware",
        )
        _require(not seen or value >= seen[-1], "now() went backwards")
        seen.append(value)
    return seen


def check_id_gen(id_gen: IdGen, *, samples: int = 1000) -> list[str]:
    """``new_id()`` returns distinct ids valid as memory ids; returns the samples."""

    _require(samples >= 2, "check_id_gen needs at least two samples")
    _require(isinstance(id_gen, IdGen), "id generator does not implement IdGen")
    ids: list[str] = []
    for _ in range(samples):
        value = id_gen.new_id()
        _require(isinstance(value, str), "new_id() must return a str")
        _require(bool(_ID_RULE.fullmatch(value)), f"new_id() returned an invalid id: {value!r}")
        ids.append(value)
    _require(len(set(ids)) == len(ids), "new_id() repeated an id")
    return ids
def check_context_pack(
    pack: object,
    *,
    slots: Sequence[str] | None = None,
    max_tokens: int | None = None,
    forbidden: Sequence[str] = (),
) -> ContextPack:
    """A well-formed pack whose version key carries no content.

    - ``pack`` is exactly a ``ContextPack`` and its ``pack_version`` is the
      recomputed digest (so a field changed after construction is caught);
    - ``slots``, when given, is the expected slot order;
    - per slot: ``overflow == len(truncated_ids)``; at most one entry is both
      carried and truncated, and it is the slot's last entry (the order of
      ``truncated_ids`` is the producer's, see ``ContextSlot``); with
      ``max_tokens``, ``used_tokens_est`` stays within it;
    - no string in the version payload contains an entry's text or title
      (8 characters or more; a title or text equal to an id, slot name or the
      policy name is not content) or any ``forbidden`` string (e.g. the query
      or a private body the caller knows about). A ``forbidden`` string
      shorter than 8 characters is a usage error (``ValueError``): it would
      match ids and hashes by chance;
    - ``as_dict()`` is JSON serialisable.
    """

    short = [text for text in forbidden if not isinstance(text, str) or len(text) < 8]
    if short:
        raise ValueError("forbidden strings must be at least 8 characters")
    _require(type(pack) is ContextPack, "context pack must be exactly memfw.ports.ContextPack")
    assert isinstance(pack, ContextPack)
    _require(pack.generated_at.tzinfo is not None, "generated_at must include a timezone")
    _require(
        pack.pack_version == compute_pack_version(pack.slots, pack.policy),
        "pack_version does not match the pack contents",
    )
    names = [slot.name for slot in pack.slots]
    if slots is not None:
        _require(names == list(slots), f"slots are {names}, expected {list(slots)}")
    _require(len(set(names)) == len(names), "slot names must be unique")
    for slot in pack.slots:
        _require(
            slot.overflow == len(slot.truncated_ids),
            f"slot {slot.name}: overflow differs from truncated_ids",
        )
        carried = [entry.id for entry in slot.entries]
        partial = [memory_id for memory_id in slot.truncated_ids if memory_id in carried]
        _require(len(partial) <= 1, f"slot {slot.name}: more than one partial entry")
        if partial:
            _require(
                carried[-1] == partial[0], f"slot {slot.name}: partial entry is not the last one"
            )
        if max_tokens is not None:
            _require(
                slot.used_tokens_est <= max_tokens,
                f"slot {slot.name}: used_tokens_est exceeds {max_tokens}",
            )
    leaves = _string_leaves(json.loads(pack_version_payload(pack.slots, pack.policy)))
    structural = {pack.policy, *names}
    structural.update(entry.id for slot in pack.slots for entry in slot.entries)
    structural.update(memory_id for slot in pack.slots for memory_id in slot.truncated_ids)
    content = [entry.text for slot in pack.slots for entry in slot.entries]
    content += [entry.title for slot in pack.slots for entry in slot.entries]
    candidates = [
        text
        for text in content
        if isinstance(text, str) and len(text) >= 8 and text not in structural
    ]
    for text in [*candidates, *forbidden]:
        _require(not any(text in leaf for leaf in leaves), "pack_version input contains content")
    try:
        json.dumps(pack.as_dict())
    except (TypeError, ValueError) as error:
        raise ContractError(f"as_dict() is not JSON serialisable: {error}") from error
    return pack


def _string_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


__all__ = [
    "ContractError",
    "check_clock",
    "check_context_pack",
    "check_deriver",
    "check_id_gen",
    "check_ingestor",
    "check_projector",
    "check_recall_provider",
    "tree_digest",
]
