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
from .relations import RelationTable, RelationType, builtin_relation_table, load_relation_table
from .relations_lint import lint_relations
from .taxonomy import broader_of, lint_taxonomy, narrower_index

__all__ = [
    "OntologyError",
    "RelationTable",
    "RelationType",
    "broader_of",
    "builtin_relation_table",
    "import_seed",
    "lint_concepts",
    "lint_relations",
    "lint_taxonomy",
    "load_relation_table",
    "load_concepts",
    "matching_memories",
    "merge_concepts",
    "narrower_index",
    "project_aliases",
    "propose_concept",
]
