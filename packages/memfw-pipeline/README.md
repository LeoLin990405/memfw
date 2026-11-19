# memfw-pipeline

The Stage contract and runner for the derived artifacts of a memfw deployment
(an admitted view, an index, a projection). Every stage implements one small
protocol; the runner gives each of them the same guarantees:

- inputs digest: unchanged inputs skip the rebuild, idempotent post-publish
  steps (adapter syncs) still run;
- single-writer lock (`O_NOFOLLOW`); a held lock exits 75, which a unit treats
  as success;
- status `running` at start (carrying the last good counts), counts and fixed
  reason codes only at the end, never item names or text;
- build into a temporary directory beside the target, verify, fsync, then an
  atomic exchange of real directories (`renameat2(RENAME_EXCHANGE)` on Linux,
  `renamex_np(RENAME_SWAP)` on macOS, else two renames with rollback);
- shrink guard: an empty build, or one under half of the last published count, is
  refused (exit 5) unless `--allow-shrink`;
- freshness: the source revision the live artifact was built from and how far
  it lags;
- an optional path-trust check for root-run deployments;
- `memfw-pipeline unit STAGE --systemd|--launchd` prints a hardened unit.

Standard library only at run time. It depends on `memfw-ports` (the `Clock`
port and the name rules) and on nothing else of memfw; memfw never imports
it. Apache-2.0, like memfw. See `docs/pipeline.md` in the memfw repository.

```bash
memfw-pipeline plan example --config /srv/memfw/pipeline.toml
memfw-pipeline run example --config /srv/memfw/pipeline.toml
memfw-pipeline status --config /srv/memfw/pipeline.toml
memfw-pipeline unit example --config /srv/memfw/pipeline.toml --systemd
```
