# OKF projection (`memfw okf`)

`memfw` can export a vault as an [Open Knowledge Format (OKF) v0.2](https://github.com/GoogleCloudPlatform/open-knowledge-format)
bundle. OKF is a directory of Markdown documents with YAML frontmatter, plus optional
`index.md` / `log.md` navigation and a `manifest.json` validation summary.

**The OKF bundle is a derived, rebuildable projection.** Markdown in the memfw vault remains
the authority; deleting or regenerating the bundle never changes memory.

## Usage

```sh
memfw okf export ./okf-bundle --root ./localdata/demo-vault --force
memfw okf validate ./okf-bundle
```

`export` writes one concept per memory, with:

| memfw | OKF |
|---|---|
| memory id / title | concept id / `title` |
| tag `type:<value>` | `type` (fallback: `Memory`) |
| first body line / summary | `description` |
| tags | `tags` |
| memory id | `resource: urn:memfw:<id>` |
| source / provenance | `sources`, `generated` |
| timestamps | `generated.at`, `memfw` metadata |

The bundle root contains a versioned `index.md`, a `log.md`, and a `manifest.json` with
concept/type counts. `validate` checks that every concept has parseable YAML frontmatter and a
non-empty `type`, and that the manifest concept count matches the bundle.

## Import

`memfw okf import` is an Ingestor: it turns an OKF bundle into **staged candidates** under
`<vault>/_candidates/remember/` and never writes the authoritative vault. A human applies each
candidate with `memfw candidate-apply`, exactly like a candidate from `memory_remember`; both go
through the same staging writer (`memfw.storage.candidate_staging`, re-exported as
`memfw.capture.staging`).

```sh
memfw okf import ./okf-bundle --root ./vault            # dry run (default): report only
memfw okf import ./okf-bundle --root ./vault --apply    # stage candidates
memfw okf import ./okf-bundle --root ./vault --json     # full report as JSON
memfw candidates --root ./vault
memfw candidate-apply ./vault/_candidates/remember/<title>-okf-<hash>.md --root ./vault --apply
```

The import is the inverse of the export table:

| OKF | staged candidate |
|---|---|
| `title` (fallback: `memfw.id`, then file stem) | `title` (one line, at most 200 characters) |
| Markdown body | body; `content_hash` is its sha256 |
| `tags` (scalar items only) | `tags` |
| `type` other than `Memory`, with no `type:` tag | `type:<type>` prepended to `tags` |
| bundle directory name | `source: okf:<bundle-name>` |
| `description`, `resource`, `status`, `sources`, `generated`, `memfw` | kept under `okf_import` for the reviewer |
| any other frontmatter key | kept under `okf_import.unknown_fields` and named in the report |

`candidate-apply` writes title, body, tags and source; the original memfw `layer`, `tier`,
`source`, `provenance`, timestamps and epistemic fields stay in the candidate's `okf_import`
block and are not applied automatically.

Every candidate has a deterministic `proposal_id`: `okf-` plus a hash of the bundle id
(`manifest.bundle_id`, else `manifest.canonical_source`, else the directory name), the concept id
(`memfw.id`, else the file path) and the body hash. The file name is derived from it, so
re-importing the same bundle — or a fresh export of the same vault — reports `already_staged`
instead of staging twice. Only live (pending, unexpired) candidates count: when the earlier
candidate expired or was applied, the concept is staged again under `<name>-2.md`. A concept whose body hash matches a memory already in the vault (for
example a bundle memfw exported from this same vault) or a pending candidate is reported as
`duplicate` and skipped; so is a second concept with the same body in one bundle. A concept with
an empty body is `skipped`.

Bundle content is untrusted data. The importer reads the bundle once, opening every directory
and file relative to its parent's descriptor with `O_NOFOLLOW`, into an in-memory snapshot; it
never reads a path twice. It refuses the whole bundle (exit code 1, nothing staged) if it finds a
symlink whose target leaves the bundle, a symlinked concept or manifest, a non-regular file, a
concept over 64 KiB (the candidate cap), `manifest.json`/`index.md`/`log.md` over 1 MiB, Markdown
that is not UTF-8, more than 10,000 entries, or frontmatter that uses YAML anchors or aliases
(alias bombs and recursive nodes are refused before any node is built). The frontmatter checks
(`type` is a non-empty string) run on the bytes read, and `memfw okf validate`'s checks run on a
private copy of the same bytes.

Staging is all-or-nothing: every candidate is rendered (each capped at 128 KiB) before the first
one is published, and a failure while publishing removes the files this run already wrote. With
`--json`, a refusal prints `{"valid": false, "error": ..., "errors": [...]}` and exits 1, like
`memfw okf validate` on an invalid bundle. Frontmatter strings are re-rendered as single-line quoted YAML, so a bundle cannot end the
candidate's frontmatter early or set `authority`; every imported candidate carries
`okf_import.untrusted: true`.

## Scope

This is a generic projection: no host, SSH, or external search assumptions. Application-specific
enrichment (for example, lifecycle/retrieval fields from a deployment) belongs in the adapter or
exporter that consumes memfw.
