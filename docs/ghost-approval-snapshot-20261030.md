# Ghost approval context snapshot, 2026-10-30

Provider requests now freeze the rendered entry context and Ghost source descriptors
in their existing managed request artifact. Recovery restores these inputs from the
same request that identifies the protocol window. It still reads the current
admit_hints control and checks expiry at the physical send boundary.

API continuations explicitly carry this frozen context in the existing request
observations. Cold canonical replay intentionally excludes Ghost hints, so merely
restoring source descriptors would still fail after a restart. No separate store,
execution cursor, or second publication path was introduced.

Validation:

- 12 production-entry combinations: completions/responses, warm/restarted,
  enabled/revoked/expired. Approval settlement updates continuity before resumption.
- 88 related provider receipt, history recovery, admission and approval tests pass.
- Ruff, mypy, Pyrefly and ty pass on production code.
- Real local Qwen runs in artifacts/ghost-approval-resume-qwen27b-20261030-{a,b,c}.
  Run c uses the final code. All request a real python --version command, pause,
  update continuity using the real projection writer, approve/execute through the
  existing approval workflow, then complete with Python 3.12.8. Two HTTP generation
  requests, final task completed, all effects settled.

Run a is an earlier cold-injection design; b/c use request-scoped injection.
No full pytest or push was performed.
