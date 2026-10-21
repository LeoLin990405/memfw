# Day 2 policy experiment

This report uses the production pure policy selector with a fixed synthetic input.
Byte usage counts UTF-8 body bytes only; it excludes frontmatter, logs, indexes, archives, and filesystem overhead.

| Strategy | Budget | Kept | Body bytes | Forgotten | Anchors lost | Fits | Proposals |
|---|---:|---:|---:|---:|---:|:---:|---:|
| fifo | 1MB | 33 | 975,484 | 467 | 0 | True | 0 |
| fifo | 10MB | 332 | 9,996,825 | 168 | 0 | True | 0 |
| fifo | 100MB | 500 | 15,085,785 | 0 | 0 | True | 0 |
| lru | 1MB | 33 | 974,552 | 467 | 0 | True | 0 |
| lru | 10MB | 333 | 9,975,223 | 167 | 0 | True | 0 |
| lru | 100MB | 500 | 15,085,785 | 0 | 0 | True | 0 |
| decay | 1MB | 31 | 966,782 | 469 | 0 | True | 0 |
| decay | 10MB | 331 | 9,991,903 | 169 | 0 | True | 0 |
| decay | 100MB | 500 | 15,085,785 | 0 | 0 | True | 0 |
| salience | 1MB | 38 | 993,481 | 462 | 0 | True | 0 |
| salience | 10MB | 327 | 9,977,239 | 173 | 0 | True | 0 |
| salience | 100MB | 500 | 15,085,785 | 0 | 0 | True | 0 |
| consolidate_then_forget | 1MB | 330 | 9,984,867 | 170 | 0 | False | 170 |
| consolidate_then_forget | 10MB | 330 | 9,984,867 | 170 | 0 | True | 170 |
| consolidate_then_forget | 100MB | 500 | 15,085,785 | 0 | 0 | True | 0 |

## Reproduction inputs

- Seed: `20261018`.
- Fixed now: `2026-01-31T00:00:00+00:00`.
- Records: 500 over 30 days.
- Body size: 10,170 to 49,970 UTF-8 bytes by deterministic rule; exact duplicate pairs: 170.
- Total input: 15,085,785 body bytes.
- Budgets use decimal MB: 1 MB = 1,000,000 bytes.
- No LLM or system clock is used. Consolidation uses exact body hashes only.
