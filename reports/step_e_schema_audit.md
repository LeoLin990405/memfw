# Stage E schema audit

**Status: incomplete.** Metadata identifies one `invalid_schema` failure for the user-specified target, with 29 candidates from 30 requested. It does not establish which validation condition failed.

The nested generation metadata for `day9-run.log` reports one failure with reason `invalid_schema`, `candidate_count=29`, and `requested_count=30`. It contains no `shape_diagnostics`. The later `query-repair.log` reports `status=complete`, `candidate_count=30`, `repaired_count=0`, and `requested_count=30`. That later state confirms a complete candidate set; it does not explain the earlier failure.

The current adapter already accepts valid overproduction and truncates the returned list to the requested count. A synthetic regression for that behavior is present in `tests/test_mlx_live.py::test_query_generation_caps_valid_overproduction_to_requested_count`. This code and test predate this audit. They do not prove that overproduction caused the historical target failure.

The existing schema shape diagnostic records root and `queries` types, row-type counts, types and presence of the `target_id` and `query` fields, and blank-query counts. No such diagnostic was recorded for this failure. Even those dimensions alone would not establish whether a syntactically valid target ID belonged to the supplied input IDs. A useful future receipt needs a target-bound, payload-free validation outcome that distinguishes the failed validator branch (for example, missing/wrong field type, blank query, invalid target binding, or response envelope error), along with the structural dimensions. It must not include IDs, query text, memory content, or raw model output.

There is an existing human approval record for the earlier candidate draft, so the 29-candidate state must not be described as a new unapproved draft. No new generation or approval was performed for this audit.

## Authorized schema-only diagnostic attempt (2026-10-21)

The user authorized `scripts/schema_postmortem.py` to parse the JSONL container internally, select only the specified `target_id`, and validate `raw_output` only for matching rows. The local command `.venv/bin/python scripts/schema_postmortem.py` returned `status=no_match`, `scanned_record_count=8`, `matched_count=0`, and `malformed_container_line_count=0` (exit code 1 is the CLI's expected no-match status). Therefore no `raw_output` was passed to either validator, and the historical schema failure was not reproduced. This is not evidence that the target never existed in another log version or that any suspected schema branch caused the earlier failure.

The script and its synthetic tests emit only fixed schema paths, type names, structural counts, stable error codes, and `<redacted len=N>` placeholders. They do not emit source string values or unknown field names. No model run, approval action, or F-H work was performed.

**Validation evidence:** the new synthetic suite has 9 passing tests, including fenced JSON, wrong target binding, malformed and non-string output, unknown field keys, overproduction, non-UTF-8 input, and CLI canary-output checks. Full pytest passed **441 tests in 181.14s**. `ruff check .`, `ruff format --check .` (136 files), and `mypy --strict src tests scripts` (117 source files) passed. The historical schema cause remains unknown.

**Reproduction commands:** `.venv/bin/python scripts/schema_postmortem.py`; `.venv/bin/pytest -q`; `.venv/bin/ruff check .`; `.venv/bin/ruff format --check .`; `.venv/bin/mypy --strict src tests scripts`. The scanner's expected `no_match` result exits with code 1 and reports 8 parsed records, 0 matches, and 0 malformed container lines.

**Repository state at verification:** branch `master`; `git log -3 --oneline` showed `d52f806`, `16117d7`, and `dc58cb3`. These are the three existing commits. The current audit changes were not included in them at the time of this report.
