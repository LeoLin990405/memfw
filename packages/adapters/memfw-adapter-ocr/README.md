# memfw-adapter-ocr

MM4 of the multimodal design: turns an attachment's original (a PDF or an
image) into a **proxy import file** (MM2a's `memfw.proxy` v1): text segments, each with a locator,
plus `coverage`, `source_sha256` and the producer. memfw itself never runs a
model; this adapter calls a **local** OCR engine and nothing is uploaded.

Standard library only; depends on `memfw-ports` and nothing else of memfw.
memfw never imports it. Import it as `memfw_adapter_ocr`.

## Output format: MM2a's `memfw.proxy` version 1

The adapter writes memfw's proxy import format (`"format": "memfw.proxy"`,
`"version": 1`), which MM2a (PR #91: `memfw attach proxy import`, Python
form `memfw.ports.proxies`) owns and documents. The format is built in ONE
function, `memfw_adapter_ocr.proxy.to_proxy_candidate`. When the installed
ports have `memfw.ports.proxies`, its constants and parser are used and every
file is validated with MM2a's own `parse_proxy_import` before it is written
and the test suite runs MM2a's `check_proxy_import` on it; with ports older
than MM2a (the standalone `memfw-ports` 0.3.0 wheel, say) the same constants
are mirrored and the parser check is skipped.

```jsonc
{
  "format": "memfw.proxy", "version": 1,
  "kind": "ocr",                                   // or "extract" (a text layer)
  "source_sha256": "<64 hex>",
  "attachment_id": "att-…",
  "coverage": {"status": "partial", "detail": "pages 1-12/40; no text on 1 page(s)"},
  "producer": {"tool": "mcp", "version": "…"},
  "created_at": "2026-01-06T12:00:00+00:00",
  "privacy": "shared",
  "source": {"pages": 40, "page_size": [612, 792]}, // image: {"width", "height"} px
  "segments": [
    {"text": "…", "locator": {"page": 3, "bbox": [72, 695.032, 242.712, 717.232]},
     "confidence": 0.98},
    {"text": "…", "locator": {"page": 4}}
  ]
}
```

Locators are written in canonical units (design v2.1), so no `coordinates`
block is used: PDF boxes in points with the origin at the bottom-left of the
page, image boxes in pixels with the origin at the top-left. Engine
coordinates are converted (poppler's top-left points: `y' = height - y`); a
box outside the page or of zero size is dropped and its text kept (counted in
`coverage.detail`); a dropped box or a segment that had to be cut makes
`coverage.status` `partial`. A segment without a box is `{"page": N}`, or has no
locator for a whole image. A run that finds no text at all is refused
(`no_text`): a proxy needs at least one segment.

## Engines: the `OcrBackend` interface

`memfw_adapter_ocr.backend.OcrBackend` is the adapter's own narrow interface
(`name`, `available()`, `extract(path, *, media, budget, workdir)`).
Implementations:

| `backend` | What it runs |
|---|---|
| `mcp` (default) | an OCR server named in `mcp_command`, run as a child process and spoken to over the MCP stdio protocol |
| `pdftotext` | poppler's `pdftotext -bbox-layout` (text-layer PDFs only), at the absolute path in `pdftotext`; with `pdfinfo` (absolute path) only the first `max_pages` pages are converted and coverage names the page count, without it a longer PDF is refused (`page_budget`); output is read with a 64 MiB cap. `pdfinfo` and `pdftotext` each get the full timeout, so this backend may take up to twice the budget |
| `"module:factory"` | any other implementation; `factory(backend_options)` returns it. **Outside the adapter's guarantees:** memfw cannot check that a custom backend stays local or uploads nothing; that is the operator's responsibility |

Engine discovery is explicit configuration only: no `PATH` search, no
scanning for other tools' installs, no hard-coded locations.

### The MCP backend

One server process per run, stopped (terminate, then kill) when the run ends.
It discovers the server's tools with `tools/list` and uses:

* image: `ocr_image(path, lang)`: texts with per-line confidence (and boxes,
  when the server returns `boxes` with `width`/`height`);
* PDF: `pdf_info(path)` for the page count and text-layer size; then
  `pdf_text(path, out_dir)` for a text layer, or `ocr_pdf(path, out_dir, …)`
  with the page budget (synchronous; the default for every scan). Every call
  gets an `out_dir` inside the run's private work directory, and a file the
  server names is read only if it is inside that directory.
* `allow_async_jobs: true` (off by default) sends scans longer than
  `sync_max_pages` pages to `ocr_submit` / `ocr_job_status` /
  `ocr_job_result` instead. **An async job leaves records in the server's own
  job store** (a job file and a log naming the input), outside memfw's
  control and not removed by it; leave it off for `local-private` originals.
  A job counts as finished only when its status is a done state or
  `running: false` **and** its output file has the same non-zero size on two
  polls; a server that never reports done times out.

The engine (PaddleOCR, MinerU, …) is the server's choice (`engine` is passed
through when set). The server's PDF answers have no boxes, so PDF segments
from it are page-level. MinerU's licence is not pure Apache-2.0 (additional
terms); memfw does not depend on it and must not: if a server uses it, that is
the operator's external tool. The adapter never imports a server's code or
reads its installation, models, configuration or job store.

The child gets a small environment (`PATH`, `HOME`, locale, a private
`TMPDIR`, plus `mcp_env`); proxy variables and anything named like a key,
token, secret or password are never passed, and `mcp_env` may not set
`PATH`, `HOME` or `TMPDIR` (refused as `bad_mcp_env`). An async job still running when
the run's time is up is abandoned with the server process; its output
directory is removed with the work directory.
An engine that starts itself in its own session (`setsid`) leaves the
server's process group and can outlive the run; there is no portable way to
stop it.

## Privacy and integrity

* Only levels in `clearance` (default `public`, `shared`) are handed to an
  engine. **The gate trusts the privacy level the caller declares** (CLI
  `--privacy`, deriver `options.privacy`); the adapter does not read the
  attachment record. Declaring a looser level than the attachment's gets the
  original OCR'd under that clearance, but the result cannot become looser in
  memfw: MM2a's import refuses a proxy less private than its attachment. Attachments default to `local-private`, so OCR of those needs the
  operator to add `local-private` to `clearance`, stating that the configured
  engine is local.
* The original's sha256 must equal the given one before the run, and again
  after it (`source_changed` otherwise). The engine reads a private copy
  (`0700` directory, `0400` file), removed afterwards. Symlinks are refused.
* The output file is written `0600` and never replaces an existing file.

## Use

Off by default. Either by hand:

```
memfw-ocr-proxy check --settings ocr-settings.json
memfw-ocr-proxy run --settings ocr-settings.json --attachment-id att-… \
    --source FILE --sha256 HEX --media-type application/pdf --privacy shared --out proxy.json
```

`--source` must lie under one of the settings' `source_roots` (no symlinks),
the same rule as the deriver.

or through the adapter registry as the `ocr` deriver (`adapters: {enabled:
[ocr], ocr: {...}}`): `DeriveRequest(kind="ocr_proxy", memory_ids=("att-…",),
options={"source": "<path under a source root>", "root": 0, "sha256": …,
"media_type": …, "privacy": …})` returns the candidate as a read-only view
(`epistemic_cap: experience`, `causal_cap: none`). Settings:

```jsonc
{
  "backend": "mcp",
  "mcp_command": ["/absolute/path/to/ocr-server"],
  "mcp_env": {},
  "lang": "en", "engine": "", "dpi": 200,
  "text_layer_min_words": 10, "sync_max_pages": 5, "allow_async_jobs": false,
  "pdftotext": "/absolute/path/to/pdftotext", "pdfinfo": "/absolute/path/to/pdfinfo",
  "source_roots": ["/absolute/dir"],
  "clearance": ["public", "shared"],
  "timeout_seconds": 600, "max_pages": 200
}
```
