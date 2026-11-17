# Attachments (MM1a)

An attachment is an original file (a scan, a PDF, an image, a recording) that
memories can point at. memfw keeps the original's bytes as a **blob outside the
vault** and a small **record** inside it. This page covers what MM1a ships:
the blob store, the records, the events and `memfw attach add/list/show/verify`.
Proxies (OCR text, transcripts), search over them, `att:` evidence rules,
detach, purge and referenced (external) originals come later; see
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
                                 <tag> = first 8 hex of sha256 of the vault's resolved path)
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
  ones from before the tag) are only counted. If the blob root changed
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
* `storage` is always `embedded` in MM1a (`referenced` is MM1b).
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
memfw attach list [--root VAULT] [--json]
memfw attach show ID [--root VAULT]
memfw attach verify [ID]... [--root VAULT]
```

* `add` hashes and copies FILE in 1 MiB chunks, links the blob to its sha256,
  writes the record and appends one `attach` event, then prints one JSON line
  (`id`, `sha256`, `size`, `media_type`, `storage`, `privacy`,
  `deduplicated`). `deduplicated: true` means the blob was already stored.
  FILE must be a regular file; a symlink is refused (attach its target).
  The media type is guessed from the name unless `--media-type` is given.
  Audio and video default to a referenced attachment, which MM1a does not
  support, so they need `--embed`.
* `list` prints `id`, media type, size, privacy level and name, tab-separated
  (`--json`: one array). A damaged record is a warning on stderr and exit 1.
* `show` prints the record as JSON plus `cited_by`: the memories whose
  `evidence` holds `att:ID` (optionally with a `#locator`).
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

Over a limit, `add` fails and says so; it never switches to a reference on
its own. A duplicate original adds nothing to the total. The keys are checked
whenever `vault.yaml` is read or written.

The total is summed from the event log: each `attach` event records the
original's `size` and whether it stored new bytes (`blob: new`) or reused a
stored blob (`blob: shared`), so an add parses no record and its cost does
not grow with the number of attachments. A log with an `attach` event from
before these fields falls back to the sizes (`lstat`) of the files in the
vault's blob directory. A blob left by an interrupted add (an orphan) that a
later add of the same bytes reuses is not counted.

## Events

MM1a adds eight event kinds: `attach`, `attach_update`, `detach`,
`attach_purge`, `attach_missing`, `attach_relink`, `proxy_add`,
`proxy_supersede`. Only `attach` is written now; the others are reserved for
MM1b and MM2 so their classification is fixed today.

`memfw.core.models` holds the one event manifest: every `EventKind` is in
exactly one of `MEMORY_EVENT_KINDS` (projected by `replay` onto `memories/`)
and `NON_MEMORY_EVENT_KINDS` (skipped by `replay` and by the log-segment
snapshot); a test fails when a new kind is in neither. The two hand-written
skip lists that `replay()` and `_project_events()` used to carry are now that
one constant, with the same members for the old kinds.

An `attach` event names the attachment id (`memory_id`), the record's hash
(`after_hash`) and `details: {"storage": "embedded", "size": N, "blob":
"new"|"shared"}` (events written before `size` carry only `storage`). It never
carries the original's sha256, name or content, so a later purge has nothing to scrub
from the hash-chained log. The first attach also records one `config_change`
for `vault_id` (`vault.yaml` is rewritten by `configure`, like
`memfw privacy legacy-default`).

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
| `blob_stage_foreign` (warning) | how many stale staging files under the root belong to other vaults (or carry no tag); not named, never deleted |

* `fsck` only reports attachment problems. It never registers an
  `external_delete` for an attachment (attachments are not memories in the
  replay projection) and never quarantines, rewrites or deletes a record or a
  blob. On a vault without attachments (no record file, no `attach` event
  and no `vault_id`) it adds nothing and does not open the blob store; once
  `vault.yaml` has a `vault_id` it checks the blob directory even when
  `attachments/` does not exist (an `add` may have stopped before its record).
* Orphan blobs come from an interrupted `add` (the blob is linked before the
  record commits) or from a record removed by hand. MM1a reports them and
  deletes nothing.
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

## What comes next

MM1a is the base the later steps build on:

* **MM1b** (roots, path policy, detach, purge, missing/relink): `storage:
  referenced` with a `location` (a `<root-name>:<relative path>` under configured
  roots, `lstat` on every component), `--reference` on `add`, `detach`
  (soft, `detach` event), `purge` (hard, needs human consent; blob refcount
  is derived from records, so a blob is removed only when no record names it;
  an orphan must not be reclaimed while any committed record is missing or
  unreadable, because records are the only place a blob's sha256 is kept),
  `attach_missing` / `attach_relink` written by `verify` and `relink`.
* **MM2a/MM2b** (proxies): proxy files next to the record
  (`attachments/<id>.proxy.<kind>.<n>.md`) with `proxy_add` /
  `proxy_supersede` events; `verify_attachments` and `record_untracked`
  currently treat every `*.md` in `attachments/` as a record, so MM2a must
  teach them the proxy file name. Locator bounds need `pages`, `width`,
  `height`, `duration` on the record (not written yet). `attachment_search` /
  `attachment_get` go through `memfw.runtime` and must filter by
  `AttachmentRecord.privacy_level` with the exit's clearance.
* **MM3** (`observation` evidence class): `att:` evidence is only listed by
  `attach show` today; the gate does not yet treat it specially.
