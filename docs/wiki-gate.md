# memfw-wiki-gate: deterministic checks for wiki pages

`packages/memfw-wiki-gate` (wheel `memfw_wiki_gate`, version 0.1.0) is PR W1-2
of the LLM Wiki (v0.5.0, section W of the pipeline plan). A wiki page is
generated text about one ontology concept, every sentence cited
(`memfw-wiki-page/1`, `memfw.ports.wiki`). The gate decides, without running a
model, which of its sentences may go on to human review. The generator's worker
runs it before submitting a candidate; the intake stage runs the same code
again, against the current admitted view, before a human sees anything.

Standard library only at run time; it depends on `memfw-ports` 0.4.0 for the
wiki formats. memfw does not import it today (the kernel's wiki record type,
W1-3, will).

## 1. Use

```python
from memfw_wiki_gate import GateOptions, check_page

report = check_page(page, job)               # page / job: object, mapping or JSON text
report = check_page(page, job, view=reader)  # intake: also against the admitted view
report = check_page(page, job, nli_scores=scores, nli_model=model,
                    options=GateOptions(require_nli=True, nli_allowlist={model}))
report.verdict        # "publish" | "needs_attention" | "retry" | "reject"
report.page           # surviving claims as a WikiPage (publish / needs_attention), else None
report.claims         # one ClaimVerdict per claim: index, section, cid, reasons
                      # (sentence_hash only with GateOptions(report_hash_key=...))
report.counts         # claims_total / _accepted / _dropped / _withheld, dropped_by_reason, by_section
report.to_json()      # memfw-wiki-gate-report/1: codes, counts, ids and hashes only
```

`check_page` never raises for bad input. `reader` implements
`SourceResolver.resolve(ref) -> ResolvedSource | None` over the admitted view
(released records only, one tenant); `GateOptions` turns on the loop check
(`approved_sentence_hashes`, from `page_sentence_hashes(approved_pages)`), names
known generated refs, asks for NLI scores (`require_nli`, `nli_allowlist`,
optional `nli_threshold`) and takes an optional per-run `report_hash_key`.

The job's `previous_page` always takes part in the loop check, with one
exemption: a previous-page sentence does not count against a source that
sentence itself cites (a page quoting its source is not a loop). The gate
removes the previous page's sentence hashes from `approved_sentence_hashes`
before checking, so a caller that passes all approved pages, the current one
(= `previous_page`) included, does not cancel that exemption; other approved
pages count in full.

**NLI scores are the caller's.** The gate never trusts the generator's
`checks.nli.min` or `build.nli_model`: a generator can write any number there.
The caller runs its own entailment checker and passes `nli_scores` (claim
`cid` -> score in `[0, 1]`) and `nli_model` (its checker's model id). With
`require_nli`, a claim without a caller score is dropped (`nli_missing`) and a
caller model outside `nli_allowlist` (or none) refuses the page
(`nli_model_not_allowlisted`); `nli_threshold` drops a claim whose caller
score is below it. The published claims carry the caller's score (or none)
and the published build the caller's `nli_model`. A malformed `nli_scores`
raises `ValueError` (it is the caller's input, not the generator's).
**Turn `require_nli` on before publishing to production**: without it a page
publishes on the deterministic checks alone, which catch changed tokens and
wording, not a reversed meaning.

**Sentence hashes in reports are keyed.** By default a report carries counts
and claim ids only: an unkeyed SHA-256 of a short sentence can be guessed by
hashing candidates. With `report_hash_key` (at least 16 bytes, one per run,
kept by the caller) each claim verdict carries
`hmac-sha256:<hex>` of its normalised sentence, which matches across reports
of the same run only.

## 2. What is checked

The page envelope first; any failure refuses the whole page (`reject`) and no
claim is looked at:

| Code | When |
|---|---|
| `page_format` | not a JSON object with exactly the page keys, duplicate keys, NaN, sections unknown, repeated or out of order |
| `job_invalid` | the job does not parse as `memfw-wiki-job/1` (a job never carries a `local-private` source) |
| `job_mismatch` | concept, tenant, page key, job digest, view version, checker version or generator fields differ from the job's |
| `source_not_in_job` | a listed page source is not exactly a job source |

The generator's `open_questions` value is ignored: the published page lists
every open question of the job (sorted by id).

Then every claim on its own. A claim that fails any check is dropped; the
report lists every reason, in this fixed order:

| Group | Codes | Rule |
|---|---|---|
| format | `claim_format`, `format_character`, `mixed_script_word`, `duplicate_cid`, `uncited`, `citation_malformed`, `citation_not_canonical`, `wiki_citation`, `unversioned_memory`, `duplicate_citation`, `epistemic_above_inference`, `causal_label_above_cap`, `causal_outside_relations`, `kind_section` | no `Cf` character at all (`format_character`: ZWJ and ZWNJ too, which the ports format allows, since they can split a marker or a token), no word mixing Latin with Cyrillic or Greek letters (`mixed_script_word`); the claim must build as a ports `WikiClaim`. `file:`, URLs, `sha256:`, bare paths and `conv:` are not citations; `wiki:` and `mem:wiki-…` are pages (v2 R1, R2) |
| resolution | `citation_unresolved`, `source_changed`, `source_not_releasable`, `tenant_mismatch`, `generated_source`, `loop_source` | every citation is a job source, still the same record in the view; not generated text (G1, by target not prefix); not a source with a sentence that hashes like a sentence of an approved page or of the job's `previous_page` (G4; a previous-page sentence does not count against a source that sentence itself cites, since a page quoting its source is not a loop) |
| lifecycle | `inactive_source_section`, `inactive_source_kind`, `superseded_without_replacement` | non-current sources only in `timeline` (`disputed` also in `open`), there as kind `superseded`, which also cites the source's `superseded_by` (G6) |
| conflict | `conflict_side`, `conflict_both_sides` | a side of a job open question's conflict only in `open` / `timeline`; never both sides in one claim (design 2.3) |
| causal | `causal_above_source`, `causal_wording_above_label`, `causal_wording_unhedged`, `certainty_wording` | label at most the strongest cited source, capped at `hypothesis` (two associations stay an association); wording at most the label; no certainty wording (G5, G7) |
| tokens | `token_unsupported_{quote,path,date,time,version,id,number,name}` | check ①: each token appears in a cited source text |
| NLI | `nli_missing`, `nli_below_threshold` | the caller's scores only: missing with `require_nli`, below `nli_threshold` when set |
| page | `section_withheld` | the claim passed, but its section lost more than half its claims |

Page-level outcomes after the claims (worst wins):

| Code | Verdict | When |
|---|---|---|
| `privacy_not_releasable`, `tenant_mismatch` | `reject` | the view returns a job source (cited or not) as `local-private` or in another tenant |
| `previous_page_stricter` | `reject` | the job's `previous_page` is stricter than the strictest job source (the generator never offers one; the job or its builder is wrong). To rebuild, the job builder drops `previous_page` from the job (a new job digest) |
| `nli_model_not_allowlisted` | `reject` | `require_nli` and the caller's `nli_model` is missing or not allowlisted |
| `summary_empty` | `reject` | no `summary` claim survives (design 1.2) |
| `internal_inconsistent` | `reject` | the surviving claims do not rebuild as a valid page (a gate bug; never seen in tests) |
| `source_changed`, `source_missing`, `view_unavailable` | `retry` | a job source (cited or not) changed, vanished, or the reader failed: the candidate is void, the plan stage re-queues (design 5.3) |
| `section_withheld` | `needs_attention` | a section was withheld whole |
| `privacy_recomputed` | `publish` | the generator's privacy differed from the recomputed one (it is ignored) |

The published page keeps only surviving claims, with `checks.tokens = "pass"`.

**Privacy (rule shared with the W1-4 generator).** The page's `sources` are
*all* the job package's sources (sorted by ref), not only those the surviving
claims cite, and its privacy is the strictest of them. So the ports invariant
`privacy == strictest(page.sources)` holds unchanged, and a page never comes
out looser than its job because a claim citing the shared source was
dropped. A job's `previous_page` stricter than its sources is refused (see
above). With `view=`, every job source is looked up in the view (cited or
not), so a changed level is a `retry` and a `local-private` or other-tenant
record a `reject`. Every page the gate returns passes
`memfw.ports.testing.check_wiki_page(page, job=job)`.

## 3. Token consistency (check ①)

A necessary condition, not a meaning check: it catches a changed number,
time, date, version, id, path, quote or name, not a reversed meaning (that is
the NLI check, W1-5). Extraction order and rules are in
`memfw_wiki_gate/tokens.py`. Decisions:

- **One spelling of every digit.** Both sides map every Unicode decimal digit
  (`Nd`: `٤٠`, `४०`, full-width `４０`) to ASCII, `−` (U+2212) to `-` and the
  financial numerals `壹贰叁肆伍陆柒捌玖拾佰仟` to everyday ones. A leading
  minus is part of a number (`-5` is not `5`).
- **Units.** A claim number with a unit (`天`/days, `小时`/hours,
  `分钟`/minutes, `秒`/seconds, weeks, `个月`/months, years, `%`/percent,
  `次`/times, sizes) needs the same unit in the source: `30 天` is backed by
  neither `30 hours` nor a unitless `30`. A unitless claim number is backed by
  the value with any unit. Scientific notation is one number (`1e3` = 1000).
- **Date and time parts** back only a claim number with a date or time unit
  (`月 日 号 点 时 分 年`, a month name, am/pm, o'clock) or after the word
  "year": `11 月` and `the year 2026` are backed by `2026-11-01`;
  `11 snapshots` and a bare `2026` are not.
  Known limit: `02:00 pm` is read as `2:00` (the `pm` is not applied).
- **Paths and quotes at boundaries.** A path is supported by the same path or
  a leading part of a longer one (`/srv/a` by `/srv/a/b`), never by a longer
  name (`/srv/backup/rest` is not in `/srv/backup/restic`); a sentence's
  closing punctuation is not part of a path, and a relative path is not part
  of an absolute one (`srv/backup/restic` is not backed by
  `/srv/backup/restic`). A quote that starts or ends with a Latin letter or
  digit matches at word boundaries (`"keep it"` is not in `keep items`); a
  quote edge that is a digit or CJK numeral never continues a longer numeral
  in the source (`「十天」` is not in `三十天`), and the numbers, dates and
  other tokens inside a quote are checked as tokens too. Zero-width characters inside a token never reach this check:
  any `Cf` drops the claim first.
- **CJK numerals and Arabic digits are equivalent.** A claim's `3` is backed
  by a source's `三`, and `三台` by `3 台`. On the claim side a CJK numeral is
  checked before a counter (`三台`, `三 台`, `两次`, `十二个`) or when it closes
  a sentence after a non-CJK character or a copula / quantity word (`是三。`),
  because bare `一` is part of ordinary words (`一致`, `统一。`); on the source
  side every CJK numeral counts. Full-width digits fold (NFKC). English number words and
  CJK-numeral dates are not recognised on the claim side.
- A source's numbers include the parts of its dates and times, not of its
  versions (`4 snapshots` is not backed by `restic 0.16.4`).
- A date matches when every component the claim names (year, month, day)
  equals the source's; a claim may not add a year the source lacks.
- English names: ALLCAPS, CamelCase, and capitalised words not at a sentence
  start. Chinese names are not checked deterministically.

## 4. Causal wording (G5)

`memfw_wiki_gate/causal.py` holds the full lexicon (`LEXICON`: causal, hedge,
association and certainty markers, zh and en). Matching runs on copies of the
claim text: NFKD, case folded, every `Cf`, `Mn` and `Me` character removed
(zero-width joiners, combining accents, variation selectors); the Chinese copy
also drops all whitespace and maps traditional forms (導致, 引發, 觸發, 因為)
to simplified ones, so `导 致`, `导́致` and `导️致` read as `导致`. English
markers are whole words (`triggered`, `thus`, `hence`, `therefore`,
`stems from`, `responsible for`, `as a consequence`, and `, so ` as a
connective); Chinese adds `触发` and `所致`. A negated modal is not a hedge (`could not`, `may
not`, `cannot`, `不可能`, `不太可能`; `unlikely` is not in the lexicon), and a
capitalised `May` before a day or year, or after `in`, is the month. A causal marker with a hedge
asserts `hypothesis`; without a hedge, or with a certainty marker, it asserts
`verified`, which no claim may carry, so unhedged causal wording is always
dropped. Association markers are statistical ones only (相关性, 正相关,
correlated); "related to" wording (相关的配置, associated with) is not. It is
conservative on purpose: negation is not parsed ("并未导致" still counts), and
a hedge anywhere in the sentence counts for the whole sentence, so "因为磁盘
可能写满，所以备份失败" passes as a hypothesis; the NLI check and the human
review are the next lines.

## 5. Attack fixtures

`tests/fixtures/wiki_gate/attacks.json` carries the v2 design's write-gate
attacks (R1 cite a page through `file:` / `https:` / bare pointers, R2 mix in
an unrelated or generated pointer, R3 write `fact` through a page, R4 loop a
page back through a source body, R5 escalate causation per sentence) and the
v3 rules (G6, conflicts, tokens in zh and en, NLI presence, format) plus the
review round's evasions (zero-width, combining and variation-selector
characters inside markers, whitespace-split and traditional markers, mixed
scripts, Arabic-Indic and Devanagari digits, unit and sign changes, truncated
paths, partial-word and cut-numeral quotes, negated modals, "May" the month),
114 cases on a synthetic corpus (`corpus.json`). The false-accept count is 0, and 28
controls (CJK/Arabic equivalence, financial numerals, units, hedged
hypotheses, superseded history) are kept. `tests/test_wiki_gate.py` also covers the view, the privacy matrix,
determinism (job source order, hash seed) and that reports never contain page
or source text, refs or token values.

## 6. Not covered here

- NLI scoring and the threshold calibration (W1-5, W1-6): the gate only
  applies the caller's scores (presence with `require_nli`, an allowlisted
  caller model, an optional threshold).
- `memfw-wiki-gate` calls no port: it reads the ports wiki formats and pins
  `memfw-ports` to the ports API version (`docs/ports-and-recall.md`, the
  ports-API pin table), not to an `EntailmentChecker` contract version.
- The kernel rules G1–G4 on memory writes (W1-3); the gate's loop check (G4)
  works on source texts and is defeated by rewording, as the design says.
- Read-time clearance (W2-1).
