"""``memfw dream skills``: skill candidates distilled from conversation history.

Procedures repeated across sessions (and agents) become SKILL.md drafts with
evidence, staged under ``_candidates/skills/``. Proposal-only: nothing is
installed or approved. See ``docs/dream.md``.
"""

from .distill import Procedure, VerifiedStep, distill
from .draft import Drafter, clean, load_drafter
from .episodes import Episode, Step, segment, step_signature
from .run import SKILLS_DIR, SkillCandidate, SkillOptions, SkillResult, run_skills

__all__ = [
    "SKILLS_DIR",
    "Drafter",
    "Episode",
    "Procedure",
    "SkillCandidate",
    "SkillOptions",
    "SkillResult",
    "Step",
    "VerifiedStep",
    "clean",
    "distill",
    "load_drafter",
    "run_skills",
    "segment",
    "step_signature",
]
