"""Attachments: originals stored as content-addressed blobs outside the vault
(MM1a) or referenced under configured roots (MM1b).

``records`` is the record format (``attachments/<att-id>.md``), ``blobs`` the
blob store, ``locations`` the roots and path safety of referenced originals,
``store`` (add, read, verify) and ``lifecycle`` (detach, relink, purge) the
mixins the vault facade is composed with, ``scan`` the dry-run inventory and
``cli`` the ``memfw attach`` commands. See ``docs/attachments.md``.
"""

from __future__ import annotations

from .blobs import BlobCheck, BlobLocationError, BlobStore, StagedBlob
from .lifecycle import AttachmentLifecycleMixin, OrphanPlan, PurgePlan, PurgeResult
from .locations import ROOTS_KEY, Location, LocationError
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
    AttachmentState,
    AttachmentStoreMixin,
    AttachResult,
    attachment_states,
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
    "ROOTS_KEY",
    "VAULT_ID_KEY",
    "AttachResult",
    "AttachmentLifecycleMixin",
    "AttachmentProblem",
    "AttachmentRecord",
    "AttachmentState",
    "AttachmentStoreMixin",
    "BlobCheck",
    "BlobLocationError",
    "BlobStore",
    "Location",
    "LocationError",
    "OrphanPlan",
    "PurgePlan",
    "PurgeResult",
    "StagedBlob",
    "attachment_states",
    "check_attachment_id",
    "committed_records",
    "parse_record",
    "render_record",
    "validate_attachment_config",
]
