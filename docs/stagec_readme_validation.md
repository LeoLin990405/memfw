# Stage C README validation

| Check | Result | Evidence |
|---|---|---|
| Full test suite | 420 passed; 203.27 s | Local `pytest` run |
| Ruff lint | Passed | Stage C local validation |
| Ruff format | Passed; 124 files checked | Stage C local validation |
| Strict mypy | Passed; 111 files checked | Stage C local validation |
| Synthetic CLI quickstart | 5 commands passed | Stage C local validation |
| README relative links | 0 missing | Stage C local validation |
| LoCoMo sample count | 10 conversations | `reports/day7_report.md`, lines 5 and 34; JSON `sample_count` |
| LoCoMo input totals | 5,882 turns; 1,986 questions | `reports/day7_report.md`, lines 5 and 34 |
| Valid-evidence questions | 1,982 | `reports/day7_report.md`, line 34 |
| Bootstrap | 1,000 paired within-conversation question resamples | `reports/day7_report.md`, line 67; JSON `bootstrap_repeats` |
| Primary comparison family | 5 BM25 policies; 10 Holm-adjusted contrasts per scope | `reports/day7_report.md`, lines 19 and 67 |
| Overall result | 0 significant adjusted contrasts in 4 overall scopes | `reports/day7_report.md`, lines 205 and 7 |
| Category result boundary | One within-category adjusted contrast; exploratory across categories/scopes | `reports/day7_report.md`, lines 7 and 205 |

Status: README and archived legacy notes are complete. No public corpus was read for this validation. Git initialization is unavailable in this checkout.
