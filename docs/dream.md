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
     shared context, or the same content words once synonyms are folded
     ("Deploys happen on Fridays" / "Deployments are scheduled on Friday");
     the earliest memory is kept. Never across different subjects (staging vs
     production, Alice vs Bob, `service: api` vs `service: worker`) or when a
     clause of one memory negates the same clause of the other;
   - `supersede`: the same attribute (`key: value` lines or `The <thing> is
     <value>` clauses) or the same context with different values; the newer
     memory is proposed to supersede the older one, which would get
     `valid_to`. Both must be about the same subject, and "X" vs "not X" is
     proposed here rather than merged. Nothing is silently overwritten;
   - `mark-stale`: a memory superseded by another but without `valid_to`, or an
     unreferenced, unconfirmed memory not updated for `--stale-days` (180).
     Nothing is deleted.

   How curate compares two memories (no model):
   - **Clauses and negation.** Each clause is read on its own, and a negation
     (`not`, `never`, `no longer`, contractions such as `isn't` and
     `doesn't`) applies to its clause only. "The cache is enabled" vs "The
     cache is not enabled" is a `supersede` ("attribute 'cache' is negated in
     one memory"); "not enabled" and "disabled" say the same thing. A "do not
     ..." clause about something else neither blocks a merge nor flags a
     conflict. "The owner is not alice" vs "The owner is bob" is no conflict.
     Time adverbs around a value (`now`, `still`, `currently`, `today`) are
     not part of it: "enabled" and "enabled now" agree.
   - **Value signatures.** A value is tied to the noun next to it (`port
     8000`, `timeout 30s`, `Postgres 14`), so "port 8000" vs "port 9000" is a
     conflict ("value of 'port' differs") even when the rest of the wording
     differs. Different nouns (`3 members`, `30 days`) are not compared.
   - **Identity keys.** `service`, `name`, `env`/`environment`, `project`,
     `repo`, `cluster`, `account`, `tenant` and `app` name *which* thing a
     memory is about. When they differ the memories are about different
     things, however many other attributes agree (`service: api, owner: alice`
     vs `service: worker, owner: alice` gives no candidate). The same holds
     when the other memory names the thing in prose: `env: prod` vs "The
     staging environment has 3 replicas" are different subjects. The nearest
     word on each side of the noun (past link words such as `for`, `of`,
     `the`, `called`; synonyms folded) is compared with the attribute value,
     and only those words count, so "Prod and staging environment" names
     staging. With no such word ("The service has 3 replicas") nothing is
     decided. Keys such as
     `owner`, `host` and `region` may change and give `supersede`.
   - **Synonyms.** A small fixed table folds wording: deploy / deploys /
     deployments, db / database, prod / production, config / configuration,
     repo / repository, env / environment, app / application, k8s /
     kubernetes, start / begin, happen / occur / scheduled, stop / halt.
     Opposites (enabled / disabled) are never folded.
3. **Distill** (Lesson from at least two independent events). Event memories
   (`type` or a tag of `event`, `incident`, `episode`, `failure`, `fix`,
   `bug`, `outage`, `postmortem` or `observation`) about one concept that
   share content words (at least two, Jaccard >= 0.3) form a cluster; events
   not linked to a concept cluster together. A cluster yields one `distill`
   candidate only with at least `--min-events` (2, never fewer) independent
   events: events whose transcript sessions (the `SOURCE:FILE_ID` of their
   evidence refs, plus refs this run's verify found) do not overlap. Session
   ids are compared in one form (lowercase, without a `conv:` prefix, an
   `@offset`, a `#turn` or a trailing slash). An event that cites no session
   never counts (`report.distill.uncited_events`): nothing shows it is not
   the same session as another note. The
   candidate proposes a new memory under `changes.new_memory` (`type:
   lesson`, a title and draft body from the recurring words and the event
   titles, `status: unconfirmed`, the evidence refs, `about` the concept) and
   lists `events`, `independent_events` and `counterexamples`: memories of the
   concept that share the recurring words but negate them or are disputed, and
   events this run marked disputed. Counterexamples are named in the reason,
   never hidden. A cluster an existing lesson already covers is skipped. The
   draft never contains transcript text, is redacted, and is dropped if it
   still carries a secret. `report.distill` counts clusters, proposals and
   the reasons clusters were rejected. Procedures are distilled into skill
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

**Reader-upgrade order** (unchanged by the M1-3 follow-ups, which add no
memory field and no status value: distill proposals use `type: lesson`,
`status: unconfirmed` and `evidence`, all of which exist):

1. Upgrade every reader first: each memfw install, the MCP server
   (`memfw-mcp`) and fleet-memory's reader, so that unknown keys are ignored
   rather than rejected.
2. Then upgrade the writers (the `memfw` CLI that applies candidates and the
   conversation worker).
3. Only then apply `status` / `evidence` from dream candidates.

Candidate files and `report.json` only gain keys (`report.distill`,
`changes.new_memory`, `candidates_truncated` in MCP responses), so a reader
that ignores unknown keys keeps working. The conversation worker's state file
gains a `scan_cursor` table; an older worker ignores it (see
`docs/conversations-share.md`).

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
`--source-name`, `--since`, `--until`, `--tenant`, `--stale-days`, `--min-events`, `--dream-id`,
`--judge`, `--allow-config-judge`, `--no-refresh`, `--now` (inject the clock).

## MCP tools

The memfw MCP server (`memfw-mcp`) has two dream tools:

- `dream_run(dry_run=true, focus, all, limit, max_sessions, source, since,
  until, stale_days, min_events)`: runs one dream as the server's tenant. It
  is a dry run unless `dry_run` is `false`; then it stages candidates under
  `_candidates/dream/<dream_id>/` and nothing else. It always uses the
  deterministic rules: no judge can be named, and `dream_judge` in
  `vault.yaml` is ignored, because a judge is code that reads transcripts.
  It reads the transcript index read-only and never builds or refreshes it
  (run `memfw history index` from the CLI), so a staged dream writes only its
  own candidate directory. A tenant may have at most 50 staged dreams waiting
  for review, and one server stages at most one dream per 60 seconds; dry
  runs are not limited.
  `limit` is at most 1000 and `max_sessions` 1 to 100. The response returns
  at most 200 candidates (`candidates_truncated` counts the rest; a staged
  dream has them all).
- `dream_list(limit=20)`: this tenant's staged dreams, newest first (id,
  `created_at`, candidate counts). A symlinked dream directory or report is
  skipped and named in `skipped`, never followed.

## Skill candidates

`memfw dream skills` distills procedures that repeat across sessions (and
agents) into SKILL.md drafts. It reads only the redacted snippets of the
transcript index.

```sh
memfw dream skills --root /path/to/vault --dry-run
memfw dream skills --root /path/to/vault --min-sessions 3 --since 2026-01-01
memfw dream skills --root /path/to/vault --from-queue
```

`--from-queue` reads the conversation worker's episode queue instead of the
transcript index (see `docs/conversations-share.md`): the newly queued
episodes plus the newest consumed ones (up to `--max-sessions` sessions), with
turns read through the conversation store adapter. Apps the share policy does
not let the worker process are skipped. A dry run only peeks at the queue; a
real run acks the episodes after staging, or releases them when it fails.
Refs then read `conv:APP:SESSION@OFFSET` (`memfw conversations get`).

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
- Curate matches words, not meanings: only the fixed synonym table is folded,
  and a value is tied to its neighbouring noun only (a value stated far from
  its noun is compared as a plain number).
- Distill drafts a lesson from recurring words and event titles; a person
  writes the actual lesson sentence. Independence is judged by cited
  transcript sessions, not by content; uncited events never support a lesson. Promote/demote (④) covers the
  budget check only.
- `dream skills` matches steps by command signature: the same procedure done
  with different tools, or a step hidden in prose, is not recognised, and
  arguments are not part of a step. Only the first 500 index entries of a
  session are read, and only the newest `--max-sessions` (200) sessions.
- Request keywords are redacted and digit-free, but a plain-word private term
  (a project or host name without digits) can still appear in a candidate
  unless it is listed under `skills_distill.private_terms`.
- Frequency-based scheduling ("often used, long unverified first") is limited
  to ordering by `reference_count`.
