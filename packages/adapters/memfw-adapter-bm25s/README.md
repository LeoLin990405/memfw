# memfw-adapter-bm25s

An in-process `RecallProvider` for the memfw recall coordinator that ranks a
vault's memories with [bm25s](https://github.com/xhluca/bm25s) (BM25, numpy
backend). Chinese text is segmented with jieba, as memfw's own FTS does. It
returns memory ids and scores only, never text. Import it as
`memfw_adapter_bm25s`.

Licences (re-checked from each project's `LICENSE`): bm25s 0.3.16 is MIT,
jieba 0.42.1 is MIT, PyYAML 6.0.3 is MIT, numpy is BSD-3-Clause. This package itself is
Apache-2.0, like memfw. It depends on `memfw-ports` for the port contract and
the stdlib vault reader, and on nothing else of memfw; memfw never imports it.

It is off until `vault.yaml` names it:

```yaml
runtime:
  recall:
    adapters:
      - factory: "memfw_adapter_bm25s:factory"
        options: {clearance: "public,shared"}
```

Only memories whose privacy level is in `clearance` (default `public,shared`)
are indexed. The index is built on demand outside the vault, under
`~/.local/share/memfw/adapters/bm25s/<vault key>/` (0700, hashed tokens, no
corpus). `memfw-adapter-bm25s build --vault PATH` builds it ahead of the
first search. See `docs/adapter-bm25s.md` in the memfw repository.
