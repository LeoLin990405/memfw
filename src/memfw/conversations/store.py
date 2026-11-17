"""The read interface the share and worker layers code against.

The unified conversation store itself (tables, ingest, readers) is owned by
:mod:`memfw.history.conversations`. Everything in this package reads it only
through :class:`ConversationSource`; :mod:`memfw.conversations.adapter` is the
one file that knows the store's schema, so a schema change is a one-file
change there.

Text that leaves this package always goes through :func:`safe_text` first:
the store is redacted at write time, and every output path redacts again
(defence in depth) and withholds a turn outright if a secret shape survives.

The definitions live in :mod:`memfw.history.transcripts` (architecture A2a:
handoff and dream read transcripts through it without importing this
package); this module re-exports them unchanged.
"""

from __future__ import annotations

from memfw.history.transcripts import (
    DATA_NOTICE,
    GOAL_SCAN_TURNS,
    SYNCED_MARKERS,
    WITHHELD,
    ConversationError,
    ConversationSource,
    SessionInfo,
    Turn,
    check_app,
    check_id,
    check_private_location,
    ensure_private_dir,
    ensure_private_file,
    goal_text,
    safe_text,
    session_fingerprint,
    ts_display,
    ts_key,
)

__all__ = [
    "DATA_NOTICE",
    "GOAL_SCAN_TURNS",
    "SYNCED_MARKERS",
    "WITHHELD",
    "ConversationError",
    "ConversationSource",
    "SessionInfo",
    "Turn",
    "check_app",
    "check_id",
    "check_private_location",
    "ensure_private_dir",
    "ensure_private_file",
    "goal_text",
    "safe_text",
    "session_fingerprint",
    "ts_display",
    "ts_key",
]
