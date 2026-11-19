"""The ``EntailmentChecker`` port (ports 0.4.0, W1-1). Standard library only.

An entailment checker answers one question for the wiki evidence gate: is
this sentence (the *hypothesis*) supported by this cited text (the
*premise*)? It returns a score in ``[0, 1]``; higher means better supported.
The gate keeps a wiki claim only when the best score over its citations
reaches a calibrated threshold. A natural-language-inference cross-encoder is
one implementation, a fact-checking model another; both live in adapter
packages and pass ``memfw.ports.testing.check_entailment_checker``.

The kernel never runs a checker. A checker never writes anything and never
logs its inputs (they are body text).
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from .types import Availability

#: Contract version of ``EntailmentChecker`` (``memfw.ports.PORT_VERSIONS``).
ENTAILMENT_PORT_VERSION: Final = 1


@runtime_checkable
class EntailmentChecker(Protocol):
    """Premise + hypothesis → support score in ``[0, 1]``.

    ``name`` follows ``check_name``; ``version`` names the checker build (for
    example ``<model>@<digest>``) and is recorded beside every score, so a
    score is only compared with scores from the same version. ``score`` is
    deterministic for the same inputs, returns a finite ``float`` (never a
    ``bool``) and raises ``ValueError`` for an empty premise or hypothesis.
    It may raise ``ProviderUnavailable`` when the model cannot run; the
    caller then treats every claim it could not score as failing the check
    (fail closed), never as passing.
    """

    name: str
    version: str

    def available(self) -> Availability: ...

    def score(self, premise: str, hypothesis: str) -> float: ...


__all__ = ["ENTAILMENT_PORT_VERSION", "EntailmentChecker"]
