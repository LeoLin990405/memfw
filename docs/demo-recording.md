# Local demo recording

This page explains the method and recording outline for the small LoCoMo portfolio demo. The actual public-data aggregates and measured runs are in [the run record](../reports/portfolio_demo_report.md).

## Method

- The script requires `--data` and reads only the explicitly supplied local JSON file.
- It adapts the first sample, keeps its first 100 turns, and selects up to 20 questions whose valid evidence references are all inside that turn window.
- It creates a temporary vault outside the project, enables production rolling prior-only keyness, and applies FIFO, LRU, decay, salience, and consolidate-then-forget at 25% of canonical stored body bytes.
- It injects no access stream. LRU falls back to creation time and matches FIFO in this demo input; that does not establish policy equivalence.
- It uses the production BM25 scorer with a 128-token whole-turn context and includes a no-forgetting reference arm.
- Consolidation uses deterministic mock proposals in a projection. It does not call the human approval or apply command.
- The output contains only scope counts, retention counts/bytes, aggregate evidence-recall counts, and replay/index stability flags. Runtime is written separately when `--receipt` is supplied.

## Validation before the public-data run

The synthetic-only tests in `tests/test_portfolio_demo.py` passed 6/6. They cover bounded ordered question selection, rejection of evidence outside the turn window, six-arm output, deterministic aggregates, absence of text and identifiers in output, rejection of required-path omissions and pilot-vault paths, pre-read/pre-write rejection of an unsafe temporary root, and separate aggregate/runtime CLI outputs.

## Public-data reproduction

The root agent completed two runs with the explicitly supplied public local corpus; both produced byte-identical aggregate output. See [measured results](../reports/portfolio_demo_report.md). To reproduce, supply the local path explicitly:

```sh
python scripts/portfolio_demo.py \
  --data /path/to/local/locomo.json \
  --table \
  --output /tmp/memfw-demo.json \
  --receipt /tmp/memfw-demo-runtime.json
```

The run record includes a three-minute screen and narration outline. Keep only aggregate output visible. Do not copy the input corpus into the repository or show question text, turn text, sample IDs, evidence IDs, retained memory IDs, or the local corpus path. The result describes a small subset and is not a full-benchmark result or a statistical comparison.
