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

Only local KoboldCpp is available; there is no reference-model comparison or
semantic certification. Real-web acceptance is evaluated separately below.

2. One bounded ResearchContextView now supplies current questions, short labels,
   pinned content/status/conditions, investigation previews and recent failures.
   It reserves all registered questions and shares remaining space across refs;
   truncation and omissions are explicit. Synthesis handoff supplies intent only.
   Removed duplicate method/action/checklist guidance and the conflicting demand
   to resave historical evidence. save_finding passes an explicit frozen evidence
   context to the common writer instead of changing tools.ledger.
   Focused context/finding/protocol/lifecycle checks: 46 passed; Ruff/mypy clean.

3. Strict done arguments now become a ResearchReportProposal. Questions and
   references are checked before rendering; there is no strict Markdown round
   trip or global inference wording exemption. Report paragraph bindings keep
   their exact fN kinds. Historical evidence is resolved in a pure evidence view,
   never inserted as this run's acquisition. Source keys include identity AND
   revision, preserving two versions at one URL. ResearchReference replaces the
   old finding/knowledge dual maps and is frozen in the common tool receipts.

4. KernelResult carries the accepted compilation in the existing managed
   decision artifact, bound by its digest. Restoration validates it; it never
   recompiles from current knowledge. Archive and ResearchRecord projections use
   this same compilation, selected versions, coverage and evidence metadata.
   Cold start: no legacy research result/mapping reader was added.

5. Review fixes: coverage answer citations use the same paragraph parser and
   must agree with that question's findings; invalid refs get the exact answer
   path. The working view checks its final serialized length. If full questions
   alone do not fit, their wording is explicitly omitted with a budget issue,
   never silently cropped and presented as complete. Reference previews retain
   original evidence; knowledge_read(fN) returns precise evidence/source versions
   and bounded original excerpts in the same read. Excerpt truncation is marked;
   premise evidence of an inference is explicitly distinguished from direct
   evidence. Compact recall locates details without instructing strict reports
   to copy long ids into done.knowledge_refs.

Mechanical completion confirms selected-version resolution and question
accounting, not that a paragraph is semantically supported by its cited finding.
The removed lexical/Markdown matching did not prove that either. Statements
remain unreviewed; coverage is model reported. Missing sources cannot be replaced
with an unbacked hypothesis. An honest report with explicit gaps can be partial
in its ResearchRecord even when delivery is complete.

Fixed-material probe: artifacts/research-layered-contract-view-20261030, level 2,
3 cases, seed 73, temperature .3, thinking disabled. All outputs were fenced,
so raw JSON shape checks failed. After manual fence removal, SQLite's duplicate
claims, an expanded capecitabine comparison (including single-drug arms without
that direct comparator), and retail's multi-source/ellipsis excerpt prevent a
semantic acceptance. Raw prompts/outputs were preserved. This diagnostic does
not exercise the new production working view and is NOT evidence of before/after
research improvement. Final real-web and regression results are pending.

Pre-live validation: 213 targeted tests + 2 subtests passed, including existing
subprocess crash/restart cases and a new SQLite accepted-report replay check.
After field-local coverage parsing and final schema typing, a further 69 focused
checks passed. Ruff, mypy (569 production files), Pyrefly (0 errors; 35 suppressed,
77 warnings) and ty passed. Final archive snapshot adjustments are verified below.

Live probe e007b604: artifacts/research-live-wal-consolidated-20261030. Initial
writer saved f1/f2/f3 but did not complete. It first used an invalid complete
coverage state, then corrected that after feedback, but repeated the missing-q6
repair without saving the missing evidence. The followup performed web_search
and opened an official page and a secondary page; it also saved a reworded f4.
The isolated diagnostic was durably cancelled before changing frozen source.
Its reports still overgeneralized checkpoint completion and exclusive locking;
no semantic acceptance. Cancellation reproduced the pre-existing Playwright
TargetClosedError shutdown warning. KoboldCpp was not restarted or reconfigured.

A concrete interface omission was found: schema_typescript rendered every enum
as string, so the text model could not see coverage's allowed states until a
rejection. The shared renderer now exposes literal enums, bounds, patterns and
map values from the same frozen schema. No state alias or answer-specific prompt
was added. The complex note writer also now uses the shared canonical evidence
schema instead of retaining the old source/quote/p aliases in its declaration.

Live probe dd7c5b9e: artifacts/research-live-wal-visible-constraints-20261030.
The report used legal coverage states without an enum rejection. It still
claimed answers without saved evidence, repeated writes, overstated checkpoint
conditions and called WAL an excellent choice for a read-heavy workload without
measurement. It did not complete; the isolated run was durably cancelled.
There were 36 admitted requests and 29 generated responses (retries included),
with prompt tokens from 2,076 to 16,169. Requests had at most one current working
view. These small, interrupted runs are diagnostics, not a success-rate estimate.

This probe exposed another SYSTEM defect: prepare_evidence_items collapsed
internal whitespace after save_finding computed the claim identity. A multiline
SQLITE_BUSY claim committed its note and differently keyed claim, then failed
to bind its version; unchanged retries reported an identity conflict. Claim
preparation now preserves internal whitespace. The dispatch/idempotency/restart
regression first reproduced the failure, then passed for both single-line and
multiline claims. No compatibility reader or repair of old diagnostic stores
was added. A fresh store is used for the next live probe.

Expanded regression found ten obsolete expectations for manual Markdown/source
table repairs. Replaced those scenarios with current structured-contract
boundaries: plain Markdown cannot complete strict research, opened pages alone
cannot substitute for adopted knowledge, the application uses observed final
URLs, manual source tables are rejected, counter feedback stays field-local,
and feedback never creates evidence. Latest focused run: 120 tests + 2 subtests
passed. Ruff, mypy (569 files), Pyrefly (0 errors, 35 suppressed/77 warnings), and
ty passed. Semantic acceptance remains pending.
