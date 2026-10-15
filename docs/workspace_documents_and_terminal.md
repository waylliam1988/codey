# Workspace integrity, terminal output and document preview

## Boundaries

An edit has two independent outcomes: its filesystem mutation and the trust of
the subsequent workspace observation. A changed file remains an applied edit
when identity settlement fails. Its receipt retains `changed`,
`mutation_applied`, `identity_status` and a specific `identity_reason`, including
through durable replay. Such a receipt cannot certify a verification.

Read-only inspection remains available after an unconfirmed edit. Further
mutation and verification in that batch remain blocked. After the scan scope
has been repaired, a new verification can adopt a complete current observation
without bumping the revision again or applying the edit again. Old verification
evidence is never restamped with that observation.

## Scan coverage

Identity covers regular file contents within the declared project scan scope.
The fingerprint includes the coverage rule version, built-in excluded directory
names, and the sorted union of configured and request-owned ignored prefixes.
The identity authority reads `.codey/config.json` itself, so direct, recovery
and task-entry observations use the same project policy. Invalid configuration,
unreadable entries, budget exhaustion and files observed changing during their
read cannot yield a trusted fingerprint. Large files use content hashes rather
than a size/mtime substitute.

The current limits are 5,000 files, 500 directories, 1,000 entries per directory,
and 512 MiB of content. Exhaustion is an explicit failure, never a trusted
partial scan. This is a complete bounded observation, not an incremental index
or an atomic filesystem snapshot. Symlinks are excluded by the traversal policy.

The real `E:\Selfie - 副本` project has an explicit `.codey/config.json` excluding
`.cargo-local`, `.rustup-local`, `release`, `EDSDK`, `vendor/models`, `captures`
and `output`. These are outside verification coverage; business source files
remain covered. This configuration fixes the observed toolchain traversal
exhaustion without silently expanding a global limit or ignoring incomplete
reads.

## Process output and command presentation

The common process launcher sets `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`
for both `run` and approved `shell` calls. The pipe contract is UTF-8. Other
programs must emit UTF-8; invalid byte sequences produce an explicit lossy-text
warning, including when the invalid bytes were in discarded output. No code
page is guessed. Capture validates across read chunks, and truncation retains
complete UTF-8 characters at the head and tail boundaries.

`run` retains its verification command policy. An arbitrary command refusal
preserves `command_not_allowed` and supplies `request_shell_approval`, rather
than presenting the refusal as a network failure.

Command cards default to a compact disclosure. Command and retained output have
separate copy controls and bounded scrolling. The output truncation flag
survives UI persistence; truncated output is never advertised as complete.
Denial, timeout, unknown status and nonzero exits cannot appear as success.

## One document reader and pane

`POST /api/documents/read` accepts a session ID and either a workspace path or
a research note ID. It derives the workspace from saved session state, not from
a caller-supplied project root. A previously opened workspace is pinned during
reload, and a changed binding returns `document_scope_changed`.

Workspace files must resolve inside that workspace. UNC/device paths, alternate
streams and escaping symlinks are rejected. Text is limited to 2 MiB; binary,
non-UTF-8, missing and oversized files return explicit states. Research notes
must belong to the session or its project. They use the same content, version,
format and title contract as files.

The shared pane provides session-owned tabs, Markdown/source switching, JSON
formatting, raw content copy, explicit reload preserving reading position, source line references and mouse
or keyboard resizing. Delayed responses cannot replace or recreate the active
document. Markdown uses the existing escaping renderer; raw HTML is not
executed. The renderer normalizes CRLF for parsing while the document reader
and copy action retain the source text.

Chat file links and output filenames, the Research **Open report** action and
individual research note **Open document** actions all enter this pane.

Published SVG icons ship in both wheels and source distributions, together with
the frontend public assets needed to rebuild the page from the source archive.
Archive regressions inspect the actual built wheel and sdist, not only source
file existence or development-server responses.

## Regression and real-project checks

The tests were introduced and observed failing before the corresponding fixes:

- `tests/test_workspace_scan_integrity.py`: directory limits, incomplete reads,
  coverage changes, large-file mutation, applied receipts, replay, and scope
  repair followed by inspection and verification.
- `tests/test_process_text_contract.py`: hostile Python locale settings,
  stdout/stderr Unicode, truncation boundaries, invalid UTF-8 and real delegate
  refusals.
- `tests/test_document_preview.py`: document versions, deletion, ownership,
  workspace rebinding, traversal, alternate streams, symlinks and size limits.
- `frontend/src/command_card.test.ts`, `document_links.test.ts`,
  `document_pane.test.ts`, and `render.test.ts`: copy payloads, status semantics,
  Windows links, source lines, JSON, stale responses and CRLF rendering.
- `tests/test_document_browser.py`: packaged assets and real HTTP, keyboard
  disclosure, bounded output, files, direct research reports, research notes and
  compact long-title tabs.

Run the opt-in real-project checks in PowerShell:

```powershell
$env:UI_SMOKE_PROJECT = 'E:/Selfie - 副本'
python -X utf8 -m pytest tests/test_document_browser.py tests/test_real_workspace_integrity.py -q
```

The workspace check creates and removes its own temporary directory inside the
real project. It uses production editing, reading, `py_compile`, revision and
fingerprint code. The browser check reads the existing API report and opens a
research document stored through the real knowledge store, using a separate
test server and temporary application state. It does not call the external API
or an external model, and does not replace the user's running application.

### Prior focused verification on 2026-10-15 (Windows)

These are the implementation-stage runs, not the later complete-suite results.
Current validation, including the reload-position and archive regressions, is in
[TEST_REPORT](../TEST_REPORT.md).

- Expanded pytest: **3,053 passed, 5 skipped, 222 subtests passed** across
  292 related files, including kernel, recovery, research, workspace and
  subprocess coverage. This was not the entire repository test suite.
- Additional UI/server group: **287 passed** against the final packaged assets.
- Opt-in real Selfie project: **3 passed**, with its temporary probe directory
  removed and the existing API report opened through actual HTTP and Chromium.
- Frontend: **50 Vitest tests passed**; TypeScript, formatting and production
  build passed. The packaged asset manifest matches the source and build.
- Full production tree: **mypy passed 419 files**, Pyrefly reported zero errors,
  and Ruff passed.

Browser evidence is saved under
`.e2e-artifacts/document-preview/selfie-research.png`. Restart an already
running Codey process to load the updated Python routes and runtime code.
