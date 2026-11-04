# Large tracked files: release plan (no deletion yet)

Tracked files over 5 MB (measured with `git ls-files | xargs ls -l`, 2026-10-21). `.git` is 15 MB.

| File | Size | SHA-256 (prefix) | Recommendation |
|---|---:|---|---|
| `reports/day3_synthetic.jsonl` | 14.6 MB | `cbfc689262b775c5` | Move to a GitHub Release asset; keep a short summary + full checksum + regeneration command in `reports/`. |
| `reports/day3_synthetic.capture.jsonl` | 14.5 MB | `1003c58ab29dd68c` | Same as above (synthetic, regenerable). |
| `reports/day7_official.json` | 12.1 MB | `08aa8d47356c5941` | README links to it as evidence: attach to a Release, keep a summary JSON (aggregate tables, bootstrap results, SHA-256) in the repo, and update README links. |

Steps when approved:
1. Create a Release (e.g. `v0.1-evidence`) and upload the three files.
2. Replace each with `<name>.summary.json` holding size, full SHA-256, the producing command, and the aggregate numbers cited in reports.
3. Update links in `README.md` and `reports/day7_report.md`.
4. Do **not** rewrite history unless the repo must shrink; if it must, decide explicitly (history rewrite changes every commit hash).

**Status:** done — files are in Release `data-v1`; see `reports/LARGE_FILES.md`. History was rewritten to purge them (owner-approved).
