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

## Explicit Research invocations and answer identity

The existing continuation now records invocation purpose, original plan/result
references and frozen domain input digest. Stable operation positions are
translated in `ResearchInvocation` only. A recovered invocation cannot silently
change its purpose, prompt, scope, policy or plan. Continuation schema 2 rejects
old incomplete descriptors rather than inferring a new invocation.

Answer publication now checks its full compilation, exact premise versions,
scope, questions, coverage, gaps and source selection, as well as text. A failed
required answer transaction propagates as an explicit continuation failure.
Model-extracted claims remain candidate/unreviewed.

Validation: the original continuation/contract combination passed 38 tests;
29 contract tests pass after adding frozen-input changes. Knowledge lifecycle,
report snapshot, findings and policy combinations passed 40 tests; the expanded
combination passed 58 before fixing a three-case new fixture construction error,
then its 29-test contract module passed. A new real subprocess exit immediately
after answer publication passed: resume reused the original answer and versions
without another provider request or source fetch. Ruff and targeted mypy pass.

Still pending: publication effect admission/settlement around the knowledge
transaction, history projection reconstruction, semantic review/repair, tool
exposure separation, sandbox comparison, final static checks and live probes.
No live model requests have been made in these implementation batches yet.

## Complete evidence selection (in progress)

`save_finding` now accepts up to three additional already-read passages from
the same exact source version. Every fragment retains its separate exact quote
and locator, including receipt restoration and idempotent retry. Cross-version
selection is rejected before writing. Passage splitting prefers paragraphs and
sentence endings within the actually visible window.

The findings/report-snapshot groups pass (15 tests). An expanded legacy
protocol-contract module has nine failing expectations about coercion,
unknown-field dropping, legacy synthesis writes and old error wording; no
production validation was relaxed. Baseline confirmation and fixture updates
remain to be recorded. Semantic assessment/repair is currently under test;
it is not yet a validated completed batch.

Baseline confirmation: an isolated worktree at `7058de7e` reproduces exactly
the same nine protocol-contract failures (19 passing). This is preexisting
fixture/contract debt, not introduced by multi-passage selection. The worktree
is at `artifacts/convergence-baseline-7058de7e`.

## Semantic assessment and publication (implementation under validation)

Production strict Research now defers answer publication until a bounded
assessment of the original task, every selected finding/condition with FULL
exact excerpts, and every report paragraph. Assessment runs in a fresh provider
window through `ResearchContinuation.kernel`, under the same operation and
global turn budget. At most two ordinary synthesis repairs follow concrete
assessment issues. No private provider loop or execution journal is added.
Missing/uncertain assessment fails closed; the earlier draft is never returned
as `done`. Saved claims remain candidate/unreviewed and the review is explicitly
identified as a same-provider model assessment, not independent certification.

`submit_research_review` is exposed only in the review invocation. It validates
the complete assessment and publishes a passing frozen report through the normal
effect admission, idempotent knowledge transaction and original result receipt.
Only this frozen publication effect can replay in Research; network acquisition
still cannot. A real subprocess exit after the synthesis knowledge transaction
but before effect settlement recovers using the original provider response,
assessment, compilation and commit identity. Failures publish no answer. The
final proof includes explicit model-assessment checks and receipt references.

The accepted compilation is stored in answer provenance; history can rebuild
from that immutable snapshot without latest-version resolution, model calls or
network. The matching accepted projection and assessment, rather than the
optional EvidenceLedgerStore cache, are the new strict queue input.

Validation so far: semantic review/repair group passes 7 tests, including real
subprocess exits before review interpretation, after its settled kernel and
after publication commit. A focused Research/runtime/replay/history combination
passes 70 tests plus 43 subtests. Further meaningful combined validation is
running after final proof/schema changes.

## Frozen model and nested tool views (under final validation)

One frozen inventory now has a direct model exposure view and a CodeMode
capability view. The optional exposure filter only narrows policy/controller
authorization. Provider schemas and repair prompts show direct tools; CodeMode
declarations and execution use nested capabilities from the same definitions.
Nested execution in durable production requires the admitted parent effect.
Snapshot persistence schema 2 carries both views; schema 1 remains readable.
No specialized file tools, source receipts or read-before-edit checks were
replaced. A 55-test CodeMode/snapshot combination passed, including hidden-read
execution with real parent/child effects and rejection of direct hidden calls.

## Live service probe

`artifacts/research-convergence-web-20261030-a` preserves the first attempted
web run (fresh store, actual production 262144 context, thinking=false,
native tools, max_turns=40). The FIRST model request received no response
headers within 600 seconds: `GenerationUnknownError`, no research tools ran.
The KoboldCpp performance generation count remained 38; its check endpoint
continued showing an unrelated previous generation. A direct urllib minimum
chat-completions request (8 output tokens) also timed out after 30 seconds.
This reproduces outside Codey; it is not evidence of semantic review quality.
No automatic replay, server restart or configuration change was performed.
User was asked asynchronously to confirm the active idle endpoint; code and
focused validation continue independently.

Still to do: final focused combined checks, all four static gates, archived
failure model diagnostic and real webpage acceptance when the generation
service responds, final commits and an explicit sandbox-backend decision.

## Final boundary audit (in progress)

The resumed audit found that Research successors retained `turns_used` without
charging their settled predecessor. Twelve physical decision requests could
therefore finish under an eight-turn operation budget. Successor admission now
charges the settled logical turns atomically in the existing operation state;
recovery and repeated admission reuse the same charge. There is no new counter
or budget owner. A passing review cannot publish on the last remaining turn,
when its required completion receipt could no longer be requested.

The completion projection additionally validates all submitted judgments and
their three model-assessment checks, exact effect/result refs and synthesis ID.
Clearing a history cache does not remove those acceptance requirements. Missing
task feedback now retains both the `task_coverage` issue category and its
research/delivery requirement kind.

Earlier recovery checks ran across a source-edit window and correctly rejected
different implementation fingerprints. Those are not accepted recovery results;
the final combined run uses frozen production sources.

Backend decision: retain the injectable Node VM backend for this change. No
replacement sandbox has demonstrated isolation, determinism and subprocess
recovery under this host protocol. Runtime fingerprints and host quotas improve
correctness but do not establish hostile-code isolation. A replacement backend
and independent semantic reviewer remain separate, unimplemented work.

## Validated handoff

Frozen-source validation passed:

- Research reports, exact evidence, continuation contracts, pipeline, knowledge
  lifecycle, completion queue, semantic repair, safe replay, frozen tool views
  and both real-process recovery modules: **143 passed, 15 subtests passed**.
- CodeMode integration, replay authority, production boundaries, durable scope,
  execution/repair snapshots, frozen definitions and shared operation state:
  **61 passed, 12 subtests passed**. These are separate test module lists.
- `ruff check .`, `python -m mypy codey --python-version 3.11` (571 files),
  `pyrefly check codey --python-version 3.11`, and
  `ty check codey --python-version 3.11`: passed. Pyrefly still reports 35
  suppressed diagnostics and 78 warnings; zero active errors is the gate.
- `git diff --check` and source-size inventory passed. No full pytest suite,
  frontend rebuild, replacement-sandbox experiment or independent semantic
  certification was run in this batch. The earlier baseline protocol-fixture
  debt and browser-close warning are not resolved by these changes.

The two new production modules are added to the runtime descriptor; unrelated
preexisting descriptor edits stay outside this commit.

Live acceptance remains **blocked by generation service availability**. The
latest minimal direct request (4 output tokens, thinking=false, 15s timeout)
also did not return. Its server-side queued request may remain pending; further
requests were stopped. `/v1/models` identifies Qwen3.8 27B, but `/api/extra/perf`
still has total_gens=38, idle=0 and queue=2. The second queue entry followed
that direct probe. No research-content improvement is claimed from this.

Offline archived basis preparation was rerun successfully against the final
implementation at `artifacts/research-convergence-archived-basis-final-20261030`:
original report, five
selected findings, full exact excerpts and original task. To assess it when
the service is healthy, use a NEW output directory:

```powershell
python -X utf8 -m tests.manual.research_semantic_review_local --source artifacts/research-live-wal-qwen27b-native-off-20261030 --directory artifacts/research-convergence-archived-review-next
```

This harness explicitly marks the archive import receipt as synthetic. Only the
assessment requests are real provider work through the production kernel.
After inspecting `assessment.json` and unchanged raw `wire.jsonl`, run the real
web task with a fresh store and the original question:

```powershell
python -X utf8 -m tests.manual.persistent_research_local --directory artifacts/research-convergence-web-next --stage research --live-web --native --question-file artifacts/research-convergence-web-20261030-a/question.txt --max-turns 40
```

Remaining live work: inspect whether the deployment catches the archived
unsupported clauses/missing version comparison; exercise repair on real web
material; verify exact findings, requirement coverage, source prose and final
scope by hand; investigate any newly observed execution defect. Do not treat a
passing same-model assessment alone as independent content acceptance.

Implementation commit: `b9209c0b` (following the four earlier implementation
batches listed in Git). The remaining working-tree descriptor changes are the
preexisting user changes; only this batch's two new module entries were staged.
Commits are local; this session has not pushed them to GitHub.
