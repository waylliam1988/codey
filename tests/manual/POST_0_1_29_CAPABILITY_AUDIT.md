# Post-0.1.29 Capability Audit

Date: 2026-07-11

This audit asks a narrower question than “does every release reduce turns?”:
does each hidden capability improve the workflow it was designed for, without
adding an unacceptable reliability or complexity cost?

## Method

- Providers: DeepSeek, GLM, Qwen, MiMo through live Edge CDP tabs.
- Navigation project: the real `stockalarm.py` codebase, including a 788 KiB,
  roughly 14k-line source file. The benchmark hard-disabled edit, write, and run.
- Modification fixture: a temporary Python project where a changed function had
  multiple callers and tests. Independent assertions and `unittest` verified the
  result after the temporary project was deleted.
- Recovery fixture: a temporary project forced to stop immediately after its
  first real edit, then resumed in a fresh web conversation, independently
  tested, reviewed, and checked for checkpoint deletion.
- Verification review fixture: the same diff was reviewed with and without a
  Verification Map. It changed normalization behavior, had an existing related
  test file, and had no successful check after the edit.
- Web-provider timings are observational. Model variance and throttling make a
  single elapsed-time result unsuitable as a release gate.

## Results by capability

### 0.1.25 Hidden Project Map: keep

Natural navigation A/B showed conditional rather than universal efficiency:

| Provider | Baseline | Current | Finding |
| --- | --- | --- | --- |
| DeepSeek | hit 14 turns without finishing | finished in 11 turns | clear completion benefit |
| GLM | noisy before search/quote fixes | completed accurately after fixes | useful context, timing inconclusive |
| Qwen | 12 turns | 12 turns | neutral in this task |
| MiMo | 9 turns | 11-14 turns across runs | high model variance |

Project Map should be retained because it is deterministic, bounded, shared by
Writer/advisors/Review, and helped DeepSeek complete. It must not be described
as a universal turn or token reduction.

### 0.1.26 Outline File: removed after audit

Controlled prompts showed that providers could use `outline_file`, but natural
tasks overwhelmingly preferred grep/read and did not demonstrate enough benefit
to justify a separate protocol, hidden-audit path, parser, and test surface. The
tool was removed without a compatibility alias. Large-file navigation now uses
literal grep followed by offset `read_file`; impact navigation uses
`find_references`.

### 0.1.27 Find References: strongly keep

All four providers completed the reference-aware modification and passed
independent verification:

| Provider | Turns | Outcome |
| --- | ---: | --- |
| DeepSeek | 8 | updated implementation and affected caller; 3 tests passed |
| GLM | 7 | used batched reads; distinguished affected/unaffected callers |
| Qwen | 7 | updated both required sites; 3 tests passed |
| MiMo | 12 | recovered from parallel/protocol and newline formatting errors; passed |

This is direct evidence that bounded lexical reference hints reduce the risk of
missing callers. The tool should remain lexical and bounded; no LSP, index, or
semantic-call-graph expansion is justified yet.

### 0.1.28 Durable Execution Checkpoint: strongly keep

Every provider acted as Writer in a forced-interruption recovery flow. The first
run stopped after a real edit, the second fresh conversation received the local
checkpoint, and independent tests passed. DeepSeek, MiMo, and Qwen completed
directly. GLM completed after a provider-specific, compile-gated repair for smart
quotes in full Python `edit(content=...)` commands. Successful flows deleted the
checkpoint only after verification and Review.

The checkpoint restores local execution facts rather than model plans. It adds
clear reliability value even when it does not reduce turns during uninterrupted
tasks.

### 0.1.29 Verification Map: strongly keep as advisory evidence

Review A/B used the same diff with no post-edit check:

| Provider | Without map | With map |
| --- | --- | --- |
| DeepSeek | approved and missed verification | requested concrete tests in existing `tests/test_auth.py` |
| GLM | approved | requested tests and raised an additional compatibility risk for Writer verification |
| MiMo | noticed missing verification generically | named the real test file and specific assertions |
| Qwen | noticed missing tests generically | named the real test file and project-wide pytest candidate |

The map improves specificity and, for DeepSeek/GLM, caught a verification gap
that baseline Review missed. It can also increase reviewer sensitivity or expose
model misconceptions. Therefore Review findings must remain advisory and return
to Writer for validation; a single reviewer approval must not become a hard gate.
The recovery smoke was corrected to model this real behavior: a reviewer finding
is followed by Writer verification and an independent local check before the
checkpoint is deleted. In one GLM/Qwen run, Qwen repeatedly misclassified valid
one-space Python indentation as a syntax error; DeepSeek and GLM did not. The
final local check still passed, so this is recorded as Reviewer model variance,
not a checkpoint or Verification Map failure.

## Bottlenecks found and fixed

1. Literal grep silently skipped files over 512 KiB and returned a clean
   no-match result. It now searches files up to 8 MiB, has a 16 MiB cumulative
   read budget, counts failed decode attempts, and marks omitted content.
2. The public grep contract used `pattern`, encouraging unsupported regex. The
   public argument is now `query`, and the contract explicitly says literal,
   case-insensitive, no regex. Legacy `pattern` input remains tolerated at the
   parser boundary so cached web-model prompts fail safely.
3. Hidden project-audit search had the same large-file blind spot. It now uses
   the bounded large-file search while preserving stricter secret, symlink, and
   direct-read limits.
4. GLM may typographically rewrite quotes in JSON and Python source. Structural
   JSON smart quotes are repaired without changing prose. Full `.py` content is
   quote-normalized only if the original fails `compile()` and the candidate
   succeeds.
5. A proposed “finish when evidence is sufficient” system-prompt rule did not
   improve DeepSeek in live testing and was removed rather than adding permanent
   prompt tokens.

No new DOM regression was observed: DeepSeek did not enter resend state, MiMo
did not click upload/stop, and Qwen submitted normally. Provider generation time
remains the dominant latency for MiMo and Qwen.

## Final classification

- Strongly justified: `find_references`, Durable Execution Checkpoint,
  Verification Map as advisory evidence.
- Justified with conditional efficiency: Hidden Project Map.
- Removed after weak natural-use evidence: `outline_file`.
- Not justified: claims that all post-0.1.25 features universally reduce tokens
  or tool turns.

The next engineering step should be repeated benchmark collection, not another
production tool: at least three runs per provider/task, median and range, and
separate Navigation, Modification, Verification, and Recovery scorecards.
