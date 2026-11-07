# Attachment implementation verification — 2026-11-07

Scope: only composer attachment interaction. Normal states have no attachment
status sentence. Neither current-task/model/branch hints nor `Attachment pending`
are added to the product.

## Automated checks

- Frontend: **341 passed**, across 55 Vitest files.
- Selected Python suites, including shipped-frontend browser workflows:
  **116 passed, 6 subtests passed**. No full pytest run.
- TypeScript production build, changed-file Ruff checks, frontend formatting,
  and `python -m tools.publish_frontend --check` pass.

The selected Python invocation covers attachment storage/delivery, task admission,
the durable input queue, lazy server startup, Cline model selection, provider
snapshots and cold-start selection, context budgets, Responses tool history and
compaction, attachment browser workflows, and the existing single-action composer
regressions. Relevant entry points:

```
tests/test_input_attachments.py
tests/test_ui_attachments.py
tests/test_ui_composer_primary_action.py
frontend/src/attachments.test.ts
frontend/src/input_outbox.test.ts
```

Coverage includes session isolation, malformed/oversize files, immutable content
integrity, canonical metadata and public history, fork visibility, both actual
image protocol serializers, explicit model modalities, browser text delivery,
pre-send file budgets, and excluding a future task's files from a current send.
Browser cases cover picker/drop/paste, error-only UI at 1280px and 480px, reload,
Retry, rejected-input recovery without overwriting a newer draft, lost delivery
replies, actual consumed Steer metadata, and Stop targeting an idle attachment's
retained delivery identity.

## Native desktop observation

The real Windows Codey/WebView2 window, titled `Codey | Attachment review`, was
inspected using Computer Use in an isolated local-service state directory.
The fixture imports a real small PNG through the attachment storage route and
seeds only its initial draft reference. HTTP, capability checks, preview reads,
SSE updates, and rendering use the production implementation.

1. With no confirmed image support on the controlled active run, the file chip
   and thumbnail remain visible. Send is the sole primary action and is disabled.
   The error is exactly the approved unsupported-image sentence.
2. With a new controlled active run declaring image support, the retained image
   remains visible, the error disappears, and Send becomes available. There is
   no normal attachment status line or task/model/branch context row.

The fixture window and its isolated service were closed after review. The user's
other application windows and project state were not used for the test.
Reproduce with `python -m tests.manual.attachment_review --artifacts <new-folder>`;
write `supported`, then `quit`, to that folder's `advance.txt`.

This is UI and protocol verification with controlled run/model facts, not a claim
of a live external vision-model invocation. Production image eligibility remains
conservative and is described in [the attachment contract](../attachments_2026-11-07.md).
