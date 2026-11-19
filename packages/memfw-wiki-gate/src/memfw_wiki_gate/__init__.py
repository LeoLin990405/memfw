"""memfw-wiki-gate: deterministic evidence checks for generated wiki pages.

The worker (before submitting a candidate) and the intake stage (before a
human sees it) run the same checks over a ``memfw-wiki-page/1`` page and the
``memfw-wiki-job/1`` job it answers::

    from memfw_wiki_gate import check_page

    report = check_page(page_json, job_json)            # worker
    report = check_page(page_json, job_json, view=view)  # intake, view reader
    report.verdict          # publish | needs_attention | retry | reject
    report.page             # the surviving claims as a WikiPage, or None
    report.to_json()        # codes, counts, ids (keyed hashes on request): no text

Standard library only; the wiki formats come from ``memfw.ports`` (inside
memfw) or the ``memfw_ports`` wheel. No model runs here: NLI scores come
from the caller's own checker (``nli_scores=``, ``nli_model=``), never from
the generator's page.
"""

from __future__ import annotations

from .causal import CAUSAL_ORDER, LEXICON, asserted_causal, asserts_certainty
from .gate import (
    CHECKER_VERSION,
    CLAIM_REASONS,
    PAGE_REASONS,
    REPORT_FORMAT,
    VERDICTS,
    ClaimVerdict,
    GateOptions,
    GateReport,
    Verdict,
    check_page,
    page_sentence_hashes,
)
from .normalize import normalize_sentence, normalize_text, sentence_hash, sentence_hashes
from .resolve import AdmittedViewReader, JobSources, ResolvedSource, SourceResolver
from .tokens import TOKEN_KINDS, SourceIndex, Token, cjk_number, extract_tokens

__version__ = "0.1.0"

__all__ = [
    "CAUSAL_ORDER",
    "CHECKER_VERSION",
    "CLAIM_REASONS",
    "LEXICON",
    "PAGE_REASONS",
    "REPORT_FORMAT",
    "TOKEN_KINDS",
    "VERDICTS",
    "AdmittedViewReader",
    "ClaimVerdict",
    "GateOptions",
    "GateReport",
    "JobSources",
    "ResolvedSource",
    "SourceIndex",
    "SourceResolver",
    "Token",
    "Verdict",
    "asserted_causal",
    "asserts_certainty",
    "check_page",
    "cjk_number",
    "extract_tokens",
    "normalize_sentence",
    "normalize_text",
    "page_sentence_hashes",
    "sentence_hash",
    "sentence_hashes",
]
