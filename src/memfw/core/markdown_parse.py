"""Parse one memory Markdown file into a :class:`MemoryDocument` (no I/O, no vault).

This is the narrow, dependency-light entry point for readers outside the
storage layer (for example the recall projection), which need the document
model and nothing else from the repository.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from .errors import IntegrityError
from .models import MemoryDocument

#: Fields whose default carries no information, so a rewrite omits them.
#: They were added on 2026-11-06; a file written after that date carries them
#: explicitly, even at the default. ``parse`` records which of these the source
#: file had so ``render`` writes them back and the bytes survive both
#: directions: legacy file -> new file, and new file -> rewritten file.
_OPTIONAL_DEFAULT_FIELDS = (
    "type",
    "tenant",
    "related",
    "valid_from",
    "valid_to",
    "superseded_by",
)


def parse_memory_markdown(markdown: str, *, expected_id: str | None = None) -> MemoryDocument:
    """Validate frontmatter + body; raise :class:`IntegrityError` on any defect.

    Duplicate keys are rejected, unknown keys from a newer writer are ignored
    (and listed in ``ignored_keys``), and ``expected_id`` must match ``id``.
    """

    markdown = markdown.replace("\r\n", "\n")
    if not markdown.startswith("---\n"):
        raise IntegrityError("Markdown is missing YAML frontmatter")

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def unique_mapping(
        loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise IntegrityError(f"Duplicate frontmatter key: {key}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    # The frontmatter ends at the first ``---`` line of its own, not at the
    # first ``---\n`` substring: a value such as ``x---`` or ``Q3---`` ends a
    # YAML line with those characters and must not truncate the mapping.
    closing = markdown.find("\n---\n", 3)
    if closing < 0:
        raise IntegrityError("Invalid Markdown frontmatter: no closing --- line")
    raw = markdown[4 : closing + 1]
    body = markdown[closing + 5 :]
    try:
        metadata = yaml.load(raw, Loader=UniqueKeyLoader)
    except (ValueError, yaml.YAMLError) as exc:
        raise IntegrityError(f"Invalid Markdown frontmatter: {exc}") from exc
    if not isinstance(metadata, dict):
        raise IntegrityError("Frontmatter must be a YAML mapping")
    if expected_id is not None and metadata.get("id") != expected_id:
        raise IntegrityError("Frontmatter id does not match filename")
    body = body.lstrip("\n")
    if body.endswith("\n"):
        body = body[:-1]
    # Remember which optional fields the source file carried, so render()
    # can write them back instead of dropping a default that the file's own
    # writer chose to spell out.
    observed = tuple(key for key in _OPTIONAL_DEFAULT_FIELDS if key in metadata)
    try:
        document = MemoryDocument.model_validate({**metadata, "body": body})
        document._observed_default_fields = observed
        return document
    except ValidationError as exc:
        # Forward compatibility: a newer memfw may write frontmatter keys this
        # version does not know. Ignore exactly those keys (the document stays
        # visible) instead of rejecting the whole memory; any other error is
        # still an integrity error. A rewrite by this version drops them.
        unknown = {
            str(error["loc"][0])
            for error in exc.errors()
            if error["type"] == "extra_forbidden" and error.get("loc")
        }
        if not unknown or len(unknown) != len(exc.errors()):
            raise IntegrityError(f"Invalid memory document: {exc}") from exc
        known = {key: value for key, value in metadata.items() if str(key) not in unknown}
        try:
            document = MemoryDocument.model_validate({**known, "body": body})
            document._ignored_keys = tuple(sorted(unknown))
            document._observed_default_fields = observed
            return document
        except Exception as retry:
            raise IntegrityError(f"Invalid memory document: {retry}") from retry
    except Exception as exc:
        raise IntegrityError(f"Invalid memory document: {exc}") from exc
