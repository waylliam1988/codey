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

## Settled assessment and protocol completion follow-up

The user supplied a P1 reproduction: a passing submission published an answer,
but a missing later model `done` was treated as a content rejection. The repair
then changed the report and correctly collided with the immutable original
publication. The earlier last-turn reservation did not address this case.
An added scripted-provider regression reproduced `semantic_review_failed`
before the fix.

After the original submission receipt settles, the shared driver now advances
to its ordinary completion gate and protocol closure without requesting a
model `done`. Native closure sends the original tool result. A completed
negative judgment still authorizes semantic repair; missing assessment or
unfinished protocol closure produces `research_review_incomplete` instead.
A known passing judgment keeps its original synthesis identity and is never
sent through semantic repair. Its created-note projection is restored from
that same receipt. No per-cycle publication identity was introduced.

This supersedes the last-turn reservation above: a valid submission can now
complete on the last decision turn. Native protocol acknowledgements keep
their existing bounded closure mechanism. Fault tests include unavailable
post-submission tools, repeated unexpected native acknowledgement calls, and
real process exits after publication commit and after its settled receipt.

## Resumed local deployment probes

The restarted 5001 service reported idle=1, queue=0 and total_gens=0. A direct
8-token request returned `OK` in about one second; request/response are kept in
`artifacts/research-convergence-service-20261030-b`.

The original frozen failed report was assessed in
`artifacts/research-convergence-archived-review-20261030-b` through the actual
review kernel, before the P1 follow-up. Three real responses repeatedly marked
all five findings and all eleven report paragraphs supported, including f2's
absent end-mark explanation, f4's absent checkpoint modes, and the inaccurate
source-quality paragraph. The first two submissions failed requirement/path
shape checks; the third exhausted the invocation. Final status was failed /
review_incomplete, with no accepted assessment. This is **not** evidence that
semantic mistakes were detected.

A serial diagnostic in `artifacts/research-convergence-quotes-20261030-b`
then asked for separate clause judgments with supporting quotations, including
positive controls and the existing fixed-material medical comparison case.
The f2 diagnostic identified its unsupported causal clauses. The f4 response
also identified the absent modes but emitted a nested JSON string instead of
the required judgment objects; it is a protocol failure. The threshold-only
positive control was semantically supported. The wrong medical comparator
still received a false positive, explicitly equating high-dose combination
therapy with single-drug treatment. Its corresponding correct-comparator
control was supported. The probe does not establish a generally reliable
semantic gate; its raw outputs were preserved and no domain-specific answer
rules were added to production.

The production web retry with a fresh store is running in
`artifacts/research-convergence-web-20261030-b` (original task, native tools,
thinking=false, production server context, 40 decision turns). Final outcome
and focused follow-up validation will be appended after completion.

P1 follow-up verification: **82 tests and 12 subtests passed** across the semantic
review/repair module, actual operation-driver restart, shared operation state,
protocol/execution/frozen-snapshot guards, completion queue, parent synthesis
and pipeline. The semantic module includes four actual process-exit boundaries,
no extra model `done` in both protocols, retained publication under failed
native closure, and no repair when no assessment settled. Ruff, full-production
mypy (571 files), Pyrefly and ty passed again. No full pytest run was requested
or performed.

## Accepted completion at the actual work queue

The next P1 report was reproduced through the real durable research runner,
the production `complete_or_block_work_item` entry, and a SQLite Ghost queue.
The accepted report had an explicitly admitted historical-version evidence
gap, a passing assessment and a satisfied original completion proof. Before
the fix, the queue nevertheless blocked it with `research_proof_partial_answer`.
The queue validated the accepted compilation and then continued into the old
ResearchRecord proof algorithm, which also expects obsolete citation/relation
fields absent from this delivery projection.

Accepted results now return immediately after validating the original frozen
report, completed assessment, publication identity, run/session/project, task
text and original content-addressed completion proof. The returned proof is
the original object, including its proof id; no new semantic judgment or
all-questions-answered policy is introduced by the queue. Real work continuation
passes its admitted task text, including its saved-task wrapper. The queue
stores the original shared proof reference and full publication identity, and
replays those same references from its events. Its older ResearchRecord path
remains for results without an accepted report/assessment.

New regression coverage includes both text and native durable protocols,
accepted explicit gaps, actual queue claim/completion/rebuild/repeated delivery,
and rejection of changed reports, assessments, proof identities, publication,
task or execution scope. A shared proof reference without a valid publication
reference cannot satisfy the queue storage transition. No legacy review is
called for accepted results, including invalid ones.

The focused group passed **95 tests**: accepted queue integration, semantic
review/repair and real process restarts, completion queue, production Ghost
post-turn work queue, queue storage and run ownership. The unchanged tested
sources were then copied to the main worktree. Ruff, mypy (571 production files),
Pyrefly and ty passed there as well. Pyrefly reports zero active errors with
35 suppressed diagnostics and 78 warnings. `git diff --check` passed. No full
pytest suite was run.

The prior frozen-source web run in
`artifacts/research-convergence-web-20261030-b` has now ended. It made 31 actual
generation requests/responses, searched five times, successfully opened pages
seven times, and saved seven sourced candidate findings. It stopped with
`no_progress` after repeated completion proposals omitted required `[fN]`
citations, despite field-specific feedback. It never reached semantic assessment;
there are zero answer versions in its knowledge database. Its candidates remain
unreviewed. The browser-close `TargetClosedError` warning remains visible.
This is a failed live acceptance, not a demonstration of semantic improvement.

The queue patch was prepared in the isolated worktree
`D:/codey-research-queue-20261030` while that original web process retained its
unchanged production implementation. After the process ended, the patch was
integrated for commit before the next serial live run. Non-strict publication
still uses its existing post-kernel entry; sandbox replacement and independent
semantic review are also outside this queue patch.
