# Pipeline runner: `memfw-pipeline`

`packages/memfw-pipeline` (import `memfw_pipeline`, version 0.1.0) is the Stage contract and the
runner for the derived artifacts of a memfw deployment: an admitted view, a metadata index, an
FTS / bm25s / Qdrant index, a projection. A stage implements one small protocol; the runner
gives every stage the same lock, atomic swap, shrink guard, status file and freshness report.
memfw never imports it.

| | |
|---|---|
| Distribution | `memfw-pipeline` 0.1.0, Apache-2.0, wheel only, not published to PyPI |
| Runtime dependencies | the standard library and `memfw-ports` **0.3.0** (the `Clock` port and the name rule) |
| Python | 3.11 or newer (`tomllib`) |
| Entry points | `memfw-pipeline` (console script), `python -m memfw_pipeline` |
| Coupling | none to memfw: a stage is loaded from the config file by `module:callable` |

## 1. Why

Every derived artifact needs the same mechanics: one writer at a time, build somewhere the
readers cannot see, check the result, swap it in atomically, refuse a build that suddenly lost
most of its items, record what happened without leaking names or text, and say how far the
artifact lags its source. Written by hand in each builder, those copies drift: one forgets the
fsync, another follows a planted symlink, a third publishes an empty index after a mount
failed. `memfw-pipeline` holds the mechanics once; a stage only says what its inputs are, how
to build, and how to check the build.

## 2. The Stage contract

`memfw_pipeline.contract.Stage` is a `runtime_checkable` protocol. The runner calls it in a
fixed order:

```text
freshness(ctx) -> Freshness             which source revision, how old (informative)
prepare(ctx) -> Prepared                optional; replaces inputs_digest when present
inputs_digest(ctx) -> str               digest equal to the live artifact's record -> skip
build(ctx, workdir) -> BuildResult      into a fresh directory beside the target
verify(ctx, workdir, result)            raise VerifyFailed to keep the live artifact
publish(ctx, workdir) -> method         atomic swap
post_publish(ctx) -> [PostPublishStep]  idempotent; also runs when inputs are unchanged
```

Required members: `name` (the stage kind, the ports name rule `^[a-z][a-z0-9_-]{0,63}$`, for
example `admitted-view`), `inputs_digest`, `build`, `verify`, `publish`, `freshness`. Optional,
looked up with `getattr`:

- `post_publish(ctx) -> [PostPublishStep]`;
- `live_count(ctx) -> int | None`: items in the live artifact (`None` when there is none). It
  is checked against the artifact record's `items` before a build is skipped, and it is a
  shrink baseline when there is no record (section 4). A stage's own `live_count` must not
  count the record file (`.memfw-pipeline.json`); `DirectoryStage` skips it;
- `prepare(ctx) -> Prepared`: when present it is called **instead of** `inputs_digest`.
  `Prepared(digest, data)` carries the digest (same rule as below) and whatever the stage read
  to compute it; the runner hands `data` to `build` (and to post-publish steps) as
  `ctx.prepared`, so the inputs are read once. `data` is never recorded. Anything else than a
  `Prepared` is `inputs:bad_prepared`, exit 1;
- `status_formats: Mapping[str, str]`: the extra status keys the stage may report in
  `BuildResult.extra`, each mapped to a format name from `EXTRA_FORMATS` (below).

| Type | What it carries |
|---|---|
| `StageContext` | `stage` (configured name), `target` (live artifact directory), `state_dir` (the runner's status directory; a stage does not write there), `options` (read-only mapping from the config), `clock`, `prepared` (the `data` of `prepare`, else `None`) |
| `BuildResult` | `counts` (must carry `items`, which the shrink guard compares), `artifacts` (relative paths inside the work directory that must exist; only their number is recorded) and `extra` (at most 16 keys; more is a `ValueError`, so the build fails) |
| `StepResult` | `counts` of one post-publish step |
| `PostPublishStep` | `name` (a code), `run(ctx) -> StepResult`, `attempts` 1..5 (retries), `timeout_seconds` (optional, (0, 86400]) |
| `Prepared` | `digest` (lowercase hex, 16 to 128) and `data` (anything; handed to `build` as `ctx.prepared`) |
| `Freshness` | `revision` (an id: commit, generation, digest prefix; never a name; see `revision_ok` below) and `as_of` (timezone-aware, or `None`) |

`inputs_digest` must return lowercase hex of 16 to 128 characters (else `inputs:bad_digest`,
exit 1); `digest_of(*parts)` gives a sha256 over length-prefixed parts (unambiguous
concatenation).

**Revisions.** `revision_ok(value)`: the value matches `^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$`
(`REVISION_RE`) **and** contains nothing that could be an address: a bare dotted quad (four
dot-separated groups of one to three digits not preceded by a letter or digit, such as
`1.2.3.4` or `host:1.2.3.4`) or a dotted host name (a dot between two labels that start with a
letter, such as `files.example`). Revisions are git-like ids: a commit hash, a tag, a generation
number, `sha256:<hex>`. `v1.2.3`, `v1.2.3.4` (write a four-part version with a leading `v`),
`release-2026.11` and `0123abcd` pass. `Freshness` refuses anything else with `ValueError`, and
`clean_status` turns it into `redacted`.

**Extra status fields.** A build may report a few non-count facts (an embedding model name, a
vault id) in `BuildResult.extra`. The runner keeps a key only when it is a code, the stage
declares it in `status_formats`, and the value matches the declared format:

| Format | Value |
|---|---|
| `code` | `^[a-z][a-z0-9_]{0,63}$` |
| `sha256` | 64 lowercase hex characters |
| `uuid` | a lowercase hyphenated UUID |
| `model` | `name`, `org/name`, either with `:tag`: `^[a-z0-9][a-z0-9._-]{0,63}(/[a-z0-9][a-z0-9._-]{0,63})?(:[a-z0-9._-]{1,32})?$`, and no dot between two labels that start with a letter (so no host name such as `files.example.lan`, no file name such as `notes.md`, no path with more than one `/`) |
| `int` | a non-negative int, or a digit string (`0`, or up to 18 digits without a leading zero) |

Any other key or value is dropped and the run gets the warning `extra_rejected` (the build
still publishes). Free text is never a format. Kept fields go into the status and the artifact
record as `extra` plus `extra_formats` (key to format name), so an `unchanged` run reports them
too, and every later re-check of the status (`clean_status`, `memfw-pipeline status`) tests
each value against the format recorded for its own key, not against any format. Only the
top-level `extra` key gets this rule: a post-publish step named `extra` is an ordinary entry.

**Post-publish step budget.** The runner cannot stop in-process code, so `timeout_seconds` is
the step's own deadline: the step enforces it (`subprocess.run(timeout=...)`, an HTTP client
timeout). The runner records each step's `duration_ms` (all attempts together) and sets
`over_budget: true` when that exceeds `timeout_seconds * attempts`; a step over budget that
succeeded still counts as ok.

**Fixed vocabulary.** Count keys, reason codes and step names match `^[a-z][a-z0-9_]{0,63}$`
(`CODE_RE`); counts are non-negative ints, at most 32 keys per build or step. They are a fixed
vocabulary of the stage, like an enum: a stage must never build a count key or a reason code
from its data (a file name, a tag, a tenant).

**Errors.** A stage raises a `StageError` subclass with a reason code. Only the code reaches the
status file (an invalid code becomes `other`); the message is never written or printed, so it
may name things.

| Exception | Typical exit | Meaning |
|---|---|---|
| `InputUnavailable` | 1 (from `prepare` / `inputs_digest`) | the inputs cannot be read; the live artifact is kept |
| `SetupError` | 2 (from the factory, `prepare` or `inputs_digest`) | configuration or environment problem |
| `VerifyFailed` | 3 (from `verify`) | the new artifact failed verification; the live artifact is kept |
| `StageError` | 3 (from `build` / `publish`) | any other stage failure |

The runner picks the exit code by where the exception is raised: anything (a `StageError` or
any other exception) raised by `prepare` or `inputs_digest` exits 1 unless it is a `SetupError`; anything raised by `build`,
`verify` or `publish` exits 3. A non-`StageError` exception is recorded by its type name only.

**Reserved name.** `.memfw-pipeline.json` (`ARTIFACT_RECORD`) at the top of the work directory
belongs to the runner (section 4). A build that creates it (file, directory or symlink) fails
with `verify:reserved_name`, exit 3.

**`DirectoryStage`** is a base for stages whose artifact is one directory. A subclass implements
`inputs_digest`, `build` and `freshness` and may set `count_glob` (relative to the artifact, for
example `items/*.md`). It supplies:

- `verify`: every declared artifact exists and is not a symlink (`artifact_missing`); with
  `count_glob`, the work directory holds exactly `items` matching regular files
  (`count_mismatch`);
- `publish`: `fsops.swap_in` (section 3);
- `live_count`: regular, non-symlink files matching `count_glob` in a real target directory,
  the record file excluded; `None` without `count_glob` or without a live artifact;
- `post_publish`: no steps.

## 3. One run, step by step

`memfw-pipeline run STAGE --config FILE` (or `run_stage(spec, RunOptions(...))`):

1. **Trust check** (when trust is on, section 9). On failure it exits 2 and writes nothing, not
   even the status file: nothing is written into a tree another user could have prepared.
2. **Directories.** `<state_dir>/<stage>` is created with `pipeline.state_mode` (default 0700),
   every missing parent with the same mode (and `chmod` to it if it already exists; a symlink
   there is refused). The target's parent is created 0700 when missing. Failure: exit 2,
   `state_dir_unusable`. With trust on, the trust check runs **again** now that the
   directories exist.
3. **Lock.** `.<target>.lock` in the target's parent (for target `/srv/memfw/derived/example`:
   `/srv/memfw/derived/.example.lock`), so two configs naming one target share one lock. It is
   opened with `O_NOFOLLOW` and taken with a non-blocking exclusive `flock`, retried every
   0.1 s for `RunOptions.lock_wait_seconds` (default 5 s; the CLI uses the default). A `plan`
   or `status` probe holds it for an instant, so a probe never makes a scheduled run give up.
   Still held after the wait: exit **75**, nothing done, nothing written (the JSON line says
   `result: busy`, `ok: true`). A planted symlink or an unopenable lock: exit 2,
   `lock_unusable`. The kernel drops the lock when the process dies, so a killed run never
   wedges the next. **`flock` is local to one host**: on NFS it may be a no-op (`nolock`) or not
   exclude a run on another host. Keep one writer host, and one target per writer.
4. **`running` status.** Written at once, carrying the last good fields from the previous status
   (`last_items`, `last_success_at`, `last_built_at`, `published_digest`, `published_revision`,
   `swap_method`). A run killed by the service manager therefore leaves `running` with the
   live artifact still described. A status file that exists but cannot be read adds the warning
   `status_unreadable`.
5. **Crash leftovers.** When the target itself is missing but `.<target>.old-*` directories
   exist (a crash, or a failed rollback, between the two renames of the fallback swap):
   - exactly one: it is renamed back into place (warning `previous_artifact_restored`);
   - more than one: **refused**, exit 2, `restore_ambiguous`. A half-removed copy can look
     newest and names do not order across reboots, so an operator decides;
   - the rename fails (or the result is not a real directory): refused, exit 2,
     `restore_failed`.

   After a refusal no `.old-*` entry is removed: one of them may be the only good copy. Fix it
   by hand (rename the right copy to the target, remove the others), then run again. Otherwise
   `.<target>.new-*` and `.<target>.old-*` entries beside the target (from a crashed run; they
   may hold a full copy of the data) are removed under the lock; `leftovers_removed` counts
   them.
6. **Stage factory, freshness, digest.** The factory is imported (`factory_unloadable`,
   `factory_failed`, `not_a_stage`: exit 2). A failing `freshness` is only a warning
   (`freshness_unavailable`); the digest decides. The digest comes from `prepare` when the stage
   has it, else from `inputs_digest`. A bad digest is `inputs:bad_digest`, exit 1.
7. **Unchanged?** When not `--force`, the **live artifact's own record** (section 4) holds the
   same inputs digest, and `live_count` is `None` or equals the record's `items`, the build is
   skipped. The status file is not consulted for this decision. The status gets `counts`,
   `last_items`, `published_digest` and `extra` from the record, and `published_revision` from
   the current freshness (when available). Post-publish steps **still run**, so a wiped
   downstream index heals on the next run. Result `unchanged`.
8. **Shrink baseline** (section 5).
9. **Build.** A work directory `.<target>.new-<pid>-<ns>` (mode 0700) is made **beside the
   target**, on the same filesystem, so the swap is a rename. `build` fills it; then `verify`.
   Any failure removes the work directory and exits 3 (`build_failed` / `verify_failed`). The
   extra fields are then filtered (`extra_rejected`, section 2).
10. **Shrink guard** (section 5): refused builds are removed, exit 5.
11. **Record, fsync, `publishing`, publish.** The runner writes the artifact record into the work
    directory (`verify:reserved_name` if the stage already created that name), then fsyncs every
    regular file and directory there (symlinks are neither followed nor synced); a failure of
    either removes the work directory and exits 3 as `verify_failed`. Next a `publishing` status
    with `publishing_digest` (the digest being published) is written, so a run killed from here
    on is known to have been publishing. Then `publish` runs and the target's parent is fsynced.
    `DirectoryStage.publish` swaps real directories, never a symlink, so readers that key on the
    resolved path see one stable directory:
    - `rename`: no live artifact yet;
    - `exchange`: one atomic exchange, `renameat2(RENAME_EXCHANGE)` on Linux,
      `renamex_np(RENAME_SWAP)` on macOS; the parent is fsynced, then the old artifact (now in
      the work directory) is removed;
    - `rename_pair`: the fallback when the platform, kernel or filesystem cannot exchange
      (`ENOSYS`, `EINVAL`, `ENOTSUP`, `EOPNOTSUPP`, `EXDEV`): target to `.<target>.old-*`, work
      directory to target; if the second rename fails the old artifact is renamed back. After
      both renames the parent is fsynced before the old copy is removed.

    A target that is a symlink or not a directory is refused. Any publish failure keeps the old
    artifact live and exits 3 (`publish_failed`). A method outside these three is recorded as
    `custom`. On success the status gets `last_items`, `last_built_at`, `published_digest`,
    `swap_method` and `published_revision` (the build's source revision; dropped when freshness
    was unavailable).
12. **Post-publish steps** run in order; each failing step is retried up to its `attempts`, and
    each gets `duration_ms` (and `over_budget`, section 2). `last_success_at` is set whether or
    not they pass (the artifact is live and current either way); a failed step exits 4
    (`post_publish_failed`) with `<step>:<code>` in `errors`.
13. **Final status** (section 6) is written and printed as one JSON line.

| Exit | Meaning |
|---|---|
| 0 | built, or inputs unchanged (post-publish steps ran) |
| 1 | inputs unavailable; the live artifact is kept |
| 2 | setup error (config, untrusted path, stage factory, state directory, lock, a crash leftover the runner will not resolve: `restore_ambiguous`, `restore_failed`) |
| 3 | build, verify or publish failed; the live artifact is kept |
| 4 | published (or unchanged) but a post-publish step failed |
| 5 | shrink refused |
| 75 | another run holds the lock: nothing was done; a unit counts it as success |

`memfw-pipeline plan STAGE` reports what `run` would do (`build`, `skip_unchanged`,
`inputs_unavailable`, `setup_error`) with the digest, `live_count`, `artifact_items` (the
record's `items`, or `null`), whether the lock is held, the number of post-publish steps and
freshness. It decides `skip_unchanged` from the artifact record exactly as `run` does. It writes
nothing and only probes the lock for an instant; it exits 2 on `setup_error`, else 0. Note that
`plan` runs `prepare` / `inputs_digest`, so it reads the inputs once.

## 4. The artifact record

Every artifact the runner publishes carries its own record, `.memfw-pipeline.json` at its top
level (mode 0600, written through an `O_EXCL | O_NOFOLLOW` temporary file and passed through
`clean_status`):

```json
{"schema": 1, "inputs_digest": "<hex>", "counts": {"items": 42, "skipped": 0},
 "extra": {}, "built_at": "2026-01-01T03:30:00Z"}
```

It is written into the work directory after `verify` and the shrink guard, before the fsync and
the swap, so it moves with the artifact in the same rename. Whether a build may be skipped is
decided from the **live** artifact's record (and `live_count`), never from the status file. It
is read with `O_NOFOLLOW`; a missing, symlinked or malformed record (wrong `schema`, a digest
that is not lowercase hex, `counts` without an `items` int) reads as none, and the next run
builds.

**Why.** The status file is written after the swap. A run killed between the swap and its final
status write (the service manager's timeout, a power cut) leaves the status describing the
*previous* artifact while the new one is live, or the other way round with a status that
claims a digest the live artifact was not built from. If the skip decision trusted the status,
a later run could see a matching `published_digest` and skip forever over an artifact that does
not match its inputs. With the record inside the artifact, the digest and the bytes change in
one rename: whatever is live describes itself. The `publishing` status written just before the
swap (with `publishing_digest`) tells an operator that such a run was killed mid-publish.

An artifact published before the record existed (or by another tool) has no record, so the
first run under this runner rebuilds it once.

**The record is visible inside the artifact.** Readers of the published directory see
`.memfw-pipeline.json` at its top level. A stage that walks the artifact (`**/*`, `os.walk`) in
`live_count`, `verify` or a post-publish step, and any downstream reader that indexes every
file, must skip it. `DirectoryStage` already leaves it out of `count_glob` counts.

## 5. Shrink guard

Without `--allow-shrink`:

- an **empty build** (`items == 0`) is refused (`empty_build`). With the stage key
  `empty_build = "refuse"` (the default) that holds always, first run included; with
  `empty_build = "refuse_if_shrinking"` only when the baseline is known and above zero, for a
  source that may legitimately be empty;
- a build with `items < baseline * 0.5` (`SHRINK_FLOOR`) is refused (`shrink_refused`) when the
  baseline is known and above zero; **exactly half passes**.

The baseline, first match wins (`baseline_source` in brackets):

1. the live artifact record's `items` (`artifact`);
2. `last_items` from the status, when the target exists as a directory (`status`);
3. the stage's `live_count` (`live`);
4. `last_items` from the status even without a target (`status`): a target that vanished
   does not reset the guard;
5. none (`none`): only the empty-build rule applies.

A refusal exits 5 with `result: shrink_refused`, the work directory removed and the live
artifact untouched. The status records `shrink: {baseline, baseline_source, would_publish}`;
with `--allow-shrink` it adds `allowed: true` and nothing is refused. Use it once, by hand,
after checking that the smaller artifact is right.

## 6. Status file

`<state_dir>/<stage>/status.json`, mode 0600 (0640 / 0644 with `state_mode` 0750 / 0755,
section 8), written through an `O_EXCL | O_NOFOLLOW`
temporary file, fsynced and renamed into place (then the directory is fsynced). It is read
back with `O_NOFOLLOW` (at most 1 MiB); a missing, symlinked, corrupt or wrong-`schema` file
reads as none.

Fields: `schema` (1), `runner_version` (2), `stage`, `kind`, `result`, `ok`, `exit_code`,
`started_at`, `finished_at`, `duration_ms`, `inputs_digest`, `counts`, `artifacts` (a number),
`extra` (section 2), `shrink`, `post_publish` (per step: `ok`, `attempts`, `duration_ms`,
`over_budget` when set, `counts`, or `error` and an exception type name), `errors`, `warnings`,
`leftovers_removed`, `artifact_present`, the carried fields of section 3 step 4, and
`freshness` (section 7).

`result` is `running` or `publishing` while a run is in flight (a killed run leaves one of
them; `publishing` adds `publishing_digest`), and at the end one of `built`, `unchanged`,
`busy` (printed only, not written), `inputs_unavailable`, `setup_error`, `build_failed`,
`verify_failed`, `shrink_refused`, `publish_failed`, `post_publish_failed`.

**The second fence: `clean_status`.** The runner only puts counts, codes, digests, revisions,
timestamps and booleans in the status itself; every payload still passes `clean_status` before
it is written or printed, so a stage bug can never put a file name or text there. Every key must
be a code. Strings must have the shape their key allows:

| Key | Shape |
|---|---|
| `*_at`, `source_as_of` | `YYYY-MM-DDTHH:MM:SSZ` |
| `stage`, `kind` | the ports name rule |
| `inputs_digest`, `published_digest`, `publishing_digest` | lowercase hex, 16 to 128 |
| `source_revision`, `published_revision` | `revision_ok` (no IPv4 address, no dotted host name) |
| `errors`, `warnings` entries | `code` or `prefix:code` |
| `exception` | a type name (`^[A-Z][A-Za-z0-9_]{0,63}$`) |
| `runner_version` | digits |
| top-level `extra` values | the format recorded for that key in `extra_formats` |
| anything else | a code (`CODE_RE`) |

A string of the wrong shape, and any value that is not a string, number, boolean, null, list or
mapping, becomes `redacted`; floats are rounded to three places. The status never holds item
names, paths or text.

**Limit of the fence.** A lowercase slug (`alice_smith`, `project_x`) **is** a valid reason
code, so `clean_status` cannot catch a stage that builds codes or count keys from its data.
The first fence is the contract: codes and count keys are a fixed vocabulary of the stage
(section 2). Review a stage for that; the runner cannot.

## 7. Freshness

Each status and `memfw-pipeline status` carry `freshness`:

| Field | Meaning |
|---|---|
| `source_revision`, `source_as_of` | what the stage's `freshness` reports now (`null` when unavailable) |
| `published_revision` | the revision the live artifact was built from |
| `behind` | the live artifact was built from another revision than the current one |
| `lag_seconds` | 0 when not behind; else the time since the current revision was made (a lower bound; `null` when the source gives no time) |
| `last_success_age_seconds` | time since the last successful run (`null`: never) |
| `max_lag_seconds` | the stage's threshold (config, default 86400) |
| `stale` | see below |

`stale` is true when nothing was ever published, there was never a successful run, the last
successful run is older than `max_lag_seconds` (the timer stopped), or `lag_seconds` exceeds
it.

`memfw-pipeline status [STAGE] --config FILE` prints `{"stages": [...]}`: the last status of
each stage (all of them without `STAGE`) with freshness recomputed now and whether the lock is
held. It reads the source for its current revision unless `--no-probe` (or the trust check
fails), in which case the revision recorded in the last status is used. It exits **0 when it
lists at least one stage and every one is `ok` and not stale**, else 1; a stage that never ran is `never_run`, not ok.

## 8. Config file

TOML, or JSON when the file name ends in `.json`. `--config` itself may be relative (it is
resolved against the working directory); every path inside must be absolute and free of `..`.
Unknown keys are rejected at every level, so a typo fails at startup instead of silently
changing behaviour.

```toml
[pipeline]
state_dir = "/srv/memfw/pipeline-state"    # status, one directory per stage
state_mode = "0700"                        # or "0750" / "0755" for a non-root monitor

[stages.example]
factory = "memfw_pipeline.example:factory" # module:callable(options) -> Stage
target = "/srv/memfw/derived/example"      # the live artifact directory
max_lag_seconds = 86400                    # freshness threshold, 60 s to 365 days
trust_paths = true                         # unset: on exactly when run as root
empty_build = "refuse"                     # or "refuse_if_shrinking"
options = { source = "/srv/memfw/source" } # passed to the factory, read-only to the stage

[stages.example.unit]                      # only `memfw-pipeline unit` reads this (section 10)
schedule = "*-*-* 03:30:00"
```

| Key | Required | Rule |
|---|---|---|
| `pipeline.state_dir` | yes | absolute |
| `pipeline.state_mode` | no | `"0700"` (default), `"0750"` or `"0755"`: the mode of the stage state directories (and missing parents); the status file gets 0600, 0640 or 0644. Use 0750 with a group for a non-root monitor that reads `status.json` |
| `stages.<name>` | | `<name>` follows the ports name rule; it is the CLI's `STAGE` |
| `factory` | yes | `module.path:callable` |
| `target` | yes | absolute; never the same as, inside, or containing the state directory or another stage's target (a swap would carry the status or another artifact with it). Compared as written **and** resolved (`realpath`) |
| `options` | no | table, default empty |
| `max_lag_seconds` | no | int, 60 to 31536000, default 86400 |
| `trust_paths` | no | bool; unset means on exactly when the runner runs as root (effective uid 0); an explicit `true` / `false` is honoured |
| `empty_build` | no | `"refuse"` (default) or `"refuse_if_shrinking"` (section 5) |
| `unit` | no | table (section 10) |

Config errors exit 2 with a fixed code: `config_unreadable`, `config_invalid`,
`state_dir_invalid`, `stage_name_invalid`, `stage_config_invalid`, `factory_invalid`,
`target_invalid`, `max_lag_invalid`, `state_mode_invalid`, `empty_build_invalid`,
`unknown_stage`.

The overlap check cannot see every alias: on a case-insensitive filesystem (the macOS default)
`/srv/memfw/Derived` and `/srv/memfw/derived` are one directory but neither the lexical nor the
`realpath` comparison says so. Spell each path one way.

## 9. Path trust (`trust_paths`)

For a stage run as root, a path another user can write would let that user swap the artifact,
the config or the stage code under the runner. Trust is on when `trust_paths = true`, or when
`trust_paths` is unset and the runner's effective uid is 0; `trust_paths = false` turns it off
even for root. With trust on, `run` and `plan` check these paths before anything else (and stop
with exit 2 / `action: setup_error`); `run` checks again after it has created the state
directory and the target's parent; `status` then does not load the stage to probe the source:

- the config file, the stage's state directory, the target (and so its parent, where the lock
  and the work directories live);
- every level of the stage factory's dotted module, top-level package first
  (`a`, then `a.b`, then `a.b.c`): each package directory and its `__init__.py` (or the module
  file), found with `importlib.util.find_spec`. `find_spec` of a dotted name imports its
  parents, so the top-down order means only code already judged trusted is imported;
- the runtime: `memfw_pipeline` itself and, when importable, `memfw_ports` and `memfw`, every
  level the same way.

A path is trusted when it and every existing ancestor is not a symlink, is owned by root or the
current user, and is not group- or other-writable. Missing components (not created yet) are
judged by the ancestors that exist. One exception: a root-owned ancestor **directory** with the
sticky bit (`/tmp`) may be world-writable; the path itself never may. Failures are fixed codes:
`config_untrusted`, `state_dir_untrusted`, `target_untrusted`, `factory_untrusted`,
`runtime_untrusted`.

Limits:

- **Paths are taken as written; no symlink in any component**, the ancestors included. A path
  through a symlinked directory fails the check even when the link is root-owned. On macOS
  `/var` and `/tmp` are symlinks into `/private`, so `/var/...` or `/tmp/...` fails: configure
  real paths (`/private/var/...`), resolved once by hand.
- The module check covers **package directories and their `__init__.py`**, not every file in
  them: a group-writable module file inside a trusted package directory is not caught. Nor are
  the interpreter, the virtual environment's other packages, or the paths in `options`. Keep
  the whole environment (for example `/opt/memfw/venv`) root-owned and not writable by others.

## 10. Service-manager units

`memfw-pipeline unit STAGE --config FILE --systemd|--launchd [--python /opt/memfw/venv/bin/python]`
prints a template to **review**; the package never installs anything. The command line is
`<python> -I -m memfw_pipeline run <stage> --config <config>` (`-I`: isolated mode, no user site
directory, no current-directory imports, no `PYTHON*` variables). The interpreter is `--python`,
else `unit.python`, else the running interpreter.

Settings (`[stages.<name>.unit]`; unknown keys rejected; values may not hold whitespace, quotes
or control characters, so the generated lines need no quoting):

| Key | Used by | Default |
|---|---|---|
| `python` | both | the running interpreter |
| `description` | systemd | `memfw pipeline: <stage>` |
| `user` | systemd `User=` | omitted (root; needs `trust_paths = true`, below) |
| `schedule` | systemd `OnCalendar=` | `daily` |
| `randomized_delay_seconds` | systemd `RandomizedDelaySec=` | 600 |
| `timeout_seconds` | systemd `TimeoutStartSec=` | 3600 |
| `capabilities` | systemd `CapabilityBoundingSet=` (and `AmbientCapabilities=` with `user`) | none |
| `read_only_paths`, `read_write_paths` | systemd | none (the state directory and the target's parent are always read-write) |
| `environment` | both | none; proxy variables are refused |
| `launchd_label` | launchd `Label` | `org.memfw.pipeline.<stage>` |
| `launchd_hour`, `launchd_minute` | launchd `StartCalendarInterval` | 3, 30 |
| `launchd_interval_seconds` | launchd `StartInterval` (instead of hour/minute) | |
| `log_dir` | launchd `StandardOutPath` / `StandardErrorPath` | none |

**Root units need trust.** A systemd unit without `user` runs as root, and a launchd template
always does as a LaunchDaemon (it sets no `UserName`). For those, `unit` refuses with
`unit_requires_trust_paths` unless the stage sets `trust_paths = true` **explicitly** (the
root default of section 9 does not count, so the reviewed config states it), or the command is
given `--allow-untrusted`.

**systemd** (`memfw-pipeline-<stage>.service` and `.timer`, one text with file headers):
`Type=oneshot`, `SuccessExitStatus=75`, `UMask=0077`, `Nice=10`, `IOSchedulingClass=idle`;
`UnsetEnvironment=` the proxy variables and `NO_PROXY=127.0.0.1,localhost,::1`; loopback network
only (`IPAddressDeny=any`, `IPAddressAllow=localhost`,
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`); `InaccessiblePaths=` the docker
(`/run/docker.sock`, `/var/run/docker.sock`), containerd (`/run/containerd/containerd.sock`) and
podman (`/run/podman/podman.sock`) sockets, the system bus (`/run/dbus`) and the systemd private
socket (each with `-`, so a missing one is fine; uid 0 with `AF_UNIX` could still `connect()`
to them); `NoNewPrivileges=true`, `CapabilityBoundingSet=` (empty by default) and, with `user`
and `capabilities`, `AmbientCapabilities=` with the same set (a non-root process gets nothing
from the bounding set alone), `ProtectSystem=strict` with `ReadWritePaths=` the state
directory, the target's parent (lock and work directories) and `read_write_paths`,
`ReadOnlyPaths=`, `ProtectHome=read-only`, `PrivateTmp`, `PrivateDevices`,
`ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`, `ProtectKernelLogs`,
`ProtectClock`, `ProtectHostname`, `LockPersonality`, `RestrictSUIDSGID`, `RestrictRealtime`,
`RestrictNamespaces`, `SystemCallArchitectures=native`, `SystemCallFilter=@system-service`
with `SystemCallErrorNumber=EPERM` (a filtered call fails with `EPERM` and the stage reports an
error code, instead of the kernel killing the run with `SIGSYS`);
`RequiresMountsFor=` the state directory, the target's parent and the read-only paths. The timer has `OnCalendar`, `RandomizedDelaySec`, `Persistent=true`. Exits 1 to
5 are failures, so `systemctl --failed` shows them; `status.json` says which. Create the stage's
state directory and the target's parent before enabling the timer: systemd does not start a unit
whose `ReadWritePaths=` or `ReadOnlyPaths=` entries do not exist. The reference output is in
`tests/fixtures/pipeline/admitted-view.systemd.txt` (with `user` and capabilities) and
`admitted-view.systemd-defaults.txt` (defaults, root).

**Capabilities.** With the default (none), a stage running as root still cannot read files it
does not own; add `CAP_DAC_READ_SEARCH` to read and list them. Some filesystems with an extra
ACL layer on top of the mode bits refuse that alone for another user's 0700 directories; there
`CAP_DAC_OVERRIDE` is needed for **reads**. Writes outside `ReadWritePaths` stay impossible
either way, because `ProtectSystem=strict` makes the rest read-only. Check the first run
(`journalctl -u`): an empty build caused by unreadable inputs is refused by the shrink guard.

**launchd**: `Label`, `ProgramArguments`, `ProcessType=Background`, `LowPriorityIO`, `Nice=10`,
`Umask` 077, `RunAtLoad=false`, `EnvironmentVariables` (`NO_PROXY` plus `environment`),
`StartCalendarInterval` or `StartInterval` (setting both is refused), and the log paths when
`log_dir` is set (reference: `tests/fixtures/pipeline/admitted-view.launchd.plist`). launchd
has no equivalent of the systemd sandbox keys; they are not emitted. Two more launchd limits:
there is **no per-run timeout** (a hung stage runs until killed by hand; the next start finds the
lock held and exits 75), and the template sets **no non-root user** (no `UserName`): as a
LaunchDaemon it runs as root, as a LaunchAgent as the logged-in user.

Unit errors exit 2: `unit_invalid`, `unit_path_invalid`, `unit_env_invalid`,
`unit_capability_invalid`, `python_invalid`, `config_path_invalid`, `unit_kind_invalid`,
`unit_requires_trust_paths`.

## 11. The example stage

`memfw_pipeline.example:factory` builds `CopyStage` (kind `example-copy`), the reference the
tests drive and a template, not a production stage. It copies a flat directory of `*.md` files
to `<target>/items/` and writes `<target>/manifest.json` (`{"items": N}`); `count_glob` is
`items/*.md`. Options: `source` (required, absolute; symlinks and other files are counted as
`skipped`), `revision_file` (optional: its first line is the revision, its mtime `as_of`;
without it the revision is a digest prefix and `as_of` the newest file's mtime), `marker`
(optional: an idempotent post-publish step `write_marker` rewrites it with the live item count,
a stand-in for an adapter sync; it writes a unique temporary file opened with
`O_CREAT | O_EXCL | O_NOFOLLOW` beside the marker and renames it over the marker, so a symlink
planted at the temporary name is never followed).

```bash
memfw-pipeline plan example --config /srv/memfw/pipeline.toml
memfw-pipeline run example --config /srv/memfw/pipeline.toml
memfw-pipeline status --config /srv/memfw/pipeline.toml
memfw-pipeline unit example --config /srv/memfw/pipeline.toml --systemd
```

### Writing a stage

```python
from pathlib import Path
from memfw_pipeline import (
    BuildResult, DirectoryStage, Freshness, InputUnavailable, StageContext, digest_of,
)

class TitleIndex(DirectoryStage):
    name = "title-index"            # the kind; the config key names the instance
    count_glob = "rows/*.json"      # live_count and verify use it

    def __init__(self, options):
        self.source = Path(options["source"])

    def inputs_digest(self, ctx: StageContext) -> str:
        try:
            files = sorted(self.source.glob("*.md"))
            return digest_of("title-index", "1", *(p.read_bytes() for p in files))
        except OSError:
            raise InputUnavailable("source_unreadable") from None

    def freshness(self, ctx: StageContext) -> Freshness:
        return Freshness(self.inputs_digest(ctx)[:16])

    def build(self, ctx: StageContext, workdir: Path) -> BuildResult:
        rows = workdir / "rows"
        rows.mkdir(mode=0o700)
        ...                         # write one rows/<n>.json per item
        return BuildResult(counts={"items": n, "skipped": skipped}, artifacts=("rows",))

def factory(options):
    return TitleIndex(options)
```

Count keys (`items`, `skipped`) and reason codes (`source_unreadable`) are constants, never
derived from the data. Write only inside `workdir`, and never `workdir/.memfw-pipeline.json`;
`publish`, the record, the lock and the status are the runner's. A stage that reads its inputs
twice (once for the digest, once to build) can implement `prepare` instead of `inputs_digest`
and return `Prepared(digest, data)`; `build` then finds `data` in `ctx.prepared`.

## 12. Packaging

A standalone uv workspace member with its own wheel, not published to PyPI:

```bash
uv build --wheel --package memfw-pipeline
```

At run time it imports only the standard library and the memfw ports (`Clock`, `check_name`):
from `memfw.ports` inside a memfw install, else from the `memfw-ports` 0.3.0 wheel's
`memfw_ports`. memfw never imports `memfw_pipeline`. Stages live in their own packages and are
named in the config by `module:callable`.

## 13. Not in P1

This release is the contract, the runner, the unit templates and the example stage. Porting the
real admitted-view builder onto it, and the other stages (metadata index, FTS / bm25s / Qdrant
indexes, projections), come later, one package per change.

### Open for P1b (porting the deployment view builder)

Now covered by the contract, so P1b does not need its own mechanism for them: extra status
fields (`BuildResult.extra` with `status_formats`), counts and extra across `unchanged` runs
(restored from the artifact record), one read of the inputs (`prepare` / `ctx.prepared`),
post-publish step durations (`duration_ms`, `over_budget`), and the empty-build choice
(`empty_build = "refuse_if_shrinking"`).

Still open:

- **Status location and compatibility.** The deployment view builder writes its own
  `status.json` next to its output, and the existing health check reads that file. The runner
  writes `<state_dir>/<stage>/status.json` instead (and the artifact now carries
  `.memfw-pipeline.json`). P1b either keeps a compatibility writer (a post-publish step that
  writes the old file from fixed fields) or moves the health check to the runner's status path
  (`memfw-pipeline status`). Decide before the switch, so the health check never reads a stale
  file.
- **A step timeout the runner enforces.** `PostPublishStep.timeout_seconds` is advisory for the
  runner (it measures and flags `over_budget`); the step must enforce it. A hard stop would
  need steps that run as subprocesses the runner can kill.
- **First run over an existing artifact.** An artifact without a record is always rebuilt once,
  and the shrink baseline then comes from the status (none for a fresh state directory) or
  `live_count`. A ported stage needs a `live_count` (or `count_glob`) that matches the old
  artifact, or the first run has no baseline beyond the empty-build rule.
- **`last_built_at` and `published_revision` on `unchanged`.** An `unchanged` run takes
  `published_revision` from the current freshness and carries `last_built_at` from the previous
  status; the record's `built_at` is not read back, and the record holds no revision. After a
  run killed mid-publish these two fields can describe the earlier build until the next real
  build.
- **Lock and state for a non-root monitor.** `state_mode` opens the status file to a group,
  but the lock file is created 0600 beside the target, and a target parent the runner creates
  is 0700; a monitor that cannot open the lock reports `locked: true`.
- **One writer host.** `flock` does not exclude across hosts (NFS); two hosts must not run the
  same target.
- **No CLI flag for `lock_wait_seconds`** (the CLI always waits the default 5 s).
