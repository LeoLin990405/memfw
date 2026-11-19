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
inputs_digest(ctx) -> str               unchanged digest -> skip to post_publish
build(ctx, workdir) -> BuildResult      into a fresh directory beside the target
verify(ctx, workdir, result)            raise VerifyFailed to keep the live artifact
publish(ctx, workdir) -> method         atomic swap
post_publish(ctx) -> [PostPublishStep]  idempotent; also runs when inputs are unchanged
```

Required members: `name` (the stage kind, the ports name rule `^[a-z][a-z0-9_-]{0,63}$`, for
example `admitted-view`), `inputs_digest`, `build`, `verify`, `publish`, `freshness`. Optional,
looked up with `getattr`: `post_publish(ctx)` and `live_count(ctx) -> int | None` (items in the
live artifact; `None` when there is none). `live_count` is the shrink guard's fallback when the
status file is missing or corrupt, and the check that the live artifact still holds what the
status says.

| Type | What it carries |
|---|---|
| `StageContext` | `stage` (configured name), `target` (live artifact directory), `state_dir` (the runner's lock and status directory; a stage does not write there), `options` (read-only mapping from the config), `clock` |
| `BuildResult` | `counts` (must carry `items`, which the shrink guard compares) and `artifacts` (relative paths inside the work directory that must exist; only their number is recorded) |
| `StepResult` | `counts` of one post-publish step |
| `PostPublishStep` | `name` (a code), `run(ctx) -> StepResult`, `attempts` 1..5 (retries) |
| `Freshness` | `revision` (an id: commit, generation, digest prefix; `^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$`, never a name) and `as_of` (timezone-aware, or `None`) |

`inputs_digest` must return lowercase hex of 16 to 128 characters; `digest_of(*parts)` gives a
sha256 over length-prefixed parts (unambiguous concatenation).

**Fixed vocabulary.** Count keys, reason codes and step names match `^[a-z][a-z0-9_]{0,63}$`
(`CODE_RE`); counts are non-negative ints, at most 32 keys per build or step. They are a fixed
vocabulary of the stage, like an enum: a stage must never build a count key or a reason code
from its data (a file name, a tag, a tenant).

**Errors.** A stage raises a `StageError` subclass with a reason code. Only the code reaches the
status file (an invalid code becomes `other`); the message is never written or printed, so it
may name things.

| Exception | Typical exit | Meaning |
|---|---|---|
| `InputUnavailable` | 1 (from `inputs_digest`) | the inputs cannot be read; the live artifact is kept |
| `SetupError` | 2 (from the factory or `inputs_digest`) | configuration or environment problem |
| `VerifyFailed` | 3 (from `verify`) | the new artifact failed verification; the live artifact is kept |
| `StageError` | 3 (from `build` / `publish`) | any other stage failure |

The runner picks the exit code by where the exception is raised: anything (a `StageError` or any other exception)
raised by `inputs_digest` exits 1 unless it is a `SetupError`; anything raised by `build`,
`verify` or `publish` exits 3. A non-`StageError` exception is recorded by its type name only.

**`DirectoryStage`** is a base for stages whose artifact is one directory. A subclass implements
`inputs_digest`, `build` and `freshness` and may set `count_glob` (relative to the artifact, for
example `items/*.md`). It supplies:

- `verify`: every declared artifact exists and is not a symlink (`artifact_missing`); with
  `count_glob`, the work directory holds exactly `items` matching regular files
  (`count_mismatch`);
- `publish`: `fsops.swap_in` (section 3);
- `live_count`: regular, non-symlink files matching `count_glob` in a real target directory;
  `None` without `count_glob` or without a live artifact;
- `post_publish`: no steps.

## 3. One run, step by step

`memfw-pipeline run STAGE --config FILE` (or `run_stage(spec, RunOptions(...))`):

1. **Trust check** (only with `trust_paths = true`, section 6). On failure it exits 2 and
   writes nothing, not even the status file: nothing is written into a tree another user could
   have prepared.
2. **State directory.** `<state_dir>/<stage>` is created 0700, every missing parent 0700 too
   (and `chmod 0700` if it already exists; a symlink there is refused). The target's parent is
   created the same way when missing. Failure:
   exit 2, `state_dir_unusable`.
3. **Lock.** `<state_dir>/<stage>/lock`, opened with `O_NOFOLLOW` and held with a non-blocking
   exclusive `flock`. Held by another run: exit **75**, nothing done, nothing written (the JSON
   line says `result: busy`, `ok: true`). A planted symlink or an unopenable lock: exit 2,
   `lock_unusable`. The kernel drops the lock when the process dies, so a killed run never
   wedges the next.
4. **`running` status.** Written at once, carrying the last good fields from the previous status
   (`last_items`, `last_success_at`, `last_built_at`, `published_digest`, `published_revision`,
   `swap_method`). A run killed by the service manager therefore leaves `running` with the
   live artifact still described. A status file that exists but cannot be read adds the warning
   `status_unreadable`.
5. **Leftover cleanup.** First, when the target itself is missing but a `.<target>.old-*`
   directory exists (a crash, or a failed rollback, between the two renames of the fallback
   swap), the newest one is renamed back into place (warning `previous_artifact_restored`).
   Then `.<target>.new-*` and `.<target>.old-*` entries beside the target (from a crashed run;
   they may hold a full copy of the data) are removed under the lock; `leftovers_removed`
   counts them.
6. **Stage factory, freshness, digest.** The factory is imported (`factory_unloadable`,
   `factory_failed`, `not_a_stage`: exit 2). A failing `freshness` is only a warning
   (`freshness_unavailable`); the digest decides. A bad digest is `inputs:bad_digest`, exit 1.
7. **Unchanged?** When not `--force`, the previous `published_digest` equals the digest, the
   target is a real directory, and `live_count` is `None` or equals the recorded `last_items`
   (the status alone is not trusted), the build is skipped. Post-publish steps **still run**,
   so a wiped downstream index heals on the next run. Result `unchanged`.
8. **Shrink baseline.** `last_items` from the status, else `live_count`, else none (section 4).
9. **Build.** A work directory `.<target>.new-<pid>-<ns>` (mode 0700) is made **beside the
   target**, on the same filesystem, so the swap is a rename. `build` fills it; then `verify`.
   Any failure removes the work directory and exits 3 (`build_failed` / `verify_failed`).
10. **Shrink guard** (section 4): refused builds are removed, exit 5.
11. **fsync and publish.** Every regular file and directory in the work directory is fsynced
    (symlinks are neither followed nor synced), `publish` runs, then the target's parent is
    fsynced. `DirectoryStage.publish` swaps real directories, never a symlink, so readers that
    key on the resolved path see one stable directory:
    - `rename`: no live artifact yet;
    - `exchange`: one atomic exchange, `renameat2(RENAME_EXCHANGE)` on Linux,
      `renamex_np(RENAME_SWAP)` on macOS; the old artifact (now in the work directory) is
      removed;
    - `rename_pair`: the fallback when the platform, kernel or filesystem cannot exchange
      (`ENOSYS`, `EINVAL`, `ENOTSUP`, `EOPNOTSUPP`, `EXDEV`): target to `.<target>.old-*`, work
      directory to target; if the second rename fails the old artifact is renamed back.

    A target that is a symlink or not a directory is refused. Any publish failure keeps the old
    artifact live and exits 3 (`publish_failed`). A method outside these three is recorded as
    `custom`. On success the status gets `last_items`, `last_built_at`, `published_digest`,
    `swap_method` and `published_revision` (the build's source revision; dropped when freshness
    was unavailable).
12. **Post-publish steps** run in order; each failing step is retried up to its `attempts`.
    `last_success_at` is set whether or not they pass (the artifact is live and current either
    way); a failed step exits 4 (`post_publish_failed`) with `<step>:<code>` in `errors`.
13. **Final status** (section 5) is written and printed as one JSON line.

| Exit | Meaning |
|---|---|
| 0 | built, or inputs unchanged (post-publish steps ran) |
| 1 | inputs unavailable; the live artifact is kept |
| 2 | setup error (config, untrusted path, stage factory, state directory, lock) |
| 3 | build, verify or publish failed; the live artifact is kept |
| 4 | published (or unchanged) but a post-publish step failed |
| 5 | shrink refused |
| 75 | another run holds the lock: nothing was done; a unit counts it as success |

`memfw-pipeline plan STAGE` reports what `run` would do (`build`, `skip_unchanged`,
`inputs_unavailable`, `setup_error`) with the digest, `live_count`, whether the lock is held,
the number of post-publish steps and freshness. It takes no lock and writes nothing; it exits 2
on `setup_error`, else 0.

## 4. Shrink guard

Without `--allow-shrink`:

- an **empty build** (`items == 0`) is always refused (`empty_build`), first run included;
- a build with `items < baseline * 0.5` (`SHRINK_FLOOR`) is refused (`shrink_refused`) when the
  baseline is known and above zero; **exactly half passes**;
- the baseline is `last_items` from the status (`baseline_source: status`), else the live
  artifact's `live_count` (`live`), else none (`none`: only the empty-build rule applies).

A refusal exits 5 with `result: shrink_refused`, the work directory removed and the live
artifact untouched. The status records `shrink: {baseline, baseline_source, would_publish}`;
with `--allow-shrink` it adds `allowed: true` and nothing is refused. Use it once, by hand,
after checking that the smaller artifact is right.

## 5. Status file

`<state_dir>/<stage>/status.json`, mode 0600, written through an `O_EXCL | O_NOFOLLOW`
temporary file, fsynced and renamed into place (then the directory is fsynced). It is read
back with `O_NOFOLLOW` (at most 1 MiB); a missing, symlinked, corrupt or wrong-`schema` file
reads as none.

Fields: `schema` (1), `runner_version`, `stage`, `kind`, `result`, `ok`, `exit_code`,
`started_at`, `finished_at`, `duration_ms`, `inputs_digest`, `counts`, `artifacts` (a number),
`shrink`, `post_publish` (per step: `ok`, `attempts`, `counts`, or `error` and an exception type
name), `errors`, `warnings`, `leftovers_removed`, `artifact_present`, the carried fields of
section 3 step 4, and `freshness` (section 6).

**The second fence: `clean_status`.** The runner only puts counts, codes, digests, revisions,
timestamps and booleans in the status itself; every payload still passes `clean_status` before
it is written or printed, so a stage bug can never put a file name or text there. Every key must
be a code. Strings must have the shape their key allows:

| Key | Shape |
|---|---|
| `*_at`, `source_as_of` | `YYYY-MM-DDTHH:MM:SSZ` |
| `stage`, `kind` | the ports name rule |
| `inputs_digest`, `published_digest` | lowercase hex, 16 to 128 |
| `source_revision`, `published_revision` | the revision rule |
| `errors`, `warnings` entries | `code` or `prefix:code` |
| `exception` | a type name (`^[A-Z][A-Za-z0-9_]{0,63}$`) |
| `runner_version` | digits |
| anything else | a code (`CODE_RE`) |

A string of the wrong shape, and any value that is not a string, number, boolean, null, list or
mapping, becomes `redacted`; floats are rounded to three places. The status never holds item
names, paths or text.

## 6. Freshness

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

## 7. Config file

TOML, or JSON when the file name ends in `.json`. `--config` itself may be relative (it is
resolved against the working directory); every path inside must be absolute and free of `..`.
Unknown keys are rejected at every level, so a typo fails at startup instead of silently
changing behaviour.

```toml
[pipeline]
state_dir = "/srv/memfw/pipeline-state"    # lock + status, one directory per stage

[stages.example]
factory = "memfw_pipeline.example:factory" # module:callable(options) -> Stage
target = "/srv/memfw/derived/example"      # the live artifact directory
max_lag_seconds = 86400                    # freshness threshold, 60 s to 365 days
trust_paths = false                        # root-run deployments: true
options = { source = "/srv/memfw/source" } # passed to the factory, read-only to the stage

[stages.example.unit]                      # only `memfw-pipeline unit` reads this (section 9)
schedule = "*-*-* 03:30:00"
```

| Key | Required | Rule |
|---|---|---|
| `pipeline.state_dir` | yes | absolute |
| `stages.<name>` | | `<name>` follows the ports name rule; it is the CLI's `STAGE` |
| `factory` | yes | `module.path:callable` |
| `target` | yes | absolute; never the same as, inside, or containing the state directory or another stage's target (a swap would carry the lock, the status or another artifact with it) |
| `options` | no | table, default empty |
| `max_lag_seconds` | no | int, 60 to 31536000, default 86400 |
| `trust_paths` | no | bool, default false |
| `unit` | no | table (section 9) |

Config errors exit 2 with a fixed code: `config_unreadable`, `config_invalid`,
`state_dir_invalid`, `stage_name_invalid`, `stage_config_invalid`, `factory_invalid`,
`target_invalid`, `max_lag_invalid`, `unknown_stage`.

## 8. Path trust (`trust_paths`)

For a stage run as root, a path another user can write would let that user swap the artifact,
the config or the stage code under the runner. With `trust_paths = true`, `run` and `plan` check
these paths before anything else (and stop with exit 2 / `action: setup_error`); `status` then
does not load the stage to probe the source:

- the config file, the stage's state directory, the target;
- the stage factory's top-level package: its package directory and its `__init__.py` (or the
  module file), found with `importlib.util.find_spec` without importing it.

A path is trusted when it and every existing ancestor is not a symlink, is owned by root or the
current user, and is not group- or other-writable. Missing components (not created yet) are
judged by the ancestors that exist. One exception: a root-owned ancestor **directory** with the
sticky bit (`/tmp`) may be world-writable; the path itself never may. Failures are fixed codes:
`config_untrusted`, `state_dir_untrusted`, `target_untrusted`, `factory_untrusted`.

Limits:

- **No symlink in any component**, the ancestors included. Configure real paths (resolve
  them once by hand); a path through a symlinked directory fails the check even when the link
  is root-owned.
- The factory check covers the **package directory**, not every file in it: a group-writable
  module file inside a trusted package directory is not caught. Nor are the interpreter, the
  virtual environment's other packages, or the paths in `options`. Keep the whole environment
  (for example `/opt/memfw/venv`) root-owned and not writable by others.

## 9. Service-manager units

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
| `user` | systemd `User=` | omitted (root) |
| `schedule` | systemd `OnCalendar=` | `daily` |
| `randomized_delay_seconds` | systemd `RandomizedDelaySec=` | 600 |
| `timeout_seconds` | systemd `TimeoutStartSec=` | 3600 |
| `capabilities` | systemd `CapabilityBoundingSet=` | none |
| `read_only_paths`, `read_write_paths` | systemd | none (the state directory and the target's parent are always read-write) |
| `environment` | both | none; proxy variables are refused |
| `launchd_label` | launchd `Label` | `org.memfw.pipeline.<stage>` |
| `launchd_hour`, `launchd_minute` | launchd `StartCalendarInterval` | 3, 30 |
| `launchd_interval_seconds` | launchd `StartInterval` (instead of hour/minute) | |
| `log_dir` | launchd `StandardOutPath` / `StandardErrorPath` | none |

**systemd** (`memfw-pipeline-<stage>.service` and `.timer`, one text with file headers):
`Type=oneshot`, `SuccessExitStatus=75`, `UMask=0077`, `Nice=10`, `IOSchedulingClass=idle`;
`UnsetEnvironment=` the proxy variables and `NO_PROXY=127.0.0.1,localhost,::1`; loopback network
only (`IPAddressDeny=any`, `IPAddressAllow=localhost`,
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`); `InaccessiblePaths=` the docker socket, the
system bus and the systemd private socket (uid 0 with `AF_UNIX` could still `connect()` to
them); `NoNewPrivileges=true`, `CapabilityBoundingSet=` (empty by default),
`ProtectSystem=strict` with `ReadWritePaths=` the state directory, the target's parent and
`read_write_paths`, `ReadOnlyPaths=`, `ProtectHome=read-only`, `PrivateTmp`, `PrivateDevices`,
`ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`, `LockPersonality`,
`RestrictSUIDSGID`; `RequiresMountsFor=` the state directory, the target's parent and the
read-only paths. The timer has `OnCalendar`, `RandomizedDelaySec`, `Persistent=true`. Exits 1 to
5 are failures, so `systemctl --failed` shows them; `status.json` says which. Create the stage's
state directory and the target's parent before enabling the timer: systemd does not start a unit
whose `ReadWritePaths=` or `ReadOnlyPaths=` entries do not exist.

**Capabilities.** With the default (none), a stage running as root still cannot read files it
does not own; add `CAP_DAC_READ_SEARCH` to read and list them. Some filesystems with an extra
ACL layer on top of the mode bits refuse that alone for another user's 0700 directories; there
`CAP_DAC_OVERRIDE` is needed for **reads**. Writes outside `ReadWritePaths` stay impossible
either way, because `ProtectSystem=strict` makes the rest read-only. Check the first run
(`journalctl -u`): an empty build caused by unreadable inputs is refused by the shrink guard.

**launchd**: `Label`, `ProgramArguments`, `ProcessType=Background`, `LowPriorityIO`, `Nice=10`,
`Umask` 077, `RunAtLoad=false`, `EnvironmentVariables` (`NO_PROXY` plus `environment`),
`StartCalendarInterval` or `StartInterval` (setting both is refused), and the log paths when
`log_dir` is set. launchd has no equivalent of the systemd sandbox keys; they are not emitted.

Unit errors exit 2: `unit_invalid`, `unit_path_invalid`, `unit_env_invalid`,
`unit_capability_invalid`, `python_invalid`, `config_path_invalid`, `unit_kind_invalid`.

## 10. The example stage

`memfw_pipeline.example:factory` builds `CopyStage` (kind `example-copy`), the reference the
tests drive and a template, not a production stage. It copies a flat directory of `*.md` files
to `<target>/items/` and writes `<target>/manifest.json` (`{"items": N}`); `count_glob` is
`items/*.md`. Options: `source` (required, absolute; symlinks and other files are counted as
`skipped`), `revision_file` (optional: its first line is the revision, its mtime `as_of`;
without it the revision is a digest prefix and `as_of` the newest file's mtime), `marker`
(optional: an idempotent post-publish step `write_marker` rewrites it with the live item count,
a stand-in for an adapter sync).

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
derived from the data. Write only inside `workdir`; `publish`, the lock and the status are the
runner's.

## 11. Packaging

A standalone uv workspace member with its own wheel, not published to PyPI:

```bash
uv build --wheel --package memfw-pipeline
```

At run time it imports only the standard library and the memfw ports (`Clock`, `check_name`):
from `memfw.ports` inside a memfw install, else from the `memfw-ports` 0.3.0 wheel's
`memfw_ports`. memfw never imports `memfw_pipeline`. Stages live in their own packages and are
named in the config by `module:callable`.

## 12. Not in P1

This release is the contract, the runner, the unit templates and the example stage. Porting the
real admitted-view builder onto it, and the other stages (metadata index, FTS / bm25s / Qdrant
indexes, projections), come later, one package per change.
