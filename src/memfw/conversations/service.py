"""Wiring: build the share and the worker from a vault root and its vault.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memfw.core.repository import Repository

from .adapter import SqliteStoreSource, checked_location, store_ingestor, store_path
from .backends import ModelBackend
from .policy import SharePolicy
from .share import ConversationShare
from .store import ConversationError, ConversationSource
from .worker import Budgets, ConversationWorker, Judge, WorkState, state_path


def read_config(root: Path | None) -> tuple[dict[str, Any], Path | None]:
    """``(config, vault_root)``; an absent root means an empty config."""

    if root is None:
        return {}, None
    repository = Repository(Path(root))
    if not repository.config_path.exists():
        raise ConversationError("vault root has no vault.yaml")
    return repository.read_config(), repository.root


def open_share(
    root: Path | None,
    caller: str,
    *,
    source: ConversationSource | None = None,
    store: Path | None = None,
) -> ConversationShare:
    config, vault = read_config(root)
    policy = SharePolicy.from_config(config)
    if source is None:
        db = checked_location(store, config, vault=vault) if store else None
        source = SqliteStoreSource(db or store_path(config, vault=vault))
    state = WorkState(state_path(config, vault=vault))
    return ConversationShare(source, policy, caller, summaries=state.session_summary)


def open_worker(
    root: Path | None,
    *,
    backend: ModelBackend,
    store: Path | None = None,
    state: Path | None = None,
    ingest: bool = True,
    judge: Judge | None = None,
    **budgets: Any,
) -> ConversationWorker:
    config, vault = read_config(root)
    policy = SharePolicy.from_config(config)
    db = checked_location(store, config, vault=vault) if store else store_path(config, vault=vault)
    work = WorkState(
        checked_location(state, config, vault=vault) if state else state_path(config, vault=vault)
    )
    # The store's own ``conversation_optout`` applies inside its ingest (and the
    # share policy reads the same list), so nothing is skipped here.
    ingestor = store_ingestor(config, db) if ingest else None
    return ConversationWorker(
        SqliteStoreSource(db),
        work,
        backend=backend,
        policy=policy,
        budgets=Budgets.from_config(config, **budgets),
        ingestor=ingestor,
        vault=vault,
        judge=judge,
    )
