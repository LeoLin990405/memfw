# Attachment proxies (MM2a)

A **proxy** is the searchable text of an attachment: OCR of a scan, a
transcript of a recording, captions of video keyframes, text extracted from a
PDF. memfw never runs the tool that makes it. An adapter (or a person) writes
one JSON file in the format below and imports it:

```
memfw attach proxy import ATT-ID FILE [--root VAULT] [--privacy LEVEL] [--supersedes PRX-ID]
memfw attach proxy list [ATT-ID] [--root VAULT] [--all]
memfw attach proxy show PRX-ID [--root VAULT]
memfw attach proxy budget [--root VAULT]
```

Nothing here uses the network or runs a model, and a vault without proxies
behaves exactly as before.

## The import format, version 1

This is the stable contract between memfw and proxy producers (the OCR
adapter, transcription adapters, manual exports). The Python form is
`memfw.ports.proxies` (stdlib only, shipped in the standalone `memfw-ports`
wheel): `ProxyImport.to_json()` writes a valid file, `parse_proxy_import` /
`load_proxy_import` read one, and `memfw.ports.testing.check_proxy_import` is
the contract check an adapter runs against its recorded fixtures. Example
files: `tests/fixtures/proxies/ocr-pdf-v1.json`,
`tests/fixtures/proxies/transcript-audio-v1.json`.

```json
{
  "format": "memfw.proxy",
  "version": 1,
  "kind": "ocr",
  "source_sha256": "<sha256 of the original's bytes>",
  "attachment_id": "att-…",
  "coverage": {"status": "partial", "detail": "pages 1-2/3"},
  "producer": {"tool": "paddleocr", "version": "2.9.1"},
  "created_at": "2026-11-17T08:00:00+00:00",
  "language": "zh",
  "privacy": "local-private",
  "supersedes": "prx-…",
  "source": {"pages": 3, "page_size": [612, 792]},
  "coordinates": {"unit": "px", "origin": "top-left", "dpi": 144},
  "segments": [
    {"text": "Invoice 2026-031", "locator": {"page": 1, "bbox": [144, 144, 1080, 216]},
     "confidence": 0.97},
    {"text": "Page two, whole page", "locator": {"page": 2}}
  ]
}
```

| Key | Required | Meaning |
|---|---|---|
| `format`, `version` | yes | exactly `"memfw.proxy"` and the integer `1` (`1.0`, `"1"` and other versions are refused) |
| `kind` | yes | `ocr`, `transcript`, `keyframes`, `extract` or `caption` |
| `source_sha256` | yes | sha256 of the bytes the proxy was made from; must equal the attachment's `sha256` |
| `coverage` | yes | `{"status": "full" \| "partial", "detail": "…"}`; a `keyframes` proxy is always `partial` |
| `segments` | yes | one or more `{"text", "locator"?, "speaker"?, "confidence"?}` |
| `attachment_id` | no | when present, must be the `ATT-ID` imported into |
| `producer` | no | `{"tool", "version"}`, `"human"` or `"app-manual:<app>"` (every name printable: no control, bidi or invisible format characters) |
| `created_at` | no | ISO 8601 with a timezone (when the producer ran) |
| `language` | no | BCP 47 tag (`en`, `zh-Hans`) |
| `privacy` | no | `public`, `shared` or `local-private` (default `local-private`) |
| `supersedes` | no | an active proxy of the same attachment this one replaces |
| `speaker_labels` | no | transcripts only, and only `"unverified"` |
| `source` | no | the original's extent: `pages`, `page_size` or `page_sizes` (pt), `width`/`height` (px), `duration` (s) |
| `coordinates` | no | the space `bbox`/`t`/`frame` are written in (below); default: canonical |

Unknown keys (top level, in `coverage`, `source`, `coordinates`, a segment or
a locator) and duplicate keys are refused, so a typo never passes silently.
Segment text has `\r\n`, `\r`, U+2028 and U+2029 turned into `\n` and outer
whitespace stripped; it may not be empty, exceed 65 536 characters, hold any
other C0/C1 control character (`\t` is kept), a bidirectional override or
isolate (U+202A–U+202E, U+2066–U+2069) or an invisible mark (U+200B, U+200E,
U+200F, U+061C, U+FEFF), or contain a line starting with
`<!-- memfw:segment`. Numbers too large for a float are refused, and so is
JSON nested too deeply to parse. `speaker` is allowed in transcripts only and is
never taken as verified. `confidence` is 0..1.

### Locators and units

Stored locators are always in canonical units (design v2.1):

| Medium (from the attachment's media type) | Locator keys | Canonical unit and origin |
|---|---|---|
| PDF (`application/pdf`) | `page` (1-based), `bbox` (needs `page`) | points (1/72 in), **bottom-left** origin of the page; `y0` is the lower edge |
| image (`image/*`) | `bbox` | pixels, **top-left** origin; `y0` is the upper edge |
| audio (`audio/*`) | `t: [start, end]` | seconds |
| video (`video/*`) | `t: [start, end]` or `frame` (one keyframe) | seconds |
| anything else | `page` | 1-based |

No locator (absent, `null` or `{}`) means the segment is about the whole
attachment; any other non-object (`[]`, `""`, `0`) is refused.
`bbox` is `[x0, y0, x1, y1]` with `x0 < x1`, `y0 < y1`, all `>= 0`.
Values are kept to three decimals. PDF boxes are in the page's **unrotated**
space: the page as its MediaBox lies, ignoring `/Rotate`, with the origin at
its lower-left corner. A producer that read a rotated page must map its boxes
back before writing them.

`coordinates` lets a producer keep its own space; memfw converts on import:

* `unit`: `pt` (PDF), `px` (an image, or a PDF page rendered at `dpi`), or
  `fraction` (0..1 of the page or image, needs its size in `source`);
* `origin`: `bottom-left` or `top-left` (flipping needs the page or image
  height in `source`);
* `dpi`: required for `px` boxes on a PDF page (`pt = px * 72 / dpi`), and
  refused with any other unit;
* `time_unit`: `s` (default) or `ms`.

For example a page rendered at 144 dpi, box `[144, 144, 1080, 216]` px from
the top-left on a 612x792 pt page, is stored as `page=1&bbox=72,684,540,720`.

### Bounds

Every locator is checked against the original's dimensions when they are
known. **Today that means the dimensions the producer declares in `source`
only**: memfw does not read the original, and attachment records do not carry
dimensions yet. A proxy without `source` gets only the shape checks (pages
from 1, non-negative ordered boxes and times). The `known=` hook of
`parse_proxy_import` is where record metadata (MM1b/MM2b) will plug in; the
two must then agree where both say something.
A page past the last page, a box outside its page or image, or a time past
the duration refuses the whole import; nothing is stored partially. A locator
key the medium does not allow (`t` on a PDF, `page` on an image) is refused
too.

The fragment form, used in `proxy show` and later in `att:<id>#…` evidence,
is `page=3&bbox=72,600.5,540,640`, `bbox=…`, `t=12.5,20`, `frame=t:65`.

## Stored proxies

```
<vault>/attachments/proxies/<prx-id>.md
```

```markdown
---
id: prx-…
type: attachment_proxy
format_version: 1
attachment: att-…
kind: ocr
medium: pdf
source_sha256: …
coverage: {status: partial, detail: pages 1-2/3}
producer: {tool: paddleocr, version: 2.9.1}
created_at: '…'
recorded_at: '…'
status: unverified
privacy: local-private
source: {pages: 3, page_size: [612, 792]}
segment_count: 3
---

<!-- memfw:segment {"n":1,"locator":"page=1&bbox=72,684,540,720","confidence":0.97} -->
Invoice 2026-031
```

* Proxies live in their own directory under `attachments/` (not
  `attachments/<id>.proxy.<kind>.<n>.md` as design 1.2 sketched), so the
  attachment record checks never mistake one for a record. They are never
  under `memories/` and never in a memory body.
* `id` is `prx-<uuid7>`, generated at import. `status` is `unverified`.
  `speaker_labels: unverified` is written for every transcript.
* Each file is parsed back before it is written; one that would not read back
  identically is refused. A `>` or `<` in a segment marker is escaped, so a
  speaker name cannot close the comment.

### Events

* `proxy_add` (`memory_id` = proxy id, `after_hash` = the file's sha256,
  `details: {"projection": "none", "attachment", "kind", "segments",
  "supersedes"?}`).
* `proxy_supersede` (`memory_id` = the replaced proxy, `before_hash` = its
  file hash, `details: {"projection": "none", "attachment", "by"}`).

Both are non-memory kinds (skipped by `replay`), and carry the forward-compat
marker `projection: "none"`. They carry ids and counts, never text or the
original's hash. A superseded proxy keeps its file but is no longer active
(MM2b search skips it); a proxy can be superseded once. The replacing
`proxy_add`'s `details.supersedes` is what makes the old proxy inactive; the
`proxy_supersede` event confirms it, so a crash between the two appends
never leaves two active proxies (and a retry is refused as "already
superseded").

### Privacy

A new proxy is `local-private` unless `--privacy` or the file's `privacy` says
otherwise (when both are given they must agree). A proxy can never be less
private than its attachment: `shared` on a `local-private` attachment is
refused. Readers act on the strictest of the proxy's and the attachment's
level (`ProxyRecord.effective_level`). In MM2a no exit serves proxies: HTTP,
OKF, the notes and memfw MCP servers and every projection read only
`memories/`.

### Budget

Proxies are accounted on their own (design v2.1, "预算"):
`attachments_proxy_max_bytes` in `vault.yaml` (default 67 108 864, 64 MiB)
caps everything under `attachments/proxies/` together (superseded proxies and
stray files included); an import that would pass it is refused. Proxy files are **not** part of the vault's physical size
(`memfw stats`, `memfw budget`, `memfw forget` with a total budget), so they
can never make a forgetting policy pick a memory. `memfw attach proxy budget`
prints `{"files", "bytes", "max_bytes"}`.

### Integrity

`fsck` reports `proxy_record_missing`, `proxy_record_modified`,
`proxy_record_invalid`, `proxy_record_untracked` (anything under
`attachments/proxies/`, subdirectories included, that is not a committed
`prx-*.md`), `proxy_attachment_missing`, `proxy_source_mismatch` and
`proxy_supersede_missing` (a replacement committed but its `proxy_supersede`
confirmation was not written, e.g. after a crash; the old proxy is already
inactive) and `proxy_supersede_invalid` (a `proxy_add` or `proxy_supersede`
naming a target that, at that point in the log, was not a committed, still
active proxy of the same attachment; it is ignored, so a forged or stale
event never deactivates another proxy). It never registers an `external_delete` and never
changes a proxy. On a vault without proxies it adds nothing. `memfw recover`
after an interrupted import removes the uncommitted proxy file.

## Not yet

* Search over proxies (`proxy_segments`), the `ProxyProvider` recall provider
  and the MCP `attachment_search` / `attachment_get` tools are MM2b.
* Attachment records do not carry `pages`/`width`/`height`/`duration`; once
  they do, imports are bounded by them as well (`known=`).
* Marking a proxy `reviewed`, and purging proxies with their attachment
  (MM1b `purge`), are not implemented here.
