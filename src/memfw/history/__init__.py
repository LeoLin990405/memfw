"""Read-only transcript layer: index, search and read agent session logs.

Raw logs stay where they are. The derived index keeps locations, lineage ids,
timestamps and redacted snippets only. See ``docs/history.md``.
"""

from .index import (
    DEFAULT_DB_NAME,
    HistoryError,
    HistoryLimits,
    SourceRoot,
    TranscriptIndex,
    default_db,
    file_id,
    make_ref,
    parse_source_option,
    sources_from_config,
    split_ref,
)
from .redact import has_secret, redact, redact_path, scan, secret_continues

__all__ = [
    "DEFAULT_DB_NAME",
    "HistoryError",
    "HistoryLimits",
    "SourceRoot",
    "TranscriptIndex",
    "default_db",
    "file_id",
    "has_secret",
    "make_ref",
    "parse_source_option",
    "redact",
    "redact_path",
    "scan",
    "secret_continues",
    "sources_from_config",
    "split_ref",
]
