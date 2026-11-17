# bm25s recall adapter

`packages/adapters/memfw-adapter-bm25s` (import `memfw_adapter_bm25s`, version 0.1.0) is a
`RecallProvider` (see [Ports and the recall coordinator](ports-and-recall.md)) that ranks a
vault's memories with [bm25s](https://github.com/xhluca/bm25s) inside the memfw process. It is
a second lexical provider next to the built-in FTS: it returns `memory_id` refs, scores and
`why: lexical`, never text. memfw never imports it; it is **off** until `vault.yaml` names it.

| | |
|---|---|
| External project | bm25s **0.3.16** (pinned), MIT, `github.com/xhluca/bm25s` (licence re-read from its `LICENSE`) |
| Other dependencies | numpy 2.4.6 / 2.5.5 (BSD-3-Clause), jieba 0.42.1 (MIT, the segmenter memfw's FTS already uses), PyYAML 6.0.3 (MIT, to read `vault.yaml` exactly as memfw does) |
| Port | `RecallProvider`, `PORT_VERSION` 1 (`memfw-ports` 0.3.0) |
| Coupling | optional extra in process (architecture section 3); no service, no network |
| Default | off; with it off every output is byte-identical |

## Enable it

Install the adapter next to memfw (it is a workspace member; `uv sync --all-packages`
installs it in a development checkout, or build its wheel with
`uv build --wheel --package memfw-adapter-bm25s`). Then add it to `vault.yaml`:

```yaml
runtime:
  recall:
    adapters:
      - factory: "memfw_adapter_bm25s:factory"
        options: {clearance: "public,shared"}   # the default
        weight: 1.0                             # RRF weight (default 1.0)
        timeout_seconds: 3.0                    # per-provider wait (default 3 s)
```

The MCP `memory_search` `mode=two_stage` coordinator then runs it beside FTS (and hybrid when
an embedder is set) and fuses the lists. Every other mode is unchanged. The setting is read on
the first `two_stage` call and kept until the server restarts. A malformed entry, or a factory
that fails, makes `two_stage` return an error (`recall adapter: <code>`) rather than run
without it; see [§4.1b in Ports and the recall coordinator](ports-and-recall.md#41b-operator-configured-recall-adapters).

Options (anything else is refused):

| Option | Default | Meaning |
|---|---|---|
| `clearance` | `public,shared` | privacy levels to index (comma separated: `public`, `shared`, `local-private`) |
| `data_dir` | `~/.local/share/memfw/adapters/bm25s` | where the index lives |
| `name` | `bm25s` | provider name (labels, `providers` on rows) |
| `k1`, `b` | 1.5, 0.75 | BM25 parameters (bm25s `lucene` scoring) |

The runtime adds `vault` (the vault root) itself; `options` may not set it.

## What it indexes and filters

- **Memories only**, title and body, read with the stdlib reference reader
  (`memfw.ports.reference`). A file that reader cannot parse safely is left out (fail closed),
  as the built-in providers leave out unreadable files. Notes are not indexed; no built-in
  recall provider indexes them either.
- **Tenant** exactly as FTS: the tenant in the Markdown, exact match (the empty tenant matches
  only itself), applied before the page is cut, so a page of tenant `alpha` is never short
  because other tenants scored higher.
- **Privacy** is stricter than FTS because the index sits outside the vault: only memories whose
  level (`memfw.ports.privacy.effective_level`, strictest of the `privacy` field, the
  `privacy:` and `sensitivity:` tags and a top-level `sensitivity` key) is in `clearance` are
  indexed at all. `unknown` never is. A memory without a `privacy` field takes the vault's
  `legacy_privacy_default`, which is `local-private` when unset, so on such a vault the default
  clearance indexes nothing until the operator sets that default (as for the Obsidian
  projection). `local-private` may be added to `clearance` for this in-process adapter; it
  never leaves the machine.
- **Validity, lifecycle, `as_of`, source** are the coordinator's filter policy, as for FTS.
  Every candidate is resolved against the vault and filtered again before it can be returned,
  and rows still pass the MCP server's clearance.

## Where the index lives

A disposable projection outside the vault, keyed like the usage store:
`<data_dir>/<key>/idx-<fingerprint>/`, where `<key>` is the first 24 hex digits of the sha256 of
the vault's resolved path. Directories are 0700 and files 0600; a symlinked directory is
refused. The bm25s vocabulary holds **hashed tokens** (blake2b, 8 bytes) and the corpus is never
saved, so no word of a memory is stored in clear. **Hashing is not strong protection:** the
hashes are unsalted, so anyone who can read the files can hash a word list and test which
words occur. The private directory (owned by you, 0700, refused otherwise) is the protection,
and `local-private` is not indexed unless you add it to `clearance`. `docs.json` holds the indexed ids and tenants.

The fingerprint covers the index format, tokenizer, bm25s version and parameters, the clearance,
`vault.yaml` and every memory file's size, mtime, ctime and inode. A search that sees a new
fingerprint loads the matching saved build or builds one (on demand) and removes older ones.
Searches never write the vault. On a large vault build it ahead of the first search, so that
search stays inside its time budget:

```sh
memfw-adapter-bm25s build --vault /path/to/vault     # or: status
```

Both print counts only (documents, skipped by reason), the fingerprint and the directory.

## Tests and evaluation

- `tests/test_adapter_bm25s.py`: the contract kit (`check_recall_provider`, vault byte-identical,
  tenant ownership), tenant and privacy rules, the on-disk layout and modes, rebuild on change,
  loading a saved build, setting checks, the registry spec, a coordinator with bm25s in place of
  FTS, and the runtime wiring (`runtime.recall.adapters`, default unchanged, fail-closed codes,
  a down adapter degrading to FTS).
- `tests/test_swap_drills.py` runs it through the same recall contract as the built-in
  providers; `tests/standalone/wheel_smoke.py --bm25s` runs it from its wheel without memfw.
- `python -m eval.retrieval_r0.bm25s_compare` compares FTS, bm25s and both fused on the
  synthetic R0 report split under the `compat_fts` policy and writes
  `eval/retrieval_r0/bm25s_report.json`. It is a report, not a gate. At 160 questions per class
  all three arms score the same (evidence recall@10 0.9167, no forbidden or other-tenant hit
  in the top 10): the R0 questions are built on unique lexical keys, so this shows parity,
  not a quality gain.
