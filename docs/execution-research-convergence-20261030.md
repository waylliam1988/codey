# Execution and Research convergence, 2026-10-30

The implementation keeps one operation driver, one effect/receipt authority and
the existing knowledge transaction protocol. Research policy and semantic
assessment remain domain rules; they do not acquire their own scheduler.

User changes in `codey/runtime.json` are outside these commits. Local Qwen 27B
validation is authorized; preserve raw outputs and use fresh diagnostic stores.

Implementation batches:

1. Close CodeMode admission on fatal recovery errors, including concurrent
   intent admission and state transactions. Preserve ordinary tool failures and
   settlement of already admitted work.
2. Bind replay to the actual engine, worker/protocol and limits; make the runtime
   factory injectable. State attempt timeout and logical invocation limits
   explicitly. Retain issuance-order result delivery.
3. Give Research invocations explicit purposes and frozen inputs in the existing
   continuation. Protect complete answer publication inputs and original
   references. Make history reconstruction depend on accepted results.
4. Improve complete passage selection and validate task coverage, claim support
   and final report scope through the shared execution path. Model assessment
   must retain reviewer identity and must not become independent certification.
5. Separate model exposure from nested tool availability within one frozen
   contract, retaining authorization, file version checks and host receipts.
6. Run fixed material and real web diagnostics on the available deployment,
   correct observed defects, and record mechanical and content outcomes
   separately. Evaluate a replacement sandbox only against the same host
   protocol and meaningful isolation/recovery checks.

## CodeMode fatal recovery admission

Two new production-kernel cases reproduced the reported defect before the fix:
after catching an uncertain original child receipt, a second read was admitted
and `store()` could change task state. The old parent-only failure test passed.

Replay invalidation now retains its first cause and shares an RLock with the
actual child `begin_tool_batch` boundary and state transactions. The lock is
released before tool execution. Work admitted before closure still settles;
ordinary failed tool results do not close admission.

Validation: 24 replay/production-boundary/durable-scope tests passed, including
both sides of the concurrent admission boundary. Ruff and targeted strict mypy
passed. Broader validation and live results will be recorded below.

## Runtime contract and host limits

Replay now binds the runner, actual Node/V8/ICU/Unicode versions, platform,
architecture, locale/timezone, RPC/determinism versions, result ordering and
complete limits. A runtime factory can replace the backend without changing
tool/effect authority. Tests change the reported engine during real durable
recovery and verify that original children are never re-executed.

`timeout_ms` is explicitly a per-worker-attempt timeout, bounded by any inherited
outer cancellation deadline. There is no new cumulative wall-time budget or
retry counter. The child limit bounds logical program positions, including the
replayed prefix; recovered positions reuse original receipts and do not create
new execution charges. Host code independently bounds worker messages, queued
RPC, total child calls and returned output, even when a worker ignores limits.

Validation: 70 focused CodeMode runtime/contract/replay/production/scope tests
passed. Ruff and strict mypy over CodeMode and the effect sink passed. The Node
VM remains the current backend; a fingerprint is not an isolation guarantee.
