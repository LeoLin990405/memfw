# Portfolio demo run record

## Scope

This is a bounded local demonstration, not the full LoCoMo evaluation and not a statistical comparison. The command read the explicitly supplied public LoCoMo JSON file. The input corpus remained outside the repository; the script created a temporary vault outside the project and emitted aggregate metrics only.

The first sample has 419 turns and 199 questions, of which 197 contain valid evidence. The demo kept the first 100 turns. Of the sample questions, 57 had all valid evidence within that window; the demo selected the first 20 in source order. Those questions contribute 26 per-question-deduplicated evidence references; references may repeat across questions, so this is not a globally unique ID count. Retrieval uses the production BM25 scorer and a 128-token whole-turn context. Five retention policies use a 25% canonical-body byte budget of 3,583 bytes. The no-forgetting reference retains all 100 turns without that cap.

## Aggregate results

| Arm | Retained turns | Retained body bytes | Evidence retrieved / total | Evidence recall |
|---|---:|---:|---:|---:|
| FIFO | 27 | 3,268 | 3 / 26 | 11.54% |
| LRU | 27 | 3,268 | 3 / 26 | 11.54% |
| Decay | 23 | 3,540 | 3 / 26 | 11.54% |
| Salience | 13 | 3,241 | 4 / 26 | 15.38% |
| Consolidate then forget | 27 | 3,268 | 3 / 26 | 11.54% |
| No-forgetting reference | 100 | 14,334 | 8 / 26 | 30.77% |

The demo injected no access stream. LRU therefore falls back to creation time, which matches FIFO on this input; this is a property of this demo setup and does not establish policy equivalence. The consolidation projection found no mock proposals in this bounded sample and used FIFO fallback. It did not invoke human approval or mutate an external vault. The no-forgetting reference is uncapped, so its byte count is not comparable to the five budgeted arms as a storage target.

## Reproducibility and validation

The public corpus SHA-256 matches the input recorded by the official Day 7 run: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`. This hash comparison used the existing report metadata; the corpus contents were not reread for this report edit.

The first measured run took **1.037921 seconds**. A second run took **0.833168 seconds**. Both aggregate JSON outputs were byte-identical with SHA-256 `7df33e945dc8c5cd5c026dc6ced0fae2ed12ffaecd97df4dd0c2d4c56f00218f`. Event replay, index rebuild count, and BM25 search results remained stable across the rebuild check. Runtime varies by machine and is not a performance guarantee.

The aggregate output is [portfolio_demo.json](portfolio_demo.json); the separate first-run timing receipt is [portfolio_demo_runtime.json](portfolio_demo_runtime.json). These files contain counts and metrics, not conversation text, question text, sample IDs, evidence IDs, or retained memory IDs.

## Reproduction command

Supply the approved public local corpus explicitly. The shell example below expects it at `$HOME/Documents/memfw-data/locomo10.json` and writes only the aggregate result and runtime receipt under `/tmp`:

```sh
.venv/bin/python scripts/portfolio_demo.py \
  --data "$HOME/Documents/memfw-data/locomo10.json" \
  --table \
  --output /tmp/memfw-demo.json \
  --receipt /tmp/memfw-demo-runtime.json
```

## Three-minute recording outline

| Time | Screen and narration |
|---|---|
| 0:00–0:25 | Introduce the problem: personal agents need useful long-term memory, while local capacity and the user's right to forget impose limits. State that this is a small local evaluation, not a product-quality claim. |
| 0:25–0:55 | Show the README architecture diagram. Explain that Markdown stores current memory, the event log records changes, SQLite is rebuildable, and retention policies make bounded plans. |
| 0:55–1:20 | Show the synthetic CLI quick start. Point out that the example writes only to `localdata/demo-vault` and needs no model. |
| 1:20–1:45 | Run the command above using an explicitly supplied public local corpus path. Show only the terminal table and aggregate JSON. The measured first run took about one second on the recording machine; the timing can vary. |
| 1:45–2:25 | Walk through the six arms and the scope: first sample, 100 turns, 20 selected questions, 26 per-question-deduplicated evidence references (which may repeat across questions), five policies at 25%, plus an uncapped no-forgetting reference. Explain that evidence recall is not answer accuracy. |
| 2:25–2:45 | Note that consolidation produced no proposal in this subset and used FIFO fallback. Explain that no human approval was simulated or invoked by this demo. |
| 2:45–3:00 | Close with limits: one bounded sample subset, no statistical comparison, and no generalization claim. Point viewers to the run record and full Day 7 report. |

Keep all user or corpus text off screen. Show aggregate fields only; do not reveal the local corpus path in the recording.
