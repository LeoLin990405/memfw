"""Share the unified conversation store with local agent apps, and process it.

* :mod:`.store`    -- the read interface (Protocol), output redaction, path guards
* :mod:`.adapter`  -- the one file that knows the store's schema and ingest
* :mod:`.policy`   -- per-app visibility and opt-out
* :mod:`.share`    -- read-only search / get / list / handoff
* :mod:`.worker`   -- the asynchronous, idempotent job runner
* :mod:`.backends` -- injectable model backends (local by default)
* :mod:`.schedule` -- the launchd template (printed, never installed)
"""

from __future__ import annotations

from .policy import OPERATOR, SharePolicy
from .service import open_share, open_worker
from .share import ConversationShare, render_markdown
from .store import ConversationError, ConversationSource, SessionInfo, Turn

__all__ = [
    "OPERATOR",
    "ConversationError",
    "ConversationShare",
    "ConversationSource",
    "SessionInfo",
    "SharePolicy",
    "Turn",
    "open_share",
    "open_worker",
    "render_markdown",
]
