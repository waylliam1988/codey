# Fixed-material diagnostic review, 2026-10-30

Artifacts: `artifacts/research-layered-fixed-local-20261030`. The run contains
18 serial responses: three domains, three levels, two sampling seeds. Provider:
`koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M`, temperature 0.3,
thinking disabled, output cap 1200 tokens. All 18 requests returned stop and
nonempty output. No stronger reference provider was run. This is a small
diagnostic, not a measured general capability estimate.

The fixture's private review rubric is excluded from every model prompt.
Material identity and prompt hashes are recorded by the runner. Raw responses
remain unchanged. The following is an assistant review of the displayed source
and response text, not a new independent semantic approval in the knowledge DB.

| Case and level | Review of both repeats |
| --- | --- |
| SQLite, comprehension | Both retain reader/writer concurrency and one writer at a time. Repeat 2 has awkward “connections can write simultaneously, but take turns” wording; the single-writer constraint is explicit. Neither supplies a measured application throughput. |
| SQLite, extraction | Both retain the correct concurrency constraint and contiguous quotations. Both violate the requested bare JSON format by wrapping it in Markdown fences. The exact-locator test alone cannot certify their claim interpretation. |
| SQLite, synthesis | Both retain one writer, but overstate suitability for the actual unmeasured app. Repeat 2 promises smooth operation without noticeable read waiting; neither supplied source establishes that workload result. Broad semantic acceptance fails. |
| Capecitabine, comprehension | Both distinguish mouse evidence and the nonsignificant monotherapy result, then generalize alternating therapy as superior to monotherapy/dose modulation. Significance against different comparators does not establish that comparison. Broad semantic acceptance fails. |
| Capecitabine, extraction | Both explicitly claim alternating therapy significantly outperforms a single drug. Their quotation compares it with high-dose combination therapy, not the claimed comparator. Both also emit fenced JSON. Semantic and exact-output-contract acceptance fail independently. |
| Capecitabine, synthesis | Repeat 1 keeps the proposal for humans as a clinical-trial hypothesis, but still extrapolates monotherapy's limited effect from one nonsignificant result. Repeat 2 additionally asserts a clinical effective-strategy judgment and superiority over monotherapy. The later mouse/human caveat does not repair these claims. Neither is accepted for broad medical research. |
| Synthetic retail, comprehension | Both report the control group's growth and geographic selection/confounding, and ask for missing data. Repeat 1 opens with causal-sounding “layout brought growth,” later qualified; repeat 2 is more cautious. This is synthetic diagnostic material, never a real business finding. |
| Synthetic retail, extraction | Repeat 1 has located quotations and retains descriptive results, but emits fences. Repeat 2 additionally combines two sources into `S1, S2` and fabricates an ellipsis-joined excerpt. It fails source identity and contiguous-excerpt requirements even if fences are removed for inspection. |
| Synthetic retail, synthesis | Both preserve selection/confounding and missing uncertainty/profit data. Proposed selection mechanisms and geographic standardization are hypotheses/design suggestions, not observed results. These responses are substantially more cautious than the clinical comparisons; they do not establish overall research reliability. |

All six level-2 responses fail the bare-JSON requirement. Inspecting the content
inside their fences is useful for semantic diagnosis, but is not an adapter
that accepts that output in production. The retail repeat-2 locator defect and
the medical comparator defects survive removal of the formatting wrapper.

These results show a mixed failure surface. Short evidence reading can work;
even short fixed-material clinical extraction can introduce a wrong comparator.
Reducing context and improving tools can reduce mechanical burden, but cannot
be claimed to solve this deployment's semantic reliability. Keep extractions
unreviewed and keep test/lifecycle completion separate from research acceptance.

Reproduce with a fresh directory using:

```powershell
.venv/Scripts/python.exe -X utf8 -m tests.manual.research_layered_local --directory artifacts/<fresh-run>
```

For an authorized reference endpoint use the same fixture, levels, repeats,
temperature and output cap, with `--upstream`, `--model`, and optionally
`--api-key-env`. Do not compare different evidence packs or leak the rubric.
Level 4 uses `tests.manual.persistent_research_local --stage research --live-web`
and a fresh knowledge store; its browser ranking can vary between runs.

## Handoff: Runtime Fixes and Live Research

Updated 2026-10-30. The task remains to obtain an accepted, evidence-bound live
Research result from the user's actual local deployment. Do not infer semantic
success from tool completion or a same-model assessment alone. See also
`research-qwen27b-comparison-20261030.md` and
`execution-research-convergence-20261030.md` for earlier trials.

Current deployment is Qwen3.8 27B TurboFCFusion Q4_K_M on KoboldCpp 1.117.1,
`http://127.0.0.1:5001`, Jinja enabled, server context 262144 and output reserve
32768. No external reference provider has been used. Preserve the original
SQLite question at `artifacts/research-convergence-web-20261030-a/question.txt`;
do not add answer-specific hints or relax publication acceptance.

Local commits (not pushed):

- `10917d5c`: replace Node vm execution with a QuickJS/WASM guest, freeze guest
  intrinsics, bind shipped engine assets to recovery, and admit/settle ordinary
  Research publication on the original mutation line. Includes the previously
  isolated review-tool visibility and combined citation-feedback fixes.
- `dcb9d05a`: one bounded reconnect for a closed browser context during fetch
  page preparation; cancellation/deadlines do not retry. Includes the frozen
  request-envelope diagnostic runner.

Focused validation after these changes: 60 CodeMode runtime/isolation/contract
tests, 51 host/state/replay/driver tests, 46 Research restart/semantic-review/
queue/report/snapshot tests, and 60 related browser tests passed. These are
separate groups, not a full pytest run. Ruff, mypy (573 production files),
Pyrefly (0 active errors, 35 suppressed, 79 warnings) and ty passed. Earlier
broader `tests/test_research.py` execution reproduced 10 existing obsolete
fixture/expectation failures in the unchanged secondary worktree; these were
not fixed by weakening production behavior. The exact targeted commands and
all current edits remain reviewable in git.

Latest full web run:
`artifacts/research-convergence-final-web-20261030`, run
`run_bf2fcea3a381457fb818d3fcb3a5afd8`, native tools, thinking enabled,
temperature .3, seed 73, 40 decision turns, 300-second provider timeout.
It saved four sourced candidates, performed actual web_search during follow-up,
then repeated the same summary-only done proposal despite the missing
report_sections error. Actual stop reason was `protocol`; a cancellation request
arrived around its termination and must not relabel that failure. No semantic
assessment or answer publication occurred. Candidate f4 contains additional
SQLITE_BUSY cases absent from its selected excerpt. Keep it unreviewed.
Two page acquisition failures occurred before the browser reconnect fix;
the Playwright shutdown Future/TargetClosedError warning remains unresolved.

Frozen-request probes in `artifacts/research-payload-clock-original-20261030`
and `artifacts/research-payload-clock-merged-20261030` both returned the same
invalid summary-only done call. Both disabled thinking, and the second merged
the clock into the repair message. They dispatch no tools and are not completed
Research runs. There is no demonstrated benefit from clock placement, so the
production clock envelope is unchanged.

Current serial experiment:

```powershell
python -X utf8 -m tests.manual.research_semantic_review_local --source artifacts/research-live-wal-qwen27b-native-off-20261030 --directory artifacts/research-convergence-review-final-20261030 --timeout 240
```

This imports an archived frozen report into a fresh diagnostic store. The import
receipt is explicitly synthetic; only assessment calls are real model work.
Inspect `assessment.json` and raw `wire.jsonl` before interpreting success. The
known defects include unsupported checkpoint modes, incomplete selected support,
missing historical-version coverage and inconsistent source-quality prose.
Its only visible review tool must be submit_research_review, and a settled
submission closes without requiring another model done call.

That archived assessment has now completed in two real requests. The first
submission was rejected for a research requirement with no qN; the second
repaired that field, settled and published the frozen archive with status
passed. **Manual semantic acceptance failed:** the model endorsed the known
wrong historical network-filesystem label, treated retrieval time as support
for a current-stable-version assertion, and accepted checkpoint modes absent
from the selected excerpt. It also classified the historical comparison as a
delivery requirement. The original `assessment.json` is preserved as evidence
of false acceptance. The production label remains same_provider_model_assessment,
not independent certification; do not report this as a semantic improvement.

A fresh full web run is now active, frozen at `dcb9d05a`:

```powershell
python -X utf8 -m tests.manual.persistent_research_local --directory artifacts/research-convergence-web-final-off-20261030 --stage research --live-web --native --question-file artifacts/research-convergence-web-20261030-a/question.txt --max-turns 48 --provider-timeout 300 --seed 74
```

Run id: `run_4ee5e81e9523452d9bc05ad6411a7d2d`. Thinking is explicitly disabled;
the seed and decision budget differ from the failed thinking-enabled run, so
this is an operational trial, not an isolated causal comparison. It has begun
actual searches and page acquisition. Production sources must remain unchanged
until the run and any recovery finish; docs and tests can be updated meanwhile.

After this diagnostic, continue serially with a fresh full web run on frozen
production sources, audit the exact passages, final requirement coverage,
conditions, scope and published knowledge versions. Keep failed attempts and
unaccepted drafts intact. Run only relevant pytest and the four static checks;
the user explicitly does not want full pytest. Stage commits are authorized.
