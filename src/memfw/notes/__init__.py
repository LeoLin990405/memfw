"""Markdown notes retrieval: section index, hybrid search, optional embeddings."""

from .index import (
    EmbeddingProvider,
    HashEmbeddingProvider,
    HttpEmbeddingProvider,
    NoteHit,
    NoteParseError,
    NotesIndex,
    parse_markdown,
)

__all__ = [
    "EmbeddingProvider",
    "HashEmbeddingProvider",
    "HttpEmbeddingProvider",
    "NotesIndex",
    "NoteHit",
    "NoteParseError",
    "parse_markdown",
]
