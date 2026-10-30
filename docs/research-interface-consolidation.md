# Research interface consolidation — 2026-10-30

Scope: consolidate existing interfaces/context/report projections, preserving the
operation driver, effect admission, budgets, knowledge transactions and recovery.
Cold start: no compatibility reader for replaced research wire formats.

1. Complete input schemas now belong to ToolContract. ToolSpec is a conversion;
   the kernel validates the received frozen spec, without consulting a second
   live research declaration. Reusable nested builders remain in knowledge_schema.
   done purpose selection is centralized before freezing. Removed ToolArg and
   coercion/default parser. Expanded the existing common schema subset to enforce
   string/array bounds, patterns, and map values; no new validator framework.
   Profiles/examples/native/text/frozen-contract regressions pass. The first
   focused runs: 41 tests + 2 subtests and 29 additional tests passed; mypy clean.

Next: one bounded working view and explicit evidence input; structured report
proposal/check/render; persist the accepted compilation; one exact reference
binding and versioned evidence view. Fixed/local-model and real-web acceptance
remain pending. Only local KoboldCpp is available; do not imply a reference-model
comparison or semantic certification.

2. One bounded ResearchContextView now supplies current questions, short labels,
   pinned content/status/conditions, investigation previews and recent failures.
   It reserves all registered questions and shares remaining space across refs;
   truncation and omissions are explicit. Synthesis handoff supplies intent only.
   Removed duplicate method/action/checklist guidance and the conflicting demand
   to resave historical evidence. save_finding passes an explicit frozen evidence
   context to the common writer instead of changing tools.ledger.
   Focused context/finding/protocol/lifecycle checks: 46 passed; Ruff/mypy clean.
