"""Read-only review view over staged memory candidates."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..core.errors import IntegrityError


def list_staged_candidates(root: Path | str) -> list[dict[str, Any]]:
    base = Path(root).expanduser() / "_candidates" / "remember"
    if not base.is_dir():
        return []
    output: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise IntegrityError(f"Candidate has no frontmatter: {path}")
        try:
            _, header, body = text.split("---\n", 2)
            parsed = yaml.safe_load(header) or {}
        except (ValueError, yaml.YAMLError) as exc:
            raise IntegrityError(f"Invalid candidate: {path}") from exc
        metadata = parsed.get("metadata") if isinstance(parsed, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        output.append(
            {
                "path": str(path),
                "title": str(parsed.get("name") or "memory"),
                "body": body.strip(),
                "tags": list(metadata.get("tags") or []),
                "related": list(metadata.get("related") or []),
                "suggestions": list(metadata.get("suggestions") or []),
                "tenant": str(metadata.get("tenant") or ""),
            }
        )
    return output


__all__ = ["list_staged_candidates"]
