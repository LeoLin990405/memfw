# Large artifacts (GitHub Release `data-v1`)

These files are not stored in git. Download them from the release into `reports/` when needed.

| File | Size (bytes) | SHA-256 | Download |
|---|---:|---|---|
| `reports/day3_synthetic.jsonl` | 14613699 | `cbfc689262b775c5b9ca0093c19f9786b94c48bb2c2b5572b12a96c573c41fd1` | https://github.com/LeoLin990405/memfw/releases/download/data-v1/day3_synthetic.jsonl |
| `reports/day3_synthetic.capture.jsonl` | 14532555 | `1003c58ab29dd68c90972609596cd06b5646243a379d5cf35ecf90742b24bf06` | https://github.com/LeoLin990405/memfw/releases/download/data-v1/day3_synthetic.capture.jsonl |
| `reports/day7_official.json` | 12113184 | `87b413399cedb152ed4f6b6aae5b3af3f6e00514ea88a98b18b8e081d2f21d26` | https://github.com/LeoLin990405/memfw/releases/download/data-v1/day7_official.json |
| `reports/day4_baseline_snapshot.zip` | 139326 | `44f47f7859658defc241f52339fae11b7df75cb7531f5c23d7b89c10c34441d7` | https://github.com/LeoLin990405/memfw/releases/download/data-v1/day4_baseline_snapshot.zip |

Download all:

```sh
gh release download data-v1 -R LeoLin990405/memfw -D reports/
shasum -a 256 reports/day3_synthetic.jsonl reports/day3_synthetic.capture.jsonl reports/day7_official.json reports/day4_baseline_snapshot.zip
```

Regenerate instead:

```sh
# Both day3 files (fixed seed, synthetic data)
python scripts/gen_synthetic.py
# Day 7 official LoCoMo evaluation (needs a local copy of public locomo10.json)
python scripts/eval_locomo.py --day7 --data <MEMFW_DATA_DIR>/locomo10.json --output reports/day7_official.json --quick
```

The Day 7 summary numbers are in [day7_report.md](day7_report.md); Day 10 analysis reads `day7_official.json` via `--history`.
