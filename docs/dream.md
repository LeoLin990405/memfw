# Dream

`memfw dream run` is an offline pass over a vault that checks memories against
the transcript index ([`memfw history`](history.md)) and proposes cleanups. It
is verify-first, deterministic and local, and it **never writes the
authoritative vault**: every result is a candidate under
`_candidates/dream/<dream_id>/` that a human reviews.

## Operations

In priority order:

1. **Verify (always runs).** For each cited memory, the dream searches the
   transcript index and classifies each matching entry as supporting,
   contradicting or neutral, then proposes one of:
   - `confirm`: only supporting evidence; proposes `status: confirmed` and the
     evidence refs;
   - `correct`: the newest evidence contradicts the memory and states a single
     replacement value (`18` -> `21`); proposes the corrected body,
     `status: confirmed` and the refs. A value with more than six digits
     (phone-, card- or account-like) or anything the redactor would mask is
     never copied: such evidence gives `mark-disputed` instead;
   - `mark-disputed`: contradicting evidence without one replacement value, or
     support and contradiction together; proposes `status: disputed`.
   A memory with no evidence gets no candidate and stays `unconfirmed`.
2. **Curate.** Inside each concept (`about` oid):
   - `merge`: identical bodies, near-identical wording, or paraphrases that
     state the same values (numbers, number words, attribute values) with
     shared context; the earliest memory is kept. Never across different
     subjects (staging vs production, Alice vs Bob, `service: api` vs
     `service: worker`) or when one memory negates the other;
   - `supersede`: the same attribute (`key: value` lines or `The <thing> is
     <value>` sentences) or the same context with different values; the newer
     memory is proposed to supersede the older one, which would get
     `valid_to`. Both must be about the same subject, and "X" vs "not X" is
     proposed here rather than merged. Nothing is silently overwritten;
   - `mark-stale`: a memory superseded by another but without `valid_to`, or an
     unreferenced, unconfirmed memory not updated for `--stale-days` (180).
     Nothing is deleted.
3. **Distill** (Lesson / Procedure from at least two independent events):
   proposal-only stub in v1; reported as not implemented.
4. **Promote / demote:** proposal-only. In v1 this is the injected-layer
   budget check below.

"Cited" means a memory that is referenced (`reference_count > 0`), linked to a
concept (`about` / `applies_to`), linked from another memory's `related`, or
verified before. `--all` verifies every current memory. The most referenced
memories are checked first; `--limit` (200) caps how many.

## Matching (no model)

Verification is lexical and deterministic: an entry counts when it shares at
least half of a memory's content words (and at least two). Values (numbers,
versions) decide support or contradiction; negations (`not`, `no longer`,
`deprecated`, ...) also contradict. Redaction markers are ignored.

An optional **local** judge can replace the per-entry classification. The
judge is Python code that sees transcript text, so only the operator chooses
it:

```sh
memfw dream run --root /path/to/vault --judge my_package.judges:classify
```

`dream_judge: module:function` in `vault.yaml` is **ignored** unless the run
also passes `--allow-config-judge`: the same file chooses the transcript
sources, so an untrusted vault must not be able to pick code that reads them.
The report says when a configured judge was ignored.

The callable gets `(memory_text, evidence_text)` and returns `support`,
`contradict` or `neutral`; errors are counted and the rule result is kept.
memfw ships no judge and makes no network calls; what a hook does is up to the
operator.

## Evidence and safety

- Transcript text is read only through the history index: redacted, with
  secret-bearing entries withheld. It is treated as data; instructions in a
  transcript are never followed.
- Candidates carry **evidence refs only** (`SOURCE:FILE_ID@OFFSET`, the opaque
  ids `memfw history` returns), never transcript paths or text. Read one with
  `memfw history read REF`.
- At most `--max-sessions` (20) transcript sessions are read per dream.
- The report lists the sessions read, how many candidates of each kind were
  produced, and the basis of each one.

## Memory fields

Memories have two optional fields:

```yaml
status: confirmed        # confirmed | unconfirmed | disputed
evidence:
  - claude:3f9c2a71d04e8b65@18342
```

Missing fields mean `unconfirmed` with no evidence, and files are only
written with them when they differ from that default, so existing vaults keep
parsing and keep their bytes. New memories start `unconfirmed`.

**Upgrade every reader before applying statuses.** memfw versions before
this one reject frontmatter keys they do not know, so a memory with `status`
or `evidence` disappears from an older reader. The reader in this version
ignores unknown keys (that fix can ship on its own first); once every agent
and service reading the vault has it, applying dream statuses is safe.

## Injected-layer budget

Files loaded at the start of every session must stay small: at most 200 lines
and 25 KB. The dream checks `MEMORY.md` at the vault root and every file named
in `vault.yaml`:

```yaml
injected_files:
  - AGENTS.md
  - /path/to/agent/CLAUDE.md
```

Paths are relative to the vault unless absolute. Missing files are left out;
a symlink (or a file reached through a symlinked directory in the vault) and
a non-regular file are reported as skipped, never followed. For a file over
budget the dream proposes demoting its largest sections (at most 20,
replaced by a one-line pointer) until it fits. Headings are redacted in the
proposals, and a heading that looks like a secret is withheld. It never
edits the file.

## Output

```sh
memfw dream run --root /path/to/vault --dry-run          # print, write nothing at all
memfw dream run --root /path/to/vault --focus "deploy"   # stage candidates
```

A dry run writes nothing, not even the derived transcript index: it reads an
existing index read-only, and without one it reports history as unavailable
(build it with `memfw history index`).

A staged dream is one directory, published with a single rename so concurrent
dreams never overwrite each other. Staging works on directory file
descriptors and refuses a symlink or file anywhere in
`_candidates/dream/`:

```
_candidates/dream/<dream_id>/
  <oid>.md        # one file per concept: YAML frontmatter with the candidates
  report.json     # what was read, counts, operations, budget
```

`dream_id` is `dream-<UTC time>-<hash of the vault state and options>` unless
`--dream-id` is given; re-running with an existing id fails. Every candidate
file says `authority: false`.

Options: `--all`, `--limit`, `--focus` (steers the search only), `--max-sessions`,
`--source-name`, `--since`, `--until`, `--tenant`, `--stale-days`, `--dream-id`,
`--judge`, `--allow-config-judge`, `--no-refresh`, `--now` (inject the clock).

## Applying

v1 only stages. Review a candidate file and make the change through the normal
write path, e.g.

```sh
memfw update MEMORY_ID --root /path/to/vault --status confirmed \
  --evidence 'claude:3f9c2a71d04e8b65@18342'
memfw update MEMORY_ID --root /path/to/vault --body '...corrected text...'
memfw supersede ...    # for a supersede candidate
```

A dedicated `memfw dream apply` is future work.

## Evaluation

`python -m eval.dreams` scores the curate step on synthetic duplicate,
conflict and stale cases (merge accuracy, conflict detection rate, false-merge
rate, stale-mark recall) and writes `eval/dreams/dreams_report.json`. The
adoption rate stays unknown until a person adopts proposals.

## Limitations

- Lexical matching: paraphrased evidence without shared words is missed, and
  a correction is only proposed for a memory that states exactly one value.
- Distill (③) is a stub; promote/demote (④) covers the budget check only.
- Frequency-based scheduling ("often used, long unverified first") is limited
  to ordering by `reference_count`.
