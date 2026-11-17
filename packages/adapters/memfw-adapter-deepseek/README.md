# memfw-adapter-deepseek

A conversation-worker `ModelBackend` for memfw that labels already-redacted
conversation text through DeepSeek's OpenAI-compatible chat completions API.
It speaks prompt version `conv-v2`: for each chunk and each session it asks
for a summary of at most two sentences plus tags (`project`, `task_type`,
`components`, `intent`, `outcome`). It does not produce memory claims; the
worker keeps those extractive.

Standard library only (`urllib`); depends on `memfw-ports` for the
`ModelBackend` contract. Import it as `memfw_adapter_deepseek`.

Enable it in `vault.yaml` and give the worker process the key through the
environment:

```yaml
conversations:
  backend: "memfw_adapter_deepseek:factory"
  cloud: true
  options: {model: deepseek-chat, max_run_cost_usd: 0.5}
```

The key is read only from `DEEPSEEK_API_KEY`; an `api_key` option is refused.
See `docs/conversations-share.md` in the memfw repository for what is sent,
the privacy controls and the budget.

`memfw_adapter_deepseek.fake.FakeDeepSeek` is a loopback-only fake of the
endpoint for tests.
