"""memfw-pipeline: the Stage contract and the runner that enforces it.

See ``docs/pipeline.md`` in the memfw repository. Standard library only at run
time, plus the memfw ports (``Clock``, the name rule).
"""

from __future__ import annotations

from .config import PipelineConfig, StageSpec, load_config, load_stage
from .contract import (
    CODE_RE,
    ITEMS,
    BuildResult,
    DirectoryStage,
    Freshness,
    InputUnavailable,
    PostPublishStep,
    Prepared,
    SetupError,
    Stage,
    StageContext,
    StageError,
    StepResult,
    VerifyFailed,
    digest_of,
)
from .runner import (
    EXIT_BUILD,
    EXIT_BUSY,
    EXIT_INPUTS,
    EXIT_OK,
    EXIT_POST_PUBLISH,
    EXIT_SETUP,
    EXIT_SHRINK,
    SHRINK_FLOOR,
    RunOptions,
    plan_stage,
    run_stage,
    stage_status,
)

__version__ = "0.1.0"

__all__ = [
    "CODE_RE",
    "EXIT_BUILD",
    "EXIT_BUSY",
    "EXIT_INPUTS",
    "EXIT_OK",
    "EXIT_POST_PUBLISH",
    "EXIT_SETUP",
    "EXIT_SHRINK",
    "ITEMS",
    "SHRINK_FLOOR",
    "BuildResult",
    "DirectoryStage",
    "Freshness",
    "InputUnavailable",
    "PipelineConfig",
    "PostPublishStep",
    "Prepared",
    "RunOptions",
    "SetupError",
    "Stage",
    "StageContext",
    "StageError",
    "StageSpec",
    "StepResult",
    "VerifyFailed",
    "__version__",
    "digest_of",
    "load_config",
    "load_stage",
    "plan_stage",
    "run_stage",
    "stage_status",
]
