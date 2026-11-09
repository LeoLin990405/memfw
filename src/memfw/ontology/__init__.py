"""Concept anchors, alias normalisation, and read-path alias expansion."""

from .expand import matching_memories
from .model import (
    OntologyError,
    import_seed,
    lint_concepts,
    load_concepts,
    merge_concepts,
    project_aliases,
    propose_concept,
)

__all__ = [
    "OntologyError",
    "import_seed",
    "lint_concepts",
    "load_concepts",
    "matching_memories",
    "merge_concepts",
    "project_aliases",
    "propose_concept",
]
