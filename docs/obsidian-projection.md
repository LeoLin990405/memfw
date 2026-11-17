# Obsidian projection

`memfw project obsidian` renders a vault as a folder that Obsidian can open as
its own vault: one page per memory, concept and open-question pages, wikilinks
you can follow in the graph, and Bases tables. The board is for reading. It is
rebuilt from the vault on every run and never read back, so you can delete it
and run the command again.

```bash
memfw project obsidian --root ./vault --out ./memfw-board
memfw project obsidian --root ./vault --out ./memfw-board --clearance public
memfw project obsidian --root ./vault --out ./team-board --tenant team-a
```

Nothing runs unless you invoke the command. It adds no setting and does not
change the output of any other command.

## Layout

```
<out>/
  memories/<id>.md               one page per memory
  questions/<id>.md              type: open-question memories
  concepts/<name>.md             one page per `about` value
  memfw-views/memories.base      Bases: grouped by type, status, epistemic
  memfw-views/questions.base     Bases: open questions, all questions
  memfw-views/disputed.base      Bases: status == disputed
  .memfw-obsidian/memfw-projection.json  sha256 of every generated file
  .memfw-obsidian/tmp-projection/        temporary files (empty after a run)
```

Every generated page has the property `generated: memfw-projection` and starts
with a note that it will be overwritten. Change the memory in memfw instead.
Bases files carry the same notice as YAML comments.

### Memory pages

The properties are `id`, `title`, `type`, `epistemic`, `status`, `privacy`,
`tenant`, `valid_from`, `valid_to`, `tags`, `about`, `supersedes` and
`superseded_by`. Empty values are written as `null` or `[]`, so every page has
the same keys and Bases columns line up. The page body is the memory body.

- **Tags.** memfw tags become Obsidian tags: `a:b` is written as `a/b`, and
  characters Obsidian does not allow become `_`. Join and privacy markers
  (`fleet_path:`, `sensitivity:`, `privacy:`) are left out. The page's
  `privacy` property carries the class instead.
- **Corroboration.** `corroboration.count` is added when the U4 corroboration
  projection (`corroboration.sqlite3`, built by `memfw corroboration rebuild`)
  exists. The count is trimmed by the `conversation_share` policy, the same
  way as `memfw corroboration show`. The board shows only the count, never
  app or session names.

### Links

- **`about`.** Written as `[[concepts/<name>]]`. When the vault has a concept
  file for the oid (`memfw concepts`), the concept page shows its preferred
  label, class and status. K1 `broader` links and the derived `narrower`
  links connect concept pages, but only pages that are on the board. A
  concept that only hidden memories use is never named.
- **Supersession.** `superseded_by` links to the newer memory, and the newer
  memory's `supersedes` links back.
- **Open questions.** An open question's `conflicts` link to the memories that
  disagree, and `resolved_by` links to the answer. `open` is `true` until the
  question has an answer or a `valid_to`. A question whose answer is hidden by
  the clearance is shown as open, with no `resolved_by` and no `valid_to`, so
  the page does not reveal that a hidden answer exists.

A link is written only when its target is on the board too. A page never names
a memory that the clearance or the tenant hides.

## Privacy

Each memory gets the strictest of memfw's readings of its privacy. These
readings are the `sensitivity:*` and `privacy:*` tags, which are matched in
any case, and any top-level `privacy` or `sensitivity` front matter key. A
memory ends up in one of these classes:

| Class | When |
|---|---|
| `public` | every declared privacy value is `public` |
| `shared` | no declared value, or `shared`/`fleet`/`none` |
| `local-private` | `sensitivity:private` or `privacy: local-private` |
| `unknown` | any other value, such as `sensitivity:secret` or `privacy:restricted` |

`--clearance` is a comma list of `public`, `shared` and `local-private`, read
in any case. The default is `public,shared`. Any other token, `unknown`
included, is an error, so a typo cannot widen or silently narrow access.
Memories in the `unknown` class are never exported, whatever the clearance.

A hidden memory leaves no trace on the board. It gets no page, no link, no
backlink and no Bases row, and it is not counted in a concept's
`memory_count`. A concept that only hidden memories use gets no page.
Unreadable memory files are skipped.

Keep the board outside folders that sync automatically, and do not publish it
unless the clearance allows that.

## Tenant

Each run exports exactly one tenant: the default tenant `""`, or the one
given with `--tenant`. Tenants are never mixed on one board.

## Determinism and git

The same vault with the same flags gives byte-identical output:

- Ids are sorted, YAML keys are written in a fixed order, and no file contains
  a run timestamp. The manifest has no timestamp either.
- A file whose content did not change is not rewritten, so its mtime stays.

This means the board directory can be a git repository. Commit after each
refresh, and the diff shows only real memory changes.

Concept file names use only `A-Z a-z 0-9 _ -`. An oid with any other
character, such as `concept:demo:one`, keeps a readable stem and gets a short
hash of the original oid appended, so two oids never share a file.

## Rebuild and hand edits

The manifest records the sha256 of every file the projector wrote.

- **Deleting and overwriting.** A rebuild deletes or overwrites only files
  listed in the previous manifest. Your own files are left alone, including
  notes you add, `.obsidian/` and `.git/`.
- **Hand edits.** If a listed file's bytes no longer match the manifest, it
  was edited by hand. It is reported on stderr and in the `edited` field of
  the JSON summary, then overwritten.
- **Conflicts.** If a file that is not in the manifest already sits at a path
  the projector needs, the run is refused before anything is written.
- **Bad manifest.** A manifest that cannot be read, or that lists a path
  outside the generated folders, makes the run refuse rather than guess what
  to delete.

## Coexisting with other generators

Another tool may write its own pages into the same folder. For example, a
private board script might own `00 overview.md`, `agents/`, `boards/`,
`.memfw-obsidian/manifest.json` and `.memfw-obsidian/tmp/`. This projector is
disjoint from such a tool. Everything it owns is named by constants in
`memfw.projections.obsidian`:

| Constant | Value |
|---|---|
| `GENERATED` (page property and manifest `generator`) | `memfw-projection` |
| `MANIFEST_PATH` | `.memfw-obsidian/memfw-projection.json` |
| `TMP_DIR` | `.memfw-obsidian/tmp-projection` |
| `FOLDERS` | `memories`, `questions`, `concepts`, `memfw-views` |

It reads, writes and deletes only those paths. Another generator's manifest
in `.memfw-obsidian/` is ignored. The Bases views filter on
`generated == "memfw-projection"`, so they never list the other tool's pages.
Do not give two tools the same marker or folders.

## Safety

- The vault is only read. Symlinked memory and concept files are skipped. A
  vault with a pending transaction is refused (run `memfw recover` first).
- The output directory may not be the vault, sit inside the vault, contain
  the vault, or be a symlink. The generated folders may not be symlinks.
- Some outputs are refused as protected: `/`, the home directory itself,
  `~/Documents` and `~/Library/Mobile Documents` and anything below them
  (these folders sync), any path containing `knowledge-hub`, and every root
  listed in `MEMFW_OBSIDIAN_PROTECTED` (separated by `os.pathsep`) and
  anything below them.
- Temporary files are written in `.memfw-obsidian/tmp-projection/`, never in a
  page folder, so a crash cannot leave page content where git or Obsidian
  would pick it up. Temporary files left by an interrupted run are deleted at
  the start of the next run. That covers the temp folder and the older
  `.<name>.<hex>.tmp` names in the projector's own folders.
- Files are written through a temporary file and `os.replace`, so no symlink
  is followed.

## API

`memfw.projections.ObsidianProjector` implements the L0 `Projector` port
(`export(vault, out, policy) -> Manifest`) and passes
`memfw.ports.testing.check_projector`. `build()` returns an `ObsidianResult`
that also lists the files written, unchanged, deleted and edited.
Corroboration is injected as a callable that maps ids to counts, so the
projection does not import any capability.
