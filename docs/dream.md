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
   proposal-only stub inside `dream run`; procedures are distilled into skill
   candidates by `memfw dream skills` (below).
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
ignores unknown keys (that fix can ship on its own first) and names them in
`memfw list` warnings, so a typo such as `tennant:` shows up; once every
agent and service reading the vault has it, applying dream statuses is safe.
Note that rewriting a memory with a version that does not know a key drops
that key.

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
(build it with `memfw history index`). A checkpointed index is read as an
immutable snapshot (no side files); an index with uncheckpointed changes in
its WAL is read with SQLite's read-only mode, and if that is not possible the
dream reports history as unavailable rather than using stale evidence.

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

## Skill candidates

`memfw dream skills` distills procedures that repeat across sessions (and
agents) into SKILL.md drafts. It reads only the redacted snippets of the
transcript index.

```sh
memfw dream skills --root /path/to/vault --dry-run
memfw dream skills --root /path/to/vault --min-sessions 3 --since 2026-01-01
```

1. **Episodes.** Each session is split into tasks: a user turn opens an
   episode, except a user turn that answers a tool call (a tool result).
2. **Steps.** Each tool call becomes a signature: a shell command keeps its
   program and up to two following words from a fixed sub-command vocabulary
   or known-program list (`cd repo && uv run pytest -q` -> `cmd:uv run
   pytest`, `git push origin my-branch` -> `cmd:git push`, `ssh host7 uptime`
   -> `cmd:ssh`). Free arguments (hosts, users, branches, paths, versions)
   are never kept, and an unknown program whose name has digits is not a
   step. Another tool becomes `tool:<name>`. Generic file tools (read, edit,
   grep, ...) are not steps. Parallel tool calls are matched to their results,
   so a tool result never opens an episode.
3. **Procedures.** Episodes are clustered in a fixed order by half request
   words, half step signatures (`--similarity`, 0.4; no shared step, no
   match). A cluster must span `--min-sessions` (3) distinct sessions.
4. **Verify-first.** A step is kept only when evidence turns for it exist in
   at least `--min-sessions` of those sessions; other steps are dropped and
   listed as `dropped_steps`. Fewer than `--min-steps` (2) kept steps, no
   candidate.

Each candidate is staged under `_candidates/skills/<candidate_id>/`:

- `SKILL.md`: frontmatter `name` (lower-case, hyphenated, at most 64
  characters) and `description`, then *When to use*, *Steps* (each citing its
  step ids and session count) and *Evidence*;
- `evidence.json`: sessions (source, session id, agent), agent counts, and
  per-step signatures, session counts and refs (`memfw history read REF`);
- `candidate.json`: the proposal record: `status: proposed`,
  `approved: false`, `installed: false`, `confidence` (0..1, from step
  support, repetition, agent breadth and the share of steps kept).

Keywords (for the name and *When to use*) are request words found in at
least half of the episodes; filler words and anything with digits (host or
ticket names) are left out.

The candidate id is a hash of the steps, their refs and the sessions: a
rerun over the same evidence reports `exists` and rewrites nothing. New
evidence gives a new id with the same `procedure_key`; while a candidate for
that procedure is staged, the new one is reported as `known` (with the staged
path) and not staged again. Its sessions, agents and per-step counts are
appended once to the staged candidate's `evidence.json` under
`later_evidence` (SKILL.md is not rewritten), so a reviewer sees the latest
support. Remove or apply the staged one to restage.

A candidate that cannot be staged (for example, a disk error) is reported
with `status: error` and its message; the others are still staged and
reported, `report.staging_errors` counts the failures and the CLI exits 1.

**Private terms.** Words an operator never wants in a candidate are listed in
`vault.yaml`:

```yaml
skills_distill:
  private_terms: [projectname, hostname]
  private_terms_file: /path/outside/the/repo/private-terms.txt  # optional
```

The file holds one term per line (blank lines and `#` comments ignored); a
relative path is resolved against the vault root. Matching is
case-insensitive and whole-word. Each term becomes `<private>` in every field
of a candidate (SKILL.md, evidence, record; `private` in the skill name),
keywords that are private terms are left out, and the drafter only sees
masked input. The default list is empty. Nothing is installed
into any skills directory, staging inside a `.<agent>/skills` directory is
refused, and nothing is approved: a person copies a reviewed SKILL.md into
place.

All candidate text goes through the history redactor, URLs and paths become
`<url>` / `<path>`, and a candidate that still carries a secret or a home path
is dropped (`dropped_unsafe`).

**Optional drafter.** `--drafter module:function` names a local hook (for
example a model wrapper) that rewords the draft. It receives keywords, the
verified steps (`id`, `signature`, `sessions`) and counts, never transcript
text, and returns `name`, `description`, `when_to_use` and `steps`
(`{"text", "evidence": [step ids]}`). Its output is verified like evidence:
a step that cites no verified step id, does not name a program or tool of a
step it cites, only repeats evidence already cited, or whose text is unsafe,
is dropped;
with fewer than `--min-steps` steps left, or on any error, the deterministic
draft is kept. memfw ships no drafter and makes no network calls.

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
- Curate matches words, not meanings: synonyms ("deploys" / "deployments are
  scheduled") are not recognised as duplicates.
- Distill (③) is a stub in `dream run`; `dream skills` covers repeated
  procedures only (no lessons). Promote/demote (④) covers the budget check only.
- `dream skills` matches steps by command signature: the same procedure done
  with different tools, or a step hidden in prose, is not recognised, and
  arguments are not part of a step. Only the first 500 index entries of a
  session are read, and only the newest `--max-sessions` (200) sessions.
- Request keywords are redacted and digit-free, but a plain-word private term
  (a project or host name without digits) can still appear in a candidate
  unless it is listed under `skills_distill.private_terms`.
- Frequency-based scheduling ("often used, long unverified first") is limited
  to ordering by `reference_count`.
