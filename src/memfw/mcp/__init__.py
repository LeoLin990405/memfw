"""Generic local MCP server for a memfw vault.

The server is intentionally deployment-agnostic: it operates only on a local memfw
vault directory, with no SSH, no remote host, and no external search CLI.
"""

from .server import Gateway, main, serve

__all__ = ["Gateway", "main", "serve"]
