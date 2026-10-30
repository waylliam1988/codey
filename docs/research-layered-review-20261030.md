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
