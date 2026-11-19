"""memfw-adapter-wikigen: a ``Deriver`` that writes one cited wiki page per job.

Off until the operator enables it; meant to run on a worker machine with a
local model (never on the storage host)::

    adapters:
      enabled: [wikigen]
      wikigen:
        backend: "memfw_adapter_wikigen.ollama:factory"
        backend_options: {model: "qwen2.5:7b-instruct"}

Input is a ``memfw-wiki-job/1`` package, output a ``memfw-wiki-page/1`` page
candidate. See :mod:`memfw_adapter_wikigen.deriver` for the steps, the
identity check, the privacy rule and the refusal codes.
"""

from __future__ import annotations

from .deriver import (
    KINDS,
    NAME,
    SPEC,
    TASK,
    VIEW_FORMAT,
    Generation,
    RunBudget,
    Settings,
    WikigenDeriver,
    WikigenError,
    __version__,
    candidates,
    factory,
    read_job,
)
from .prompt import PROMPT_VERSION

__all__ = [
    "KINDS",
    "NAME",
    "PROMPT_VERSION",
    "SPEC",
    "TASK",
    "VIEW_FORMAT",
    "Generation",
    "RunBudget",
    "Settings",
    "WikigenDeriver",
    "WikigenError",
    "__version__",
    "candidates",
    "factory",
    "read_job",
]
