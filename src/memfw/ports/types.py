"""Value types shared by the four ports. Stdlib only.

Everything here is a frozen dataclass with validation in ``__post_init__``.
An adapter that builds an invalid value fails at construction, inside its own
call, so the coordinator can mark that one adapter degraded instead of
letting a malformed value reach the kernel.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, get_args

from .proxies import SEGMENT_REF_RE

#: Mirrors ``memfw.core.epistemic``; ``memfw.ports`` imports nothing else from
#: memfw so it can ship as a standalone distribution. A test keeps them equal.
EPISTEMIC_VALUES: tuple[str, ...] = ("experience", "fact", "judgment", "inference")
CAUSAL_VALUES: tuple[str, ...] = ("none", "association", "hypothesis", "verified")

#: ``attachment_segment`` (ports 0.3.0): one segment of an attachment proxy,
#: ``<prx-id>:<n>`` (``memfw.ports.proxies.SEGMENT_REF_RE``).
RefKind = Literal["memory_id", "path", "attachment_segment"]
REF_KINDS: tuple[str, ...] = get_args(RefKind)

#: Scalar values allowed in ``Candidate.extra``.
ExtraValue = str | int | float | bool | None

#: The only keys a provider may put in ``Candidate.extra``. An allowlist, not
#: a denylist: a recall provider never returns body text, and an unknown key
#: is the easiest place to smuggle some. The kernel reads the body from the
#: vault after resolve and filter.
ALLOWED_EXTRA_KEYS: frozenset[str] = frozenset(
    {"why", "score_raw", "rank_raw", "chunk", "page", "t", "lang", "kind", "locator"}
)

#: Longest string value in ``Candidate.extra``.
MAX_EXTRA_STRING = 64
#: Cap on the whole ``extra`` mapping, as compact JSON-ish ``key=value`` bytes.
MAX_EXTRA_BYTES = 256
MAX_REF_CHARS = 512

#: A memory id, the same rule the vault applies.
MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
#: A path or URI: no whitespace, control characters, quotes, backslashes or
#: angle brackets. Non-ASCII letters (e.g. CJK file names) are allowed.
PATH_REF_RE = re.compile(r"^[^\s\x00-\x1f\x7f\\\"'<>`]{1,512}$")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def check_name(value: str, *, what: str = "name") -> str:
    """Adapter and provider names: lowercase ASCII, digits, ``-`` and ``_``."""

    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ValueError(f"{what} must match {_NAME_RE.pattern}")
    return value


class ProviderUnavailable(RuntimeError):
    """Raised by a provider that cannot serve right now.

    The coordinator records the provider as degraded with reason
    ``unavailable``. The message is kept out of metrics; only the reason code
    is reported.
    """


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether an adapter can serve. ``reason`` is a short code, never user data."""

    available: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> Availability:
        return cls(True, "")

    @classmethod
    def missing(cls, reason: str) -> Availability:
        return cls(False, reason)


@dataclass(frozen=True, slots=True)
class RecallBudget:
    """Per-call limits for one provider.

    ``timeout_seconds`` bounds the wall time the coordinator waits for this
    provider. ``max_candidates`` caps how many candidates are kept from it.
    """

    timeout_seconds: float = 1.5
    max_candidates: int = 50

    def __post_init__(self) -> None:
        if not self.timeout_seconds > 0:
            raise ValueError("timeout_seconds must be positive")
        if isinstance(self.max_candidates, bool) or self.max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One recall hit: an external reference plus a score. Never body text.

    ``ref`` is a memory id (``ref_kind="memory_id"``), a path the provider
    indexed (``ref_kind="path"``) or a proxy segment (``ref_kind=
    "attachment_segment"``, ``<prx-id>:<n>``); a resolver turns it into the id
    the caller reads.
    ``rank`` is 1-based within the provider's own list.
    """

    ref: str
    ref_kind: RefKind
    score: float
    provider: str
    rank: int
    extra: Mapping[str, ExtraValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_candidate(self)
        # Freeze a caller's dict so later mutation cannot change the candidate.
        object.__setattr__(self, "extra", dict(self.extra))


def validate_candidate(candidate: Candidate) -> Candidate:
    """Check every candidate rule. Raises ``ValueError``.

    Runs at construction, and again in the coordinator: a subclass or an
    ``object.__setattr__`` after construction must not get a bad value past.
    """

    if type(candidate) is not Candidate:
        raise ValueError("candidates must be exactly memfw.ports.Candidate")
    ref = candidate.ref
    if candidate.ref_kind not in REF_KINDS:
        raise ValueError(f"ref_kind must be one of {REF_KINDS}")
    if type(ref) is not str or len(ref) > MAX_REF_CHARS:
        raise ValueError("ref must be a string of at most 512 characters")
    pattern = {
        "memory_id": MEMORY_ID_RE,
        "path": PATH_REF_RE,
        "attachment_segment": SEGMENT_REF_RE,
    }[candidate.ref_kind]
    if not pattern.fullmatch(ref):
        raise ValueError("ref does not match the id/path pattern")
    score = candidate.score
    if type(score) not in (int, float) or not math.isfinite(score):
        raise ValueError("score must be a finite number")
    check_name(candidate.provider, what="provider")
    if type(candidate.rank) is not int or candidate.rank < 1:
        raise ValueError("rank must be a positive integer")
    extra = candidate.extra
    if not isinstance(extra, Mapping):
        raise ValueError("extra must be a mapping")
    size = 0
    for key, value in extra.items():
        if type(key) is not str or key not in ALLOWED_EXTRA_KEYS:
            raise ValueError("extra key is not allowed: providers return no body")
        if value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("extra values must be plain scalars")
        if type(value) is float and not math.isfinite(value):
            raise ValueError("extra numbers must be finite")
        text = "" if value is None else str(value)
        if type(value) is str and len(value) > MAX_EXTRA_STRING:
            raise ValueError("extra string is too long")
        size += len(key.encode()) + len(text.encode()) + 2
    if size > MAX_EXTRA_BYTES:
        raise ValueError("extra is too large")
    return candidate


@dataclass(frozen=True, slots=True)
class ProjectionPolicy:
    """What a projector exports. ``version`` lands in the manifest."""

    version: str
    options: Mapping[str, ExtraValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("policy version required")
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True, slots=True)
class Manifest:
    """Description of one projection. The output is rebuildable from the vault.

    ``content_sha256`` is a digest over the exported files in a stable order,
    so two exports of the same vault under the same policy compare equal.
    """

    projector: str
    policy_version: str
    projected: int
    skipped: Mapping[str, int]
    content_sha256: str
    generated_at: datetime
    files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_name(self.projector, what="projector")
        if isinstance(self.projected, bool) or self.projected < 0:
            raise ValueError("projected must not be negative")
        if any(count < 0 for count in self.skipped.values()):
            raise ValueError("skipped counts must not be negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_sha256):
            raise ValueError("content_sha256 must be 64 lowercase hex characters")
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must include a timezone")
        if any(
            name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/")
            for name in self.files
        ):
            raise ValueError("manifest files must be relative paths inside the output")
        object.__setattr__(self, "skipped", dict(self.skipped))


@dataclass(frozen=True, slots=True)
class IngestItem:
    """One external record offered to an ingestor."""

    title: str
    body: str
    external_ref: str = ""
    metadata: Mapping[str, ExtraValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title required")
        if not isinstance(self.body, str):
            raise ValueError("body must be a string")
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class StageResult:
    """Candidate ids written (``staged``) or already present (``duplicates``)."""

    staged: tuple[str, ...]
    duplicates: tuple[str, ...] = ()

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return self.staged + self.duplicates


@dataclass(frozen=True, slots=True)
class Citation:
    """A derived view's pointer back to the memory it used."""

    memory_id: str
    version: int | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not self.memory_id.strip():
            raise ValueError("citation memory_id required")


@dataclass(frozen=True, slots=True)
class DeriveRequest:
    """Input to a deriver: which memories to use and what kind of view."""

    kind: str
    memory_ids: tuple[str, ...]
    options: Mapping[str, ExtraValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_name(self.kind, what="kind")
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True, slots=True)
class DerivedView:
    """A read-only artifact built from vault data.

    A view never writes back by itself; anything that should become memory
    goes through an ``Ingestor`` as a candidate. ``epistemic_cap`` is the
    strongest epistemic type any statement in the view may claim, and
    ``causal_cap`` the strongest causal grade. A derived view cannot claim a
    verified cause: verification needs a reviewed record, not a derivation.
    """

    deriver: str
    kind: str
    content: str
    citations: tuple[Citation, ...]
    epistemic_cap: str = "inference"
    causal_cap: str = "association"
    read_only: bool = True

    def __post_init__(self) -> None:
        check_name(self.deriver, what="deriver")
        check_name(self.kind, what="kind")
        if not self.read_only:
            raise ValueError("derived views are read-only")
        if not self.citations:
            raise ValueError("a derived view must cite at least one memory")
        if self.epistemic_cap not in EPISTEMIC_VALUES:
            raise ValueError(f"epistemic_cap must be one of {EPISTEMIC_VALUES}")
        if self.causal_cap not in CAUSAL_VALUES:
            raise ValueError(f"causal_cap must be one of {CAUSAL_VALUES}")
        if self.causal_cap == "verified":
            raise ValueError("a derived view cannot claim a verified cause")

    def cited_ids(self) -> tuple[str, ...]:
        return tuple(citation.memory_id for citation in self.citations)


__all__ = [
    "CAUSAL_VALUES",
    "EPISTEMIC_VALUES",
    "ALLOWED_EXTRA_KEYS",
    "MEMORY_ID_RE",
    "PATH_REF_RE",
    "REF_KINDS",
    "Availability",
    "Candidate",
    "Citation",
    "DeriveRequest",
    "DerivedView",
    "ExtraValue",
    "IngestItem",
    "Manifest",
    "ProjectionPolicy",
    "ProviderUnavailable",
    "RecallBudget",
    "RefKind",
    "StageResult",
    "check_name",
    "validate_candidate",
]
