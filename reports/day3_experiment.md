# Day 3 deterministic memory and capture experiment

Policy runs use cloned real vaults and the production budget engine. Total-mode
limits compare actual allocated filesystem bytes. Body bytes are shown separately.

| Policy | Profile | Important recall | Noise retention | Final disk bytes | Satisfied |
|---|---:|---:|---:|---:|:---:|
| fifo | 1MB | 0.007 | 0.018 | 2,527,232 | False |
| fifo | 10MB | 0.088 | 0.222 | 9,981,952 | True |
| fifo | 100MB | 1.000 | 1.000 | 51,933,184 | True |
| lru | 1MB | 0.007 | 0.018 | 2,531,328 | False |
| lru | 10MB | 0.086 | 0.234 | 9,973,760 | True |
| lru | 100MB | 1.000 | 1.000 | 51,933,184 | True |
| decay | 1MB | 0.007 | 0.018 | 2,560,000 | False |
| decay | 10MB | 0.192 | 0.138 | 9,973,760 | True |
| decay | 100MB | 1.000 | 1.000 | 51,933,184 | True |
| salience | 1MB | 0.007 | 0.018 | 2,568,192 | False |
| salience | 10MB | 0.146 | 0.180 | 9,965,568 | True |
| salience | 100MB | 1.000 | 1.000 | 51,933,184 | True |
| consolidate_then_forget | 1MB | 0.007 | 0.018 | 3,092,480 | False |
| consolidate_then_forget | 10MB | 0.075 | 0.222 | 9,990,144 | True |
| consolidate_then_forget | 100MB | 1.000 | 1.000 | 51,933,184 | True |

## Capture comparison

| Mode | Physical bytes | Duty cycle | Missed important | False wakes | Noise retention |
|---|---:|---:|---:|---:|---:|
| always_on | 17,260,544 | 1.000 | 0 | 0 | 1.000 |
| intensity_awake | 7,995,392 | 0.361 | 269 | 13 | 0.347 |

## Reproduction and metric formulas

- Seed `20261018`, fixed UTC time `2026-10-18T00:00:00+00:00`.
- 1000 records across 30 days and 7 themes.
- Important recall = retained important source IDs / all important source IDs.
- Merged source IDs count as retained when recorded in the representative's provenance. Noise retention = retained noise source IDs / all noise source IDs.
- Capture storage uses physical bytes from both real vaults. Both variants use the same session, log, and staging path.
- Duty cycle = elapsed FULL-state seconds / elapsed stream seconds.
- False wake count = model-caused FULL transitions at noise-labeled input timestamps. Rate divides by all model-caused FULL transitions; always-on has no automatic wake denominator and reports null.
- Quiet noise capture rate = staged noise IDs during quiet windows / all quiet-window IDs.
- Intensity capture threshold = 0.65.
- Ten protected anchors are checked against actual final anchor documents.
- The 1 MB total physical limit can remain unsatisfied because event logs, retained history, and protected anchors count toward allocated bytes.
- Missed important = important source IDs absent from staged candidates.
- Consolidation uses synthetic-only human-approved proposals, then applies the configured FIFO total-budget fallback. Proposals come from the production deterministic judge, not ground-truth duplicate labels.
- Synthetic data limitation: Body sizes are synthetically padded with opaque random traces. Themes are assigned in contiguous time blocks, so topic and time are not independent. This measures runtime behavior, not natural conversational memory quality.
- Policy features: One hundred access events are timestamped across the corpus at a fixed one-in-ten index interval; reference counts, recency intervals, and sparse manual marks vary by index and are independent of ground-truth importance.
- No model or network calls are used.
