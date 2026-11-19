# memfw-wiki-gate

The deterministic checks a generated memfw wiki page goes through before a
human reviews it. The generator's worker runs them before submitting a
candidate; the intake stage runs the same code again against the current
admitted view. No model runs here.

For each claim (one cited sentence) of a `memfw-wiki-page/1` page, against the
`memfw-wiki-job/1` job it answers:

- **citations** resolve to job sources (and, at intake, to the same record in
  the admitted view); never a wiki page, a `file:` / URL / bare pointer, a
  conversation, generated text, or a source that repeats an approved page
  sentence (loop);
- **lifecycle**: superseded, retired, expired and disputed sources only in
  `timeline` (disputed also in `open`), as `superseded` claims that also cite
  the replacement;
- **conflicts**: no claim outside `open` / `timeline` takes a side of a job
  open question's conflict, and no claim cites both sides;
- **causal wording** (zh + en lexicon): the wording asserts no more than the
  claim's causal label, which is at most the strongest cited source's and
  never above `hypothesis`; unhedged causal wording and certainty wording are
  refused;
- **tokens**: numbers (CJK numerals and Arabic digits are equivalent), times,
  dates, versions, ids, paths, quotes and English names must appear in a cited
  source text;
- **NLI**: only the caller's own scores (`nli_scores` by claim id, with the
  checker's `nli_model`), never the generator's; with `require_nli` every
  claim needs one and the model must be allowlisted.
- **hidden characters**: a claim with any format character (zero-width
  joiners included) or a word mixing Latin with Cyrillic/Greek letters is
  dropped; the lexicon ignores combining marks, variation selectors,
  whitespace inside Chinese markers and traditional forms.

The result: per-claim verdicts with fixed reason codes, a page verdict
(`publish`, `needs_attention`, `retry`, `reject`), counts, the privacy
(strictest of all the job's sources, which the page lists; `local-private` is
never published) and the surviving claims as a `WikiPage`. Reports carry
codes, counts and claim ids (sentence hashes only as an HMAC under a key the
caller passes), never page or source text.

```python
from memfw_wiki_gate import check_page

report = check_page(page_json, job_json)              # worker
report = check_page(page_json, job_json, view=view)   # intake (admitted-view reader)
if report.publishable:
    candidate = report.page
```

Standard library only at run time. It depends on `memfw-ports` (the wiki
formats) and on nothing else of memfw. Apache-2.0, like memfw. See
`docs/wiki-gate.md` in the memfw repository.
