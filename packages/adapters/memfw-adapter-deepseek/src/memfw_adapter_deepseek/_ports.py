"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports.models``: the same
classes the conversation worker checks (``BackendBudgetExhausted`` is caught by
type). Without memfw (the standalone wheels) they come from the
``memfw-ports`` wheel's ``memfw_ports.models``. Either way only the L0 port
module is imported, nothing else of memfw.
"""

from __future__ import annotations

try:
    from memfw.ports.models import (
        TAG_PROMPT_VERSION,
        TAG_TASKS,
        BackendBudgetExhausted,
        BackendUnavailable,
        ModelReply,
        render_tag_prompt,
    )
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.models import (  # type: ignore[no-redef]
        TAG_PROMPT_VERSION,
        TAG_TASKS,
        BackendBudgetExhausted,
        BackendUnavailable,
        ModelReply,
        render_tag_prompt,
    )

__all__ = [
    "TAG_PROMPT_VERSION",
    "TAG_TASKS",
    "BackendBudgetExhausted",
    "BackendUnavailable",
    "ModelReply",
    "render_tag_prompt",
]
