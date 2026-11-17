"""The one place this adapter takes names from the memfw ports.

Inside a memfw install the names come from ``memfw.ports``; without memfw
(the standalone wheels) from the ``memfw-ports`` wheel. Either way only L0
port modules are imported, nothing else of memfw.
"""

from __future__ import annotations

import importlib
from typing import Any

try:
    from memfw.ports.privacy import PRIVACY_LEVELS, field_level
    from memfw.ports.registry import AdapterSpec
    from memfw.ports.types import Availability, Citation, DerivedView, DeriveRequest
except ModuleNotFoundError:  # the standalone memfw-ports wheel
    from memfw_ports.privacy import (  # type: ignore[no-redef,assignment,unused-ignore]
        PRIVACY_LEVELS,
        field_level,
    )
    from memfw_ports.registry import AdapterSpec  # type: ignore[no-redef,assignment,unused-ignore]
    from memfw_ports.types import (  # type: ignore[no-redef,assignment,unused-ignore]
        Availability,
        Citation,
        DerivedView,
        DeriveRequest,
    )


def _proxy_parser() -> Any:
    """MM2a's ``parse_proxy_import`` from the same ports this seam uses, or ``None``."""

    root = "memfw.ports" if AdapterSpec.__module__.startswith("memfw.") else "memfw_ports"
    try:
        module = importlib.import_module(root + ".proxies")
    except ImportError:
        return None
    return getattr(module, "parse_proxy_import", None)


#: ``None`` until the installed ports carry MM2a's proxy format.
parse_proxy_import = _proxy_parser()

__all__ = [
    "PRIVACY_LEVELS",
    "AdapterSpec",
    "Availability",
    "Citation",
    "DeriveRequest",
    "DerivedView",
    "field_level",
    "parse_proxy_import",
]
