# Test Report (2026-10-05)

## Commit validation

Commit `6c811c31ba62883457bb0041d050c26632c61f77` passed GitHub CI run
[37300670289](https://github.com/waylliam1988/codey/actions/runs/37300670289).
The Windows matrix ran the full command
`python -m pytest -q -o faulthandler_timeout=120` on Python 3.11, 3.12, and
3.13. Each version reported **7298 passed, 13 skipped, 1497 subtests passed**;
durations were 344.08s, 607.47s, and 462.40s. The Linux file-boundary suite
reported 169 passed, and the required machine contract gate reported 341 passed.

## Web-provider UI gate

The Qwen research-only run passed after the commit. The gate verified Qwen
provider selection, the Research receipt, and the Evidence, Sources, Graph, and
Notes drawer tabs. Its history is retained in
`.e2e-artifacts/qwen-gate-research/local-model-provider-history.jsonl` and records
The successful main sequence was `web_search -> open_url -> knowledge_write -> done`,
followed by one bounded evidence-only `knowledge_write`. The history has one
`done`, zero completion rejections, zero JSONL parse errors, and zero transport errors.

No local full pytest or web-provider rerun was needed after these recorded
results.
