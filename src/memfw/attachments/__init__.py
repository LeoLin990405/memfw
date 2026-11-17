"""Attachments (MM1a): originals stored as content-addressed blobs outside the vault.

``records`` is the record format (``attachments/<att-id>.md``), ``blobs`` the
blob store, ``store`` the :class:`~memfw.attachments.store.AttachmentStoreMixin`
the vault facade is composed with, and ``cli`` the ``memfw attach`` commands.
See ``docs/attachments.md``.
"""

from __future__ import annotations

from .blobs import BlobCheck, BlobLocationError, BlobStore, StagedBlob
from .records import (
    ATTACHMENTS_DIR,
    DEFAULT_ATTACHMENT_PRIVACY,
    AttachmentRecord,
    check_attachment_id,
    parse_record,
    render_record,
)
from .store import (
    BLOB_ROOT_KEY,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    MAX_BYTES_KEY,
    MAX_TOTAL_KEY,
    VAULT_ID_KEY,
    AttachmentProblem,
    AttachmentStoreMixin,
    AttachResult,
    committed_records,
    validate_attachment_config,
)

__all__ = [
    "ATTACHMENTS_DIR",
    "BLOB_ROOT_KEY",
    "DEFAULT_ATTACHMENT_PRIVACY",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "MAX_BYTES_KEY",
    "MAX_TOTAL_KEY",
    "VAULT_ID_KEY",
    "AttachResult",
    "AttachmentProblem",
    "AttachmentRecord",
    "AttachmentStoreMixin",
    "BlobCheck",
    "BlobLocationError",
    "BlobStore",
    "StagedBlob",
    "check_attachment_id",
    "committed_records",
    "parse_record",
    "render_record",
    "validate_attachment_config",
]
