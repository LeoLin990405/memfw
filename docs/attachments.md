# Attachments (MM1a, MM1b)

An attachment is an original file (a scan, a PDF, an image, a recording) that
memories can point at. memfw keeps a small **record** inside the vault and the
original either as a **blob outside the vault** (embedded) or where it already
is, under a configured root (**referenced**: no copy). This page covers MM1a
(blob store, records, events, `memfw attach add/list/show/verify`) and MM1b
(referenced originals and their path safety, `detach`, `relink` and the
missing state, `purge`, `scan`). Proxies (OCR text, transcripts), search over
them and `att:` evidence rules come later; see
[What comes next](#what-comes-next).

Nothing here uses the network, runs a model or changes what any existing
command prints for a vault without attachments.

## Layout

```
<vault>/
  attachments/<att-id>.md        one record per registration (small Markdown)
  vault.yaml                     gains vault_id on the first attach
~/.local/share/memfw/blobs/      default blob root (attachments_blob_root)
  <vault-id>/<sha[:2]>/<sha256>  the original's exact bytes, stored once per sha256
  .staging/.stage-<tag>-*.tmp    an original being copied in (shared by the root's vaults;
                                 <tag> = first 16 hex of sha256 of the vault's resolved path)
```

* Records are never under `memories/`, so no memory command, index or export
  sees them.
* The blob directory is `<blob root>/<vault_id>`. The default root is the memfw
  data dir, next to the conversation store; set `attachments_blob_root` in
  `vault.yaml` to move it (an absolute or `~`-prefixed path; a relative one is
  refused). Several vaults can share one root.
* `vault_id` is a random UUID (never derived from `--id-seed`), generated once
  under the vault lock: concurrent first adds agree on one id.
* `add` copies the original into `<root>/.staging` first (no vault id
  needed), then, holding the vault lock, moves it into `<root>/<vault_id>`.
  The staging file name carries the vault's tag, so a stale stage is reported
  only to the vault that made it; other vaults' stale stages (and untagged
  ones from before the tag) are only counted. A vault that moves (or is
  reached by another resolved path) gets a new tag; its stale stages from
  before the move are then counted as another vault's and never deleted by it
  (remove them by hand once no add is running). If the blob root changed
  between staging and the lock, the add is refused before
  `<new root>/<vault_id>` is created.
* Directories are created `0700`, blobs `0600`.

### Where the blob root may be

The root must be a private local location, the same rules as the conversation
store: not inside a git work tree, not inside (or containing) the vault, not
inside a synced folder (iCloud Drive, `Library/CloudStorage`, Dropbox, Google
Drive, OneDrive, Nextcloud, Syncthing, and `~/Documents` and `~/Desktop`,
which macOS iCloud "Desktop & Documents" syncs). A marker that is a symlink
(`~/Dropbox` pointing elsewhere) is refused at its target too. The root itself
and the vault's blob directory may not be symlinks (parents may: `/tmp` is one
on macOS). A refused location stops `attach add` with an error and nothing is
written, not even `vault_id`.

## Records

```yaml
---
id: att-01a1e931-1818-772b-8713-76e6bea4a716
type: attachment
sha256: 5891b5b5…
size: 6
media_type: text/plain
storage: embedded
original_name: a.txt
origin: scanner              # only when given
captured_at: '…'             # only when given
recorded_at: '2026-11-17T11:07:51+00:00'
privacy: local-private
retention: keep              # keep | review | until:YYYY-MM-DD
rights: own work             # only when given
tags: [privacy:local-private]  # only when given
---

Optional human note.
```

* `id` is `att-<uuid7>`, generated per registration: the same original can be
  registered more than once (two records, one blob).
* `original_name` is the base name only. A record never holds an absolute
  path or any of the original's bytes.
* `storage` is `embedded` (a blob) or `referenced`; a referenced record also
  has `location: <root>:<relative path>` (see
  [Referenced originals](#referenced-originals-mm1b)) and never an absolute
  path.
* Unknown keys written by a newer version are ignored on read.
* The frontmatter opens and closes with lines that are exactly `---`, so
  `---` inside a value (a name, origin, rights or tag) never ends it. Each
  record is parsed back before it is written; one that does not read back
  identically is refused and nothing is written.

## Privacy

Attachments follow `memfw.ports.privacy`:

* A new record always carries `privacy`; the default is `local-private`
  (`--privacy public|shared|local-private` sets another). An invalid value is
  refused before anything is written.
* A record read **without** the field is `local-private`. The vault's
  `legacy_privacy_default` does not apply: it covers memories written before
  MM0, and new attachments and proxies stay `local-private` (design v2.1, N1).
* Strictest wins: `privacy:*` and `sensitivity:*` tags and a top-level
  `sensitivity` key can only make a record stricter; an unrecognised value is
  `unknown`, which no clearance admits.

No exit serves attachments in MM1a. The HTTP read-only service, OKF export,
the recall projection, the Obsidian projection, the notes MCP server and the
memfw MCP server read only `memories/` (and the index built from it), so a
record's note, name, tags and id never reach them
(`tests/test_attachments.py::test_attachments_leak_through_no_existing_exit`).
`memory_status` counts log events, so its `event_count` includes `attach`
events (metadata only). The `memfw attach` CLI is the operator's local surface
and shows every record whatever its level, like `memfw list`.

## Commands

```
memfw attach add FILE [--root VAULT] [--embed] [--privacy LEVEL]
                 [--media-type TYPE] [--origin TEXT] [--note TEXT]
                 [--captured-at ISO] [--retention keep|review|until:DATE]
                 [--rights TEXT] [--tag TAG]...
memfw attach add FILE|ROOT:PATH --reference [--root VAULT] [...same options]
memfw attach list [--root VAULT] [--json] [--all]
memfw attach show ID [--root VAULT]
memfw attach verify [ID]... [--root VAULT] [--record-missing] [--no-candidates]
memfw attach detach ID [--root VAULT]
memfw attach relink ID FILE|ROOT:PATH [--root VAULT]
memfw attach purge ID [--yes] [--root VAULT]
memfw attach purge --orphans [--yes] [--root VAULT]
memfw attach scan ROOT:[DIR]|DIR [--hash] [--root VAULT]
memfw attach reown [--new-vault-id] [--yes] [--root VAULT]
memfw fsck [--hash-referenced] [--root VAULT]
```

* `add` hashes and copies FILE in 1 MiB chunks, links the blob to its sha256,
  writes the record and appends one `attach` event, then prints one JSON line
  (`id`, `sha256`, `size`, `media_type`, `storage`, `privacy`,
  `deduplicated`). `deduplicated: true` means the blob was already stored.
  FILE must be a regular file; a symlink is refused (attach its target).
  The media type is guessed from the name unless `--media-type` is given.
  With `--reference` nothing is copied and the JSON line also has
  `location`. Audio and video default to a reference: inside a configured
  root they are referenced without the flag; outside every root they need
  `--embed`. `--embed` and `--reference` exclude each other.
* `list` prints `id`, media type, size, privacy level and name, tab-separated
  (`--json`: one array). A damaged record is a warning on stderr and exit 1.
  Detached attachments are hidden unless `--all`; JSON rows gain `storage` and
  `location` for a referenced attachment and `state` (`detached`, `missing`)
  when it is not active. `show` gains `state` the same way.
* `show` prints the record as JSON plus `cited_by`: the memories whose
  `evidence` names the attachment — `att:ID` (optionally with a `#locator`, in
  any spelling) and, since MM3, the other pointers the gate treats as naming
  it (its sha256, its blob path, its id inside another pointer).
* `verify` re-hashes records and blobs and writes nothing. It prints
  `{"checked", "ok", "problems"}` and exits 1 when a record or blob is
  missing or changed.

### Limits

| `vault.yaml` key | Default | Meaning |
|---|---|---|
| `attachments_max_bytes` | 26214400 (25 MiB) | largest original `add` accepts |
| `attachments_max_total_bytes` | 2147483648 (2 GiB) | sum of the distinct blobs the vault's `attach` events stored |
| `attachments_blob_root` | `~/.local/share/memfw/blobs` | blob root |
| `vault_id` | generated on first attach | names the vault's blob directory (a UUID) |

Over a limit, `add` fails and says so (pointing at `--reference`); it never
switches to a reference on its own. The limits apply to embedded originals
only. A duplicate original adds nothing to the total. The keys are checked
whenever `vault.yaml` is read or written.

The total is summed from the event log: each `attach` event records the
original's `size` and whether it stored new bytes (`blob: new`) or reused a
stored blob (`blob: shared`), so an add parses no record and its cost does
not grow with the number of attachments. A log with an `attach` event from
before these fields falls back to the sizes (`lstat`) of the files in the
vault's blob directory. Otherwise the total is the larger of the log total
and those file sizes (one `lstat` per stored blob, no record parsed), so
orphans count and no cycle of interrupted adds, re-adds of the same bytes
(`blob: shared`) and detaches can push the bytes on disk past the limit.
Log compaction keeps `size`, `blob` and `heir`.

## Events

MM1a adds eight event kinds: `attach`, `attach_update`, `detach`,
`attach_purge`, `attach_missing`, `attach_relink`, `proxy_add`,
`proxy_supersede`. MM1a writes `attach`; MM1b writes `detach`,
`attach_missing`, `attach_relink` and `attach_purge`, plus `attach_found` (added in MM1b); `attach_update` and the
proxy kinds are reserved for MM2. All are known, non-memory kinds. The MM1b
writes also carry `details.projection: "none"`, the event-log
forward-compat marker that lets a reader which does not know a kind skip it
(harmless for readers that know it).

`memfw.core.models` holds the one event manifest: every `EventKind` is in
exactly one of `MEMORY_EVENT_KINDS` (projected by `replay` onto `memories/`)
and `NON_MEMORY_EVENT_KINDS` (skipped by `replay` and by the log-segment
snapshot); a test fails when a new kind is in neither. `replay()` and
`_project_events()` project only `MEMORY_EVENT_KINDS` and skip everything
else, which for the known kinds is exactly `NON_MEMORY_EVENT_KINDS`.

An `attach` event names the attachment id (`memory_id`), the record's hash
(`after_hash`) and `details: {"storage": "embedded", "size": N, "blob":
"new"|"shared"}` (events written before `size` carry only `storage`; a
referenced one is `{"storage": "referenced", "size": N}`). `detach` and
`attach_missing` repeat the committed record hash, `attach_relink` has the
old and new record hashes, `attach_purge` has `details.blob` (`deleted`,
`kept`, `absent`, `none`) and, when a kept shared blob changes owner for the
quota, `details.heir` (another attachment id). No event carries a location
or a name. It never
carries the original's sha256, name or content, so a later purge has nothing to scrub
from the hash-chained log. The first attach also records one `config_change`
for `vault_id` (`vault.yaml` is rewritten by `configure`, like
`memfw privacy legacy-default`).

### Unknown event kinds (forward compatibility, v0.4.x)

memfw 0.3.0 refuses an event log with any kind it does not know. From v0.4
a reader also accepts an unknown kind, but only when the event carries an
explicit marker that it never changes memory state:

```json
{"kind": "proxy_retire", "details": {"projection": "none", ...}, ...}
```

* **The marker** is `details.projection == "none"` (the exact string). It sits
  in `details`, so it is covered by the event hash, adds no field to the event
  model and leaves every existing event's bytes alone. The kind name must
  match `[a-z][a-z0-9_.-]{0,63}`. A namespaced kind (`x-<name>`) was not
  chosen: a kind would keep its prefix after a later version learns it.
* **Unmarked unknown kinds are refused exactly as before**: the same
  `Invalid event … input_value='<kind>'` integrity error. Any other marker
  value (`"memory"`, `null`, `"None"`, a different key) counts as unmarked.
* **Known kinds are classified by the tables above alone**; a marker on them
  changes nothing (a marked `create` still projects).
* A marked unknown event (`ForeignEventKind` in `memfw.core.models`) is:
  * kept byte-for-byte: the active log is never rewritten, and
    `log compact` archives it losslessly (all of its `details`, plus its
    canonical line as `event_line` in the summary row), so it reads back
    with the same bytes and hash, also after compacting again;
  * skipped by `replay` and the log-segment snapshot (never a memory state,
    never an `external_delete`), and ignored by the history retention and
    `rollback` retention windows even when its `memory_id` names a memory;
  * listed by `log verify` as `unknown_non_memory_event_count` and
    `unknown_non_memory_event_kinds` (`{kind: count}`), keys that appear only
    when there is at least one;
  * reported by `fsck` as one `unknown_non_memory_events` row per kind (a
    report, not an error; `fsck` changes nothing for it);
  * reserving its `memory_id`, like a known non-memory kind: a later `add`
    with that id is refused ("Memory id already used");
  * never acted on by `rollback`: `rollback <event>` refuses it ("rollback
    acts only on memory events") and `--dry-run` reports `available: false`.
* **Contract for writers after v0.4.0**: a new kind that never changes a
  memory must be written with the marker so that v0.4.x reads the log. A new
  kind that can change a memory must not carry it; v0.4.x then refuses the
  log, which is the intended fail-closed outcome.
* **The eight MM1a kinds do not carry the marker** and do not need it: every
  v0.4.x reader knows them (including the reserved MM1b/MM2 ones), and 0.3.0
  refuses them with or without it (its model has no such rule). Already
  written `attach` events keep their bytes.
* Checked against a fixture vault whose log a hypothetical newer version
  extended (`tests/fixtures/event_forward_compat/newer-log`,
  `tests/test_event_forward_compat.py`).

## Integrity

`attach verify` and `fsck` report, per attachment:

| Status (`fsck` kind is `attachment_<status>`) | Meaning |
|---|---|
| `record_missing` | committed record file is gone |
| `record_modified` | record bytes differ from the committed hash |
| `record_invalid` | record does not parse |
| `record_untracked` | a file in `attachments/` that no event committed |
| `blob_missing` | no blob at the record's sha256 |
| `blob_modified` | blob bytes or size differ from the record |
| `blob_not_regular` | the blob path is a symlink or not a file (never followed) |
| `blob_unavailable` | the blob directory cannot be used (no `vault_id`, refused location) |
| `blob_orphan` (warning) | a blob no readable record names |
| `blob_mode_loose` (warning) | a blob readable by group or others |
| `blob_stage_stale` (warning) | one of this vault's `.stage-*.tmp` older than an hour under the blob root (an interrupted `add`); never deleted |
| `referenced_missing` | the referenced original is not at its location (the root is there); lists `candidates` |
| `referenced_modified` | the referenced original's bytes or size differ from the record |
| `referenced_unsafe` | a component of the location is now a symlink (never followed) |
| `referenced_unavailable` (warning) | the root is not configured on this host or not mounted |
| `blob_store_foreign_owner` | the blob directory's owner marker names another vault (this vault is a copy, or moved); see [Copies of a vault](#copies-of-a-vault-and-blob-ownership) |
| `blob_stage_foreign` (warning) | how many stale staging files under the root belong to other vaults (or carry no tag); not named, never deleted |

* `fsck` only reports attachment problems. It never registers an
  `external_delete` for an attachment (attachments are not memories in the
  replay projection) and never quarantines, rewrites or deletes a record or a
  blob. On a vault without attachments (no record file, no `attach` event
  and no `vault_id`) it adds nothing and does not open the blob store; once
  `vault.yaml` has a `vault_id` it checks the blob directory even when
  `attachments/` does not exist (an `add` may have stopped before its record).
* Orphan blobs come from an interrupted `add` (the blob is linked before the
  record commits) or from a record removed by hand. `verify` and `fsck`
  report them and delete nothing; `attach purge --orphans --yes` reclaims them.
* A stored blob that no longer matches its address is never reused: a new
  `add` of the same bytes fails and points at `attach verify`.
* `memfw recover` after an interrupted `add` removes a record no event
  committed; the blob stays as an orphan.

### Write cost

Vault writes never read the blob store. `_tree_fingerprint` (run on every
transaction), temp-file cleanup and the cold archive walk only the vault,
which holds records of a few hundred bytes. So the cost of a memory write
does not depend on how many or how large the blobs are, and `attach add`
reads only the new file plus, for a duplicate, the one blob it matches
(`tests/test_attachments.py::test_vault_writes_never_read_blobs`). Records
count toward the vault's physical budget (as `other`); blobs do not.

## Compatibility

* **No attachments, no change.** Every existing command's output is
  byte-identical to 0.3.0 on a vault without attachments
  (`tests/test_attachments_compat.py`, opt-in with `MEMFW_ARCH_BASE_REF`). The
  new `vault.yaml` keys appear only after the first `attach add`.
* **memfw 0.3.0 on a vault with attachments fails closed.** 0.3.0's event
  model accepts only the event kinds it knows, so once an `attach` event
  exists every 0.3.0 command that reads the event log stops with an
  "Invalid event … input_value='attach'" integrity error: `fsck`,
  `log verify`/`replay`, `stats`, every write (`add`, `update`, …) and MCP
  tools that read the log (`memory_status`). Reads of memory files alone still
  work: `show`, `index rebuild` (and index-backed search); `list` lists but
  prints an event log integrity warning. 0.3.0 does not touch
  `attachments/` or the blob store and accepts the new `vault.yaml` keys.
  Measured against the base ref in
  `tests/test_attachments_compat.py::test_base_ref_reader_of_an_attached_vault`.
* A vault that only gained `vault_id` (no `attach` event) stays fully usable
  by 0.3.0.
* So: **no downgrade to 0.3.0 after the first `attach add`; upgrade all hosts
  that read the vault before first use.**

## Referenced originals (MM1b)

A referenced attachment keeps no copy. Its record holds `location:
<root>:<relative path>` plus the original's sha256 and size; `verify`
re-hashes the file in place. Roots are named in `vault.yaml`, so each host maps
a name to wherever it mounts the tree:

```yaml
attachments_roots:
  kh: /mnt/notes                        # on one host
# kh: /srv/notes   (the same name on another host)
```

* A root name is `[a-z][a-z0-9_-]*` (at most 32), not a reserved word (`att`,
  `file`, `http`, `https`, `sha256`, `mem`, `memory`, `data`, `ftp`, `urn`).
  A directory is absolute or `~`-prefixed, without `..`; it may not be `/`,
  the home directory, inside or containing the vault, or containing the blob
  root (checked when `vault.yaml` is written and again when a root is used).
  Changing the mapping is a `config_change`.
* The configured root directory itself is trusted: it is opened as
  configured (a mount point, or a symlink a human set up such as `/tmp` on
  macOS); only the components below it must not be symlinks.
* A reference needs no `vault_id` and no blob store.

### Path safety

Every add, verify, relink and scan applies the same rules; a refusal writes
nothing.

* A location's path is POSIX: relative, `/`-separated, no empty, `.` or `..`
  component, no backslash, NUL or control character. Records written on a Mac
  read the same on Linux.
* A file given by its filesystem path must lie **lexically** inside a root:
  the path is made absolute but never resolved, so `..` (refused outright) or
  a symlink cannot carry it in or out. A root matches as configured and as
  resolved (`/tmp` is `/private/tmp` on macOS); the most specific root wins.
  A path outside every root is refused.
* Opening walks from the root one component at a time (`dir_fd`,
  `O_NOFOLLOW`): a symlink anywhere in the path is refused, whether it points
  out of the root or inside it, and the last component must be a regular file
  (a FIFO or a directory is refused).
* An original with more than one hard link is refused: a link elsewhere
  (perhaps outside every root) could change the bytes behind the location
  without touching any path memfw checks. Reference a copy instead.
* Each component must match a directory entry **exactly**, case and Unicode
  normalization included. On a case-insensitive filesystem `kh:report.pdf`
  would open `Report.pdf`; memfw refuses it ("no such file"), so a location
  means one file on macOS and on a case-sensitive Linux host.

### Detach, missing, relink

* `detach ID` is a soft delete: one `detach` event. The record and the blob
  stay (a blob still counts toward the total); `list` hides it, `verify` no
  longer checks a detached referenced original. Purge removes it for good.
* `verify` reports a referenced original that is gone as `referenced_missing`
  and lists `candidates`: files under the same root with the same size and
  sha256 (one bounded walk per root, symlinks never followed, at most five).
  It never relinks. A root that looks unmounted is `referenced_unavailable`,
  a warning, never "missing": not configured on this host, not a directory,
  an empty directory (an empty mount point), or every original under it gone
  (two or more). `fsck` reports the same statuses without searching for
  candidates, and only `lstat`s referenced originals (exists, regular file,
  same size) because it holds the vault lock; `fsck --hash-referenced`
  re-hashes them as `attach verify` always does.
* `verify --record-missing` records state changes: one `attach_missing` per
  newly missing original (checked again under the vault lock first, and never
  for an unmounted root) and one `attach_found` (a known non-memory kind,
  `details.projection: "none"`) for an original recorded missing that is back
  at its location; `show` and `list` say `state: missing` in between.
* `relink ID FILE|ROOT:PATH` (a human command) points the record at the new
  location after checking that the file there has the record's sha256 and
  size; anything else is a different original and is refused. One
  `attach_relink` event; the record's old bytes stay as `<id>.md.prev` until
  the event commits, so `memfw recover` can put them back.

### Purge

`purge ID` is the hard delete and needs `--yes`. Without it the plan is
printed and nothing is written:

* `cited_by` (memories whose `evidence` names the attachment, by every pointer
  the MM3 gate counts: `att:ID`, its hash, its blob path, its location) and `mentioned_by`
  (memories whose title or body mention `att:ID`). Purge never changes a
  memory: a human decides what to do with them; after the purge `fsck`
  reports each pointer left dangling (`evidence_attachment_invalid`). (There are no proxies yet;
  proxy text quoted in memory bodies is listed once MM2 adds proxies.)
* `blob`: `delete`, `keep_shared` (the notice says how many other
  attachments still use the same blob: **the original is not deleted**),
  `keep_unverified` (another committed record cannot be read, so no blob is
  treated as unreferenced), `absent`, or `none` for a referenced attachment,
  whose original is **never touched** (it is not memfw's copy).
* `stores`: the SQLite stores in the vault, and `irrecoverable_copies`.
* `proxies`: the attachment's proxy files (`attachments/proxies/prx-*.md`,
  MM2a). **A purge does not remove them yet**; proxy deletion arrives with
  the MM2b wiring. Until then the proxy text stays in the vault, and the
  plan says so.

With `--yes` one `attach_purge` event commits the purge (only the id and what
happened to the blob), then the blob is removed when no other record names
it, the record (and any `.prev`) is removed, this vault's stale staging files
are reclaimed, the memory index is dropped (rebuilt on next use, as after a
hard memory delete) and every other SQLite store in the vault that still
holds the record's name, note, origin, rights or location is VACUUMed and
checked again (`vacuumed`, `still_containing`). An interrupted purge is
finished by `memfw recover`. Attachments never go into the cold archive, so
no archive is rewritten. After a purge a full-text search of the vault, the
blob root and the decompressed archives finds nothing of the attachment
(`tests/test_attachments_mm1b.py::test_purge_leaves_nothing_behind`); the
event log keeps only its id.

### Copies of a vault and blob ownership

A vault's blobs live in `<blob root>/<vault_id>`, outside the vault, so a copy
of the vault (`cp -r`, a filesystem snapshot, a test or eval vault made from
it) shares its `vault_id` and therefore the **same blob directory**. To keep
a copy from deleting the original's blobs, the directory carries an owner
marker (`.owner`: the `vault_id`, the 16-hex tag of the vault's resolved
path and that path itself; it stays on this machine, in the private blob
root), written by the first write into it:

* purge, `purge --orphans` and the recovery of an interrupted purge delete
  nothing unless the marker names this vault; the purge plan says
  `blob: refused_owner`, `attach purge --yes` refuses before writing anything;
* `attach add` refuses to write into a directory marked by another vault;
* `verify` / `fsck` report `blob_store_foreign_owner`.

To resolve it:

* **a copy** (snapshot, eval vault, test vault): `memfw attach reown
  --new-vault-id --yes` in the copy, **before any attachment write**. It
  writes a new `vault_id` and copies every blob the copy's records name into
  the copy's own directory; the shared directory is not changed.
* **the original, moved** to a new path (its tag changed): `memfw attach
  reown --yes` marks the directory as this vault's again. It is refused
  while the path recorded in the marker still holds a vault with the same
  `vault_id`: then this vault is a copy, and taking the directory over would
  let its purges delete blobs the original still uses. `--force-take-over`
  does it anyway (only when the recorded vault is known to be a stale copy).
* a directory written before markers existed is marked by its first add (or
  by `reown --yes`); until then purges refuse.

`memfw eval context` (C4) only reads the vaults it evaluates; it never writes
attachments or purges.

`purge --orphans [--yes]` removes blobs that no committed record names and
this vault's stale staging files. It removes no orphan while any committed
record is missing or unreadable (records are the only place a blob's sha256
is kept), or while the blob directory is not this vault's, and says so in
`blocked`; with `--yes` a blocked run prints `"purged": false` and exits 1.

**What a purge cannot reach:** external backups (Time Machine, rsync, cloud
backup) of the vault or the blob root; filesystem or NAS snapshots (for
example NAS snapshots); federation replicas and other hosts' copies of the
vault; test or snapshot vaults copied from this one; OKF bundles and other
exports already written; conversation transcripts or notes that quote the
original. Those copies are irrecoverable by memfw and have to be removed
where they live.

### Scan

`scan ROOT:[DIR]` (or a directory inside a root) is a dry-run inventory for a
bulk registration (design section 8): `files`, `bytes`, `by_media_type`,
`likely_private` (by path words only, such as `private`, `bank`, `invoice`,
`身份证`; a hint for the report, never a privacy decision),
`over_embed_limit`, `already_registered`, `hard_linked` (files with more than
one hard link, which `--reference` refuses), `refused_symlinks`,
`refused_names` (names a location cannot hold), `other_entries`,
`unreadable_dirs`, `truncated`; with `--hash` also `duplicates` (groups and
files with the same sha256, hashing only files whose size repeats). It reads
no file content without `--hash` and writes nothing: no record, no event, no
blob, no `vault_id`.

## What comes next

MM1a is the base the later steps build on:

* **Knowledge-Hub registration** (design section 8, steps 2 and 3): a pilot
  in a snapshot vault, then batches in the live vault after each `attach
  scan` report is reviewed. No command does it on its own.
* **MM2a/MM2b** (proxies): MM2a stores them in `attachments/proxies/`
  with `proxy_add` / `proxy_supersede` events (see
  [attachment-proxies.md](attachment-proxies.md)); the record checks only
  read `attachments/*.md`, so a proxy is never taken for a record. Locator
  bounds will also use `pages`, `width`, `height`, `duration` once the record
  carries them. `attachment_search` / `attachment_get` go through
  `memfw.runtime` and must filter by `AttachmentRecord.privacy_level` with the
  exit's clearance.
* **MM3** (`observation` evidence class) is in: see "What counts as evidence"
  in `docs/epistemic-and-experience.md`. Locators resolve at attachment level;
  once MM2 has proxy segments, a segment index can be passed to
  `memfw.storage.evidence.resolve_attachment_reference(segment_exists=...)`.
  Locator bounds use `pages`/`width`/`height`/`duration` when a record has them.
