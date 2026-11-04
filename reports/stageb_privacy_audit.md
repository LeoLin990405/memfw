# Stage B repository privacy and validation record

- Checked: 2026-10-21 (UTC)
- Scope: repository-local paths only. No external private data, approval output, or query bodies were opened.
- `.gitignore`: SHA-256 `e7ac596f65605d65f7a1e6414ffe754cc09dd8dda11447cf30befebc6c50dd93` (881 bytes). It excludes local datasets and runtime output, private pilot paths, model/cache artifacts, secrets patterns, Python caches, and non-reproducible report archives/logs/timing outputs including `reports/day6_runs/`.
- Repository inventory: 75 candidate report `.md`/`.json`/`.jsonl` files (10/44/21); 86 test files; 83 report files overall. Sensitive-name scan flagged one candidate report filename; it was not opened. Existing test fixtures were retained as candidates.
- `CHANGE_MEMORY.jsonl` pre-append snapshot: 149 lines, SHA-256 `134479457dd0d6f88c6d22c66c9369fff06ea5d68439fd23440298ad3c1f6b66`. Counts-only scan found 0 PEM private-key markers, 0 credential assignments, 0 bearer-token signatures, and 0 private-query term matches.
- Validation: `./.venv/bin/python -m pytest -q` passed, 420 tests in 211.60 seconds; exit code 0.
- Git setup: local `git init` was attempted and rejected by the operating system with `.git: Operation not permitted`. No alternate git directory, remote, staging, or commit was created.
- Status: partial. Ignore rules and audit evidence are prepared; repository initialization and commits remain unavailable under current filesystem permissions.
