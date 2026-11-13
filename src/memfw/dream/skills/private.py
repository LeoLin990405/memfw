"""Operator-chosen private terms, masked everywhere in a skill candidate.

``vault.yaml``::

    skills_distill:
      private_terms: [projectname, hostname]
      private_terms_file: /path/outside/the/repo/private-terms.txt

The file holds one term per line (blank lines and ``#`` comments ignored); a
relative path is resolved against the vault root. Matching is
case-insensitive and whole-word (letters, digits and ``_`` bound a word).
A term becomes ``<private>`` in text and ``private`` in a skill name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MASK = "<private>"
MAX_TERMS = 10_000
MAX_FILE_BYTES = 1_000_000


class PrivateTerms:
    def __init__(self, terms: Iterable[str] = ()) -> None:
        cleaned = sorted({term.strip() for term in terms if len(term.strip()) >= 2}, key=len)
        self.terms = list(reversed(cleaned))[:MAX_TERMS]
        self._pattern = (
            re.compile(
                r"(?<![A-Za-z0-9_])(?:"
                + "|".join(re.escape(term) for term in self.terms)
                + r")(?![A-Za-z0-9_])",
                re.IGNORECASE,
            )
            if self.terms
            else None
        )

    def __bool__(self) -> bool:
        return self._pattern is not None

    @classmethod
    def from_config(cls, config: dict[str, Any], root: Path) -> PrivateTerms:
        section = config.get("skills_distill") or {}
        if not isinstance(section, dict):
            raise ValueError("skills_distill must be a mapping")
        raw = section.get("private_terms") or []
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError("skills_distill.private_terms must be a list of strings")
        terms = list(raw)
        file = section.get("private_terms_file")
        if file:
            if not isinstance(file, str):
                raise ValueError("skills_distill.private_terms_file must be a path")
            path = Path(file).expanduser()
            if not path.is_absolute():
                path = Path(root) / path
            try:
                with path.open("rb") as handle:
                    data = handle.read(MAX_FILE_BYTES + 1)
            except OSError as exc:
                raise ValueError(
                    f"cannot read skills_distill.private_terms_file: {exc.strerror}"
                ) from exc
            if len(data) > MAX_FILE_BYTES:
                raise ValueError("skills_distill.private_terms_file is too large")
            for line in data.decode("utf-8", errors="replace").splitlines():
                term = line.split("#", 1)[0].strip()
                if term:
                    terms.append(term)
        return cls(terms)

    def matches(self, text: str) -> bool:
        return self._pattern is not None and bool(self._pattern.search(text))

    def mask(self, text: str, replacement: str = MASK) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(replacement, text)

    def mask_name(self, name: str) -> str:
        """A skill name stays a slug: ``private`` replaces a term."""

        return self.mask(name, "private")

    def mask_all(self, value: Any) -> Any:
        """Mask every string (keys too) in a JSON-like value."""

        if isinstance(value, str):
            return self.mask(value)
        if isinstance(value, dict):
            return {self.mask(str(key)): self.mask_all(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.mask_all(child) for child in value]
        return value
