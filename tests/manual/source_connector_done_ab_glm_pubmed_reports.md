# GLM Source Connector Done AB Reports

Generated from live manual results on 2026-08-19.

Result files:

- `source_connector_done_ab-glm-pubmed-baseline-20260819-135054.json`: no atomic row was written before the process was interrupted, so this run is not a valid baseline sample.
- `source_connector_done_ab-glm-pubmed-finalizer-20260819-135054.json`: finalizer-only run, but it stopped on protocol before any `done` attempt.

## Baseline Status

The GLM baseline process did not produce an atomic completed row. The persisted
JSON still shows `complete=false` and `rows=[]`, so it is not counted as a valid
sample.

## Finalizer Metrics

- seconds: 149.056
- turns: 9
- done_attempts: 0
- quality_retry_count: 0
- first_done_passed: false
- eventual_done_passed: false
- connector_valid: false
- opened_target_host: false
- evidence_count: 1
- notes_created: 1
- score: 2
- proof_ok: false
- stop_reason: protocol

## Finalizer Outcome

The finalizer-only GLM run did not reach `done`. It opened the target Nature
source, wrote one knowledge item, then stopped at protocol before producing a
final report. This is a protocol-stability failure, not a done-quality
comparison win.

## Report

No report text was produced because the run stopped before `done`.

