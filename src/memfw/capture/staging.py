"""Staged memory candidate writer: re-exported from :mod:`memfw.storage.candidate_staging`.

The writer moved to storage in A2b so the OKF importer shares it without
depending on capture. These are the same objects, not copies.
"""

from __future__ import annotations

from ..storage.candidate_staging import (
    CANDIDATE_DIR,
    MAX_CANDIDATE_BYTES,
    PublishedCandidate,
    body_content_hash,
    candidate_document,
    open_candidate_directory,
    publish_candidate,
    single_line,
    unpublish_candidate,
)

__all__ = [
    "CANDIDATE_DIR",
    "PublishedCandidate",
    "unpublish_candidate",
    "MAX_CANDIDATE_BYTES",
    "body_content_hash",
    "candidate_document",
    "open_candidate_directory",
    "publish_candidate",
    "single_line",
]
