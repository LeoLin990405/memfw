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

## Scope

This is a generic projection: no host, SSH, or external search assumptions. Application-specific
enrichment (for example, lifecycle/retrieval fields from a deployment) belongs in the adapter or
exporter that consumes memfw.
