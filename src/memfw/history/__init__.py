"""Read-only transcript layer: index, search and read agent session logs.

Raw logs stay where they are. The derived index keeps locations, lineage ids,
timestamps and redacted snippets only. See ``docs/history.md``.
"""

from .index import (
    DEFAULT_DB_NAME,
    HistoryError,
    SourceRoot,
    TranscriptIndex,
    default_db,
    make_ref,
    parse_source_option,
    sources_from_config,
    split_ref,
)
from .redact import has_secret, redact, scan

__all__ = [
    "DEFAULT_DB_NAME",
    "HistoryError",
    "SourceRoot",
    "TranscriptIndex",
    "default_db",
    "has_secret",
    "make_ref",
    "parse_source_option",
    "redact",
    "scan",
    "sources_from_config",
    "split_ref",
]
