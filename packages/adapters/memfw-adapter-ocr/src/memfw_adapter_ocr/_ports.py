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


def _proxies_module() -> Any:
    """MM2a's ``proxies`` module from the same ports this seam uses, or ``None``."""

    root = "memfw.ports" if AdapterSpec.__module__.startswith("memfw.") else "memfw_ports"
    try:
        return importlib.import_module(root + ".proxies")
    except ImportError:
        return None


_PROXIES = _proxies_module()
#: ``True`` when the installed ports carry MM2a's proxy format (``memfw.ports.proxies``).
HAS_PROXY_FORMAT = _PROXIES is not None
if _PROXIES is not None:
    PROXY_FORMAT: str = _PROXIES.PROXY_FORMAT
    PROXY_FORMAT_VERSION: int = _PROXIES.PROXY_FORMAT_VERSION
    MAX_SEGMENT_CHARS: int = _PROXIES.MAX_SEGMENT_CHARS
    parse_proxy_import: Any = _PROXIES.parse_proxy_import
else:  # ports before MM2a: the same values, mirrored from memfw.ports.proxies
    PROXY_FORMAT = "memfw.proxy"
    PROXY_FORMAT_VERSION = 1
    MAX_SEGMENT_CHARS = 65_536
    parse_proxy_import = None

__all__ = [
    "PRIVACY_LEVELS",
    "AdapterSpec",
    "Availability",
    "Citation",
    "DeriveRequest",
    "DerivedView",
    "field_level",
    "HAS_PROXY_FORMAT",
    "MAX_SEGMENT_CHARS",
    "PROXY_FORMAT",
    "PROXY_FORMAT_VERSION",
    "parse_proxy_import",
]
