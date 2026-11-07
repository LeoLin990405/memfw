# MemFW release readiness

This note records the release verification for the `integrate/w2` baseline at
`4c1760828b1551be1f91768c750fb2e396c95dfb`, plus the documentation-only changes
on `docs/v1-release-sync`.

## Environment and verification

Install the runtime, test tools, and `sqlite-vec` with:

```sh
uv sync --frozen --extra dev
```

MemFW has no `test` extra. Run all checks from the repository root with the
worktree's own environment:

| Check | Command | Measured result |
|---|---|---|
| Default full test suite, including MLX | `.venv/bin/python -m pytest -q` | **622 passed, 0 failed, 2 skipped**, 3 warnings in 353.16 s |
| Type checking | `.venv/bin/python -m mypy src` | **Passed**: no issues found in 65 source files |
| Linting | `.venv/bin/python -m ruff check src/memfw tests` | **Passed**: all checks passed |

The default test command has no ignore or deselection. Commit `054174c` stubs
`os.getloadavg` for MLX transport cases other than the two explicit load-guard
cases, so the suite no longer depends on a quiet host.

## Known limitations

- With `PYTHONDONTWRITEBYTECODE=1` and a cold bytecode cache, jieba emits a
  `SyntaxWarning` that makes two `tests/test_schema_postmortem.py` assertions
  fail because they require empty stderr. A separate cold-cache run of that
  file measured **2 failed, 7 passed**; the default full environment reported
  zero failures as shown above.
- Two tests skip when `reports/day4_baseline_snapshot.zip` is absent. The file
  is a release asset rather than a tracked source file; download and checksum
  instructions are in [`reports/LARGE_FILES.md`](../reports/LARGE_FILES.md).

## Candidate lifecycle

A legacy candidate with a missing or empty `proposal_id` is rejected in both
dry-run and apply modes with `missing_proposal_id`, before any authoritative
write. Re-stage it through `/stage` to obtain the required identity. Commit
`025ee6c` added this fail-closed behavior; the operational details are in
[`docs/v1-integration.md`](v1-integration.md#candidate-staging--write-bridge).

## HTTP contract v2 summary

- Responses carry the contract identifier `memfw-http/2`.
- Read and write credentials are separate. `/search` and `/related` accept
  either token; `/stage` accepts only the write token.
- `/stage` is non-authoritative and writes candidates only under
  `<vault>/_candidates/remember/`.

The authoritative contract is
[`contract-memfw-http-v2.md`](/Users/leo/conductor-wt/r2-prds/docs/plans/wave2/contract-memfw-http-v2.md).
This section is only a summary; the linked contract defines the interface.
