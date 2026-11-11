"""``memfw dream``: verify-first offline consolidation of a memfw vault.

The dream reads memories and the transcript index (``memfw history``) and
stages candidates under ``_candidates/dream/<dream_id>/<oid>.md``. It never
writes the authoritative vault; a human reviews every candidate. See
``docs/dream.md``.
"""

from .model import Candidate, EvidenceRef, candidate_filename, concept_key
from .run import DreamOptions, DreamResult, load_judge, run_dream
from .verify import cited, classify, verify

__all__ = [
    "Candidate",
    "DreamOptions",
    "DreamResult",
    "EvidenceRef",
    "candidate_filename",
    "cited",
    "classify",
    "concept_key",
    "load_judge",
    "run_dream",
    "verify",
]
