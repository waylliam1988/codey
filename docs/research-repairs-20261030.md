# Research repair verification, 2026-10-30

## Phase-specific completion protocol

The initial native prompt and repair prompt now derive their completion guidance
and example from the current frozen `done` schema. Research requires both
`summary` and `report_sections`; evidence acquisition keeps its summary contract.
The validator still rejects summary-only research reports.

Live run: `artifacts/research-phase-web-qwen27b-20261030-a`, run
`run_f0408239c8604a36968ccc543a7f522e`. It performed real searches, opened official
documentation, saved eight findings and completed synthesis and review. Both
writer windows returned structured report fields, and the earlier final-window
summary-only loop did not occur. This is protocol execution evidence, not semantic
acceptance: manual inspection found unsupported finding scope and omitted version
and workload requirements despite the same-model assessment passing.

The controlled frozen request remains a negative result:
`artifacts/research-phase-guidance-raw-20261030-c` reproduces summary-only output
with the new phase guidance. `native-generation.json` captures the local server's
`/api/extra/generate/check` result immediately after the HTTP response. The raw
generation itself has only `<parameter=summary>`, matching the HTTP arguments.
For this reproduction the field was absent before native parsing. A prompt fix
alone has not made this deployment reliably obey the schema.

Validation before the protocol commit: 59 tests passed in the structured-report,
native-codec, contract-profile and protocol-contract selection. The protocol-contract
file has nine pre-existing failures, reproduced with the parent prompt/preparation
modules (19 passed, nine failed); these assert old coercion and error wording.
The semantic review/structured-report selection additionally passed all 36 tests.
Ruff,
mypy, Pyrefly and ty passed. Previously documented prompt hash and wording tests
still fail on the parent revision and were not rewritten to conceal the baseline.
No full pytest or push was performed.
