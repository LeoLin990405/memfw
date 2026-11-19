"""The one place this package takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports``; without memfw (the
standalone wheels) from the ``memfw-ports`` wheel's ``memfw_ports``. Only L0
port modules are imported: the ``Clock`` port and the name rule.
"""

from __future__ import annotations

try:
    from memfw.ports.clock import Clock, FixedClock, SystemClock
    from memfw.ports.types import check_name
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.clock import (  # type: ignore[no-redef,assignment,import-not-found,unused-ignore]
        Clock,
        FixedClock,
        SystemClock,
    )
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,import-not-found,unused-ignore]
        check_name,
    )

__all__ = ["Clock", "FixedClock", "SystemClock", "check_name"]
