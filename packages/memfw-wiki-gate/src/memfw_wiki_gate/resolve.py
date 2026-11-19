"""Where a claim's citations resolve: the job package or an admitted-view reader.

The worker checks a page against the job it was built from (:class:`JobSources`).
The intake stage checks it against the job *and* the current admitted view:
it implements :class:`SourceResolver` over the view and passes it as
``view=``; a citation whose view record differs from the job's (hash, privacy,
causal grade, status, successor, tenant) means the source changed while the
job ran, so the candidate is void (design 5.3).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ._ports import WikiJob, WikiSource


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """One citation target: its page-source fields, released text and tenant.

    ``generated`` is true when the target is generated text (a rendered wiki
    page, a derived summary): design G1 classifies evidence by what a pointer
    resolves to, not by its prefix, so a ``kh:`` note that is a page render is
    still generated and no claim may cite it.
    """

    source: WikiSource
    text: str
    tenant: str
    generated: bool = False


@runtime_checkable
class SourceResolver(Protocol):
    """Resolves a canonical citation ref, or returns ``None`` when it is not there.

    An admitted-view reader returns only released records (``public`` /
    ``shared``, one tenant) and ``None`` for anything else, including a record
    the caller may not see. It raises any exception when it cannot answer; the
    gate then publishes nothing (``view_unavailable``).
    """

    def resolve(self, ref: str) -> ResolvedSource | None: ...


#: The name the design uses for the intake's resolver over the admitted view.
AdmittedViewReader = SourceResolver


class JobSources:
    """A :class:`SourceResolver` over a ``memfw-wiki-job/1`` package.

    ``generated`` names refs the caller knows to be generated text (G1); a job
    builder should never offer one, so this is a belt-and-braces input.
    """

    def __init__(self, job: WikiJob, *, generated: Iterable[str] = ()) -> None:
        self._tenant = job.tenant
        self._generated = frozenset(generated)
        self._items = {item.source.ref: item for item in job.sources}

    def resolve(self, ref: str) -> ResolvedSource | None:
        item = self._items.get(ref)
        if item is None:
            return None
        return ResolvedSource(
            source=item.source,
            text=item.text,
            tenant=self._tenant,
            generated=ref in self._generated,
        )


__all__ = ["AdmittedViewReader", "JobSources", "ResolvedSource", "SourceResolver"]
