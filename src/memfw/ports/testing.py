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

import copy
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from .claims import POLARITIES, ClaimStager, ClaimVerifier, StagedClaim
from .clock import Clock, IdGen
from .context import (
    ContextEntry,
    ContextPack,
    ContextSlot,
    ContextSlotProvider,
    SlotRequest,
    compute_pack_version,
    pack_version_payload,
)
from .models import TAG_PROMPT_VERSION, TAG_TASKS, ModelReply, parse_tagged
from .projection_store import (
    MAX_QUERY_LIMIT,
    Direction,
    Edge,
    EdgeScope,
    ProjectionStore,
    RebuildResult,
    edge_set_version,
    select,
)
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
from .usage import UsageSignal, UsageSink


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
    content += [
        entry.title if isinstance(entry, ContextEntry) else entry.label
        for slot in pack.slots
        for entry in slot.entries
    ]
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


def check_context_slot_provider(
    provider: object,
    *,
    request: SlotRequest,
    max_tokens: int | None = None,
    forbidden: Sequence[str] = (),
    vault: Path | None = None,
    vault_exclude: Sequence[str] = (),
    allow_degraded: bool = False,
) -> ContextSlot:
    """A slot provider builds a well-formed, stable slot under its own name.

    - ``provider`` implements ``ContextSlotProvider`` and its ``name`` is a
      valid slot name;
    - ``build(request)`` returns exactly a ``ContextSlot`` named after the
      provider, which passes ``check_context_pack`` as a one-slot pack (with
      ``max_tokens`` and ``forbidden``);
    - a second ``build`` gives the same version key (same ids, versions and
      hashes, same cut);
    - it is not ``degraded`` unless ``allow_degraded``;
    - with ``vault``, the vault tree is byte-identical afterwards (no writes).

    Returns the first slot.
    """

    _require(
        isinstance(provider, ContextSlotProvider), "provider does not implement ContextSlotProvider"
    )
    assert isinstance(provider, ContextSlotProvider)
    name = getattr(provider, "name", None)
    _require(isinstance(name, str) and bool(name), "provider name must be a non-empty string")
    before = tree_digest(vault, exclude=vault_exclude) if vault is not None else None
    first = provider.build(request)
    _require(type(first) is ContextSlot, "build() must return exactly memfw.ports.ContextSlot")
    _require(first.name == name, f"slot is named {first.name!r}, provider is {name!r}")
    if not allow_degraded:
        _require(first.degraded is None, f"slot {name} is degraded ({first.degraded})")
    pack = ContextPack.build((first,), policy=request.policy, generated_at=request.now)
    check_context_pack(pack, slots=[first.name], max_tokens=max_tokens, forbidden=forbidden)
    second = provider.build(request)
    _require(type(second) is ContextSlot, "build() must return exactly memfw.ports.ContextSlot")
    _require(
        compute_pack_version((second,), request.policy) == pack.pack_version,
        f"slot {name}: two builds give different version keys",
    )
    if vault is not None:
        _require(tree_digest(vault, exclude=vault_exclude) == before, "provider changed the vault")
    return first


def _string_leaves(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _string_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _string_leaves(item)]
    return []


def check_claim_verifier(verifier: ClaimVerifier, *, pairs: Sequence[tuple[str, str]]) -> list[str]:
    """``polarity()`` answers one of ``POLARITIES``, the same way every time."""

    _require(bool(pairs), "check_claim_verifier needs at least one (claim, text) pair")
    _require(isinstance(verifier, ClaimVerifier), "verifier does not implement ClaimVerifier")
    answers: list[str] = []
    for claim, text in pairs:
        first = verifier.polarity(claim, text)
        _require(first in POLARITIES, f"polarity() returned {first!r}, not one of {POLARITIES}")
        _require(verifier.polarity(claim, text) == first, "polarity() is not deterministic")
        answers.append(first)
    return answers


def check_claim_stager(
    stager: ClaimStager,
    *,
    vault: Path,
    claims: Sequence[StagedClaim],
    batch_id: str = "contract-claims",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, str]:
    """Only ``_candidates/`` changes; restaging a batch id writes nothing and returns False.

    Returns the digest of ``_candidates/`` after the first staging.
    """

    _require(bool(claims), "check_claim_stager needs at least one claim")
    _require(isinstance(stager, ClaimStager), "stager does not implement ClaimStager")
    before = tree_digest(vault, exclude=(CANDIDATES_DIR,))
    staged_before = _staged(vault)
    report = {"kind": "contract"}
    first = stager.stage_claims(vault, batch_id, claims, report, created_at=created_at)
    _require(first is True, "first stage_claims() of a batch must return True")
    _require(_staged(vault) != staged_before, "stage_claims() staged nothing")
    snapshot = tree_digest(vault / CANDIDATES_DIR)
    second = stager.stage_claims(vault, batch_id, claims, report, created_at=created_at)
    _require(second is False, "restaging the same batch id must return False")
    _require(tree_digest(vault / CANDIDATES_DIR) == snapshot, "restaging a batch changed files")
    _require(
        tree_digest(vault, exclude=(CANDIDATES_DIR,)) == before,
        "stage_claims() wrote outside _candidates/",
    )
    return snapshot


def check_usage_sink(
    sink: UsageSink,
    *,
    signals: Sequence[UsageSignal],
    vault: Path | None = None,
    vault_exclude: Sequence[str] = (),
    stored: Callable[[], Sequence[Mapping[str, object]]] | None = None,
) -> None:
    """``record()`` takes signals, returns ``None``, refuses non-signals, never writes the vault.

    - each of ``signals`` is recorded twice (a repeated signal is allowed);
    - ``record()`` of anything but a ``UsageSignal`` raises ``TypeError``;
    - with ``vault``, its files (``tree_digest``, minus ``vault_exclude``:
      the sink's own store and lock files) are unchanged;
    - with ``stored`` (a sink that keeps signals: returns its rows), every id
      of every signal appears, and every string in the rows is one of the
      signals' ids, session refs, apps or ISO times: no other text is kept.
    """

    _require(bool(signals), "check_usage_sink needs at least one signal")
    _require(isinstance(sink, UsageSink), "sink does not implement UsageSink")
    _require(
        all(type(signal) is UsageSignal for signal in signals),
        "signals must be exactly memfw.ports.UsageSignal",
    )
    before = tree_digest(vault, exclude=vault_exclude) if vault is not None else None
    for signal in [*signals, *signals]:
        returned: object = sink.record(signal)  # type: ignore[func-returns-value]
        _require(returned is None, "record() must return None")
    for bogus in ("m-1", {"memory_ids": ["m-1"]}, None):
        try:
            sink.record(bogus)  # type: ignore[arg-type]
        except TypeError:
            continue
        raise ContractError(f"record() accepted {type(bogus).__name__}, not a UsageSignal")
    if vault is not None:
        _require(tree_digest(vault, exclude=vault_exclude) == before, "sink changed the vault")
    if stored is None:
        return
    rows = list(stored())
    allowed: set[str] = set()
    for signal in signals:
        allowed.update(signal.memory_ids)
        allowed.update({signal.session_ref, signal.app, signal.at.isoformat()})
    kept = {str(row.get("memory_id")) for row in rows}
    missing = sorted({i for signal in signals for i in signal.memory_ids} - kept)
    _require(not missing, f"stored rows miss ids {missing}")
    for row in rows:
        for leaf in _string_leaves(dict(row)):
            _require(leaf in allowed, f"stored rows keep text that is not a signal field: {leaf!r}")


#: Inputs for :func:`check_model_backend`, one per task (redacted-looking text).
MODEL_SAMPLES: dict[str, dict[str, object]] = {
    "chunk_summary": {
        "turns": [
            {"role": "user", "text": "Fix the flaky retry test in the sync worker."},
            {"role": "assistant", "text": "Fixed the race; the tests pass now."},
        ]
    },
    "session_summary": {
        "first_user": "Fix the flaky retry test in the sync worker.",
        "chunks": ["The user asked to fix a flaky test. The race was fixed."],
    },
    "memory_claims": {"summary": "The race in the sync worker retry test was fixed."},
}
PROMPT_VERSIONS = ("conv-v1", TAG_PROMPT_VERSION)


def check_model_backend(
    backend: object,
    *,
    samples: dict[str, dict[str, object]] | None = None,
    max_tokens: int = 256,
) -> list[ModelReply]:
    """A ``ModelBackend`` answers every task it accepts with a JSON object reply.

    ``name`` is a non-empty string and ``local`` a bool; ``prompt_version``
    (default ``conv-v1``) is a known version and ``tasks`` (default: every
    sample task) a tuple of task names. Each accepted task returns a
    :class:`~memfw.ports.models.ModelReply` with a string ``text`` that is
    one JSON object, non-negative token counts, and leaves its input
    unchanged. A ``conv-v2`` backend's tag tasks match the tag schema. A task
    outside ``tasks`` raises instead of answering.
    """

    samples = MODEL_SAMPLES if samples is None else samples
    name = getattr(backend, "name", None)
    _require(isinstance(name, str) and bool(name), "backend.name must be a non-empty string")
    _require(isinstance(getattr(backend, "local", None), bool), "backend.local must be a bool")
    run = getattr(backend, "run", None)
    _require(callable(run), "backend.run must be callable")
    version = getattr(backend, "prompt_version", "conv-v1")
    _require(version in PROMPT_VERSIONS, f"unknown prompt_version {version!r}")
    tasks = getattr(backend, "tasks", tuple(samples))
    _require(
        isinstance(tasks, tuple) and all(isinstance(task, str) for task in tasks),
        "backend.tasks must be a tuple of task names",
    )
    replies: list[ModelReply] = []
    for task, data in samples.items():
        before = copy.deepcopy(data)
        if task not in tasks:
            try:
                run(task, copy.deepcopy(data), max_tokens=max_tokens)  # type: ignore[misc]
            except Exception:
                continue
            raise ContractError(f"backend answered task {task} it does not accept")
        reply = run(task, data, max_tokens=max_tokens)  # type: ignore[misc]
        _require(data == before, f"run({task}) changed its input")
        _require(isinstance(reply, ModelReply), f"run({task}) did not return a ModelReply")
        _require(isinstance(reply.text, str), f"run({task}) reply text is not a string")
        for count in (reply.tokens_in, reply.tokens_out):
            _require(
                isinstance(count, int) and not isinstance(count, bool) and count >= 0,
                f"run({task}) token counts must be integers >= 0",
            )
        try:
            value = json.loads(reply.text)
        except ValueError as exc:
            raise ContractError(f"run({task}) reply is not JSON") from exc
        _require(isinstance(value, dict), f"run({task}) reply is not a JSON object")
        if version == TAG_PROMPT_VERSION and task in TAG_TASKS:
            _require(parse_tagged(value) is not None, f"run({task}) reply breaks the tag schema")
        replies.append(reply)
    _require(bool(replies), "backend accepted none of the sample tasks")
    return replies


__all__ = [
    "MODEL_SAMPLES",
    "ContractError",
    "check_claim_stager",
    "check_claim_verifier",
    "check_clock",
    "check_context_pack",
    "check_context_slot_provider",
    "check_deriver",
    "check_id_gen",
    "check_ingestor",
    "check_model_backend",
    "check_projector",
    "check_recall_provider",
    "check_usage_sink",
    "tree_digest",
]


# --- ProjectionStore (K3) ---------------------------------------------------------


def _sample_edges() -> tuple[Edge, ...]:
    def mm(relation: str, source: str, target: str, level: str = "public") -> Edge:
        return Edge(
            relation,
            source,
            target,
            "memory",
            "memory",
            relation,
            source_tenant="",
            target_tenant="",
            source_privacy="public",
            target_privacy=level,
        )

    def about(source: str, level: str = "public", tenant: str = "") -> Edge:
        return Edge(
            "about",
            source,
            "concept:backup",
            "memory",
            "concept",
            "about",
            source_tenant=tenant,
            source_privacy=level,
            epistemic_max="inference",
        )

    def broader(source: str, target: str, level: str | None = None) -> Edge:
        return Edge(
            "broader", source, target, "concept", "concept", "broader", source_privacy=level
        )

    return (
        about("m-pub"),
        about("m-shared", "shared"),
        about("m-priv", "local-private"),
        about("m-odd", "unknown"),
        about("m-other", tenant="other"),
        broader("concept:backup", "concept:ops"),
        broader("concept:ops", "concept:backup"),
        broader("concept:secret", "concept:ops", "local-private"),
        Edge(
            "superseded_by",
            "m-old",
            "m-pub",
            "memory",
            "memory",
            "superseded_by",
            source_tenant="",
            target_tenant="",
            source_privacy="public",
            target_privacy="public",
            valid_to="2026-01-01T00:00:00+00:00",
        ),
        mm("evidence", "m-pub", "m-priv", "local-private"),
        mm("related", "m-pub", "m-shared", "shared"),
        Edge(
            "observed_in",
            "m-pub",
            "att:photo.png#p1",
            "memory",
            "attachment",
            "evidence",
            source_tenant="",
            source_privacy="public",
        ),
    )


#: Edges :func:`check_projection_store` rebuilds a store with (every visibility case).
SAMPLE_EDGES: tuple[Edge, ...] = _sample_edges()
#: Scopes it queries with.
SAMPLE_SCOPES: tuple[EdgeScope, ...] = (
    EdgeScope("", frozenset({"public"})),
    EdgeScope("", frozenset({"public", "shared"})),
    EdgeScope("", frozenset({"public", "shared", "local-private"})),
    EdgeScope("", frozenset()),
    EdgeScope("other", frozenset({"public", "shared", "local-private"})),
)


def _nodes(edges: Sequence[Edge]) -> list[str]:
    found = {edge.source for edge in edges} | {edge.target for edge in edges}
    return sorted(found | {"m-missing"})


def _check_answers(store: ProjectionStore, edges: Sequence[Edge]) -> None:
    for scope in SAMPLE_SCOPES:
        for node in _nodes(edges):
            directions: tuple[Direction, ...] = ("out", "in")
            for direction in directions:
                for relations in (None, ("about",), ("broader", "evidence")):
                    names = frozenset(relations) if relations is not None else None
                    want = select(
                        edges, scope, node=node, direction=direction, relations=names, limit=100
                    )
                    got = store.query(
                        scope, node=node, direction=direction, relations=relations, limit=100
                    )
                    _require(
                        isinstance(got, tuple) and all(type(item) is Edge for item in got),
                        "query() must return a tuple of Edge",
                    )
                    call = (
                        f"query({scope.tenant!r}, {sorted(scope.clearance)}, {node}, {direction})"
                    )
                    _require(
                        got == want,
                        f"{call} {relations} gave {[e.key for e in got]}, not "
                        f"{[e.key for e in want]}",
                    )


def check_projection_store(
    store: ProjectionStore,
    *,
    vault: Path | None = None,
    vault_exclude: Sequence[str] = (),
    edges: Sequence[Edge] = SAMPLE_EDGES,
) -> RebuildResult:
    """A fresh ``store`` follows the ``ProjectionStore`` contract.

    - ``available()`` is ok, ``name`` is set, ``version()`` is ``None`` before
      the first rebuild;
    - ``rebuild(edges)`` returns the count and :func:`edge_set_version`, and
      ``version()`` agrees; rebuilding the same edges in reverse order gives
      the same version;
    - every ``query`` (each node, both directions, with and without a relation
      filter, under :data:`SAMPLE_SCOPES`) returns exactly what
      :func:`memfw.ports.projection_store.select` returns: tenant and
      clearance filtered (a private or ``unknown`` endpoint hides the edge,
      another tenant never shows), sorted by key;
    - ``limit`` is respected and out-of-range limits, a non-``EdgeScope``
      scope and a bad direction are refused;
    - a duplicate key or a non-``Edge`` raises and keeps the old content;
    - rebuilding with fewer edges deletes the others (and changes the version);
    - with ``vault``, its files (minus ``vault_exclude``) are unchanged.
    """

    _check_common(store, ProjectionStore, "store")
    _require(store.version() is None, "version() must be None before the first rebuild")
    before = tree_digest(vault, exclude=vault_exclude) if vault is not None else None
    edges = list(edges)
    _require(len(edges) >= 2, "check_projection_store needs at least two edges")
    result = store.rebuild(edges)
    _require(isinstance(result, RebuildResult), "rebuild() must return RebuildResult")
    expected = edge_set_version(edges)
    _require(result.edges == len(edges), "rebuild() must count every edge")
    _require(result.version == expected, "rebuild() version must be edge_set_version(edges)")
    _require(store.version() == expected, "version() must match the last rebuild")
    again = store.rebuild(list(reversed(edges)))
    _require(again.version == expected, "the same edges in another order changed the version")
    _check_answers(store, edges)

    scope = EdgeScope("", frozenset({"public", "shared", "local-private"}))
    hub = max(_nodes(edges), key=lambda node: (sum(e.source == node for e in edges), node))
    every = store.query(scope, node=hub, direction="out", limit=MAX_QUERY_LIMIT)
    if len(every) >= 2:
        one = store.query(scope, node=hub, direction="out", limit=1)
        _require(one == every[:1], "limit=1 must return the first edge by key")
    for bad_limit in (0, MAX_QUERY_LIMIT + 1):
        try:
            store.query(scope, node=hub, limit=bad_limit)
        except ValueError:
            continue
        raise ContractError(f"query() accepted limit={bad_limit}")
    bad_calls: list[Callable[[], object]] = [
        lambda: store.query(None, node=hub),  # type: ignore[arg-type]
        lambda: store.query(scope, node=hub, direction="both"),  # type: ignore[arg-type]
    ]
    for call in bad_calls:
        try:
            call()
        except (TypeError, ValueError):
            continue
        raise ContractError("query() accepted a bad scope or direction")

    for bogus in ([edges[0], edges[0]], [edges[0], "not an edge"]):
        try:
            store.rebuild(bogus)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            _require(store.version() == expected, "a refused rebuild changed the content")
            continue
        raise ContractError("rebuild() accepted a duplicate key or a non-Edge")
    _check_answers(store, edges)

    kept = edges[: len(edges) // 2]
    smaller = store.rebuild(kept)
    _require(smaller.edges == len(kept), "rebuild() must count every edge")
    _require(smaller.version != expected, "fewer edges must give another version")
    _check_answers(store, kept)
    for edge in edges[len(edges) // 2 :]:
        rows = store.query(scope, node=edge.source, direction="out", limit=MAX_QUERY_LIMIT)
        _require(edge.key not in {row.key for row in rows}, "rebuild() kept a removed edge")
    final = store.rebuild(edges)
    if vault is not None:
        _require(tree_digest(vault, exclude=vault_exclude) == before, "store changed the vault")
    return final
