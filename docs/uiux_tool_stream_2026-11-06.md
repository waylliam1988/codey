# Stable tool stream positions

Date: 2026-11-06. Fixed product copy and this design record are in English.

## Problem and behavior

Completing a tool removed its existing node and appended its replacement. An
earlier call could therefore appear below a later running call. Folding also
searched all previous groups, which could move a post-edit read above the edit.
Failures moved to a separate alert area. Shell promotion removed the generic
call before creating its command card.

Calls now keep their first-observed position within their run and parent.
Completion and failure replace that position. CodeMode retains its children,
and shell results reuse the generic call's slot and map key. Explicit parent
identities still govern attachment; late parents may attach existing orphans.
Missing identities do not imply a reconstructed execution order.

Only consecutive calls of the same kind, run, turn and parent fold together.
Pending and failed calls remain members. Intervening edits, other operations or
reasoning break the segment. Group summaries show running and failure counts.
Failures stay inline; the process summary exposes `1 failed tool` even when
closed. Approval and task recovery controls remain accessible outside history.

Expanded groups and result disclosures survive updates. Retained tool output
keeps its scroll offsets, and focused result controls regain focus without
scrolling. Empty result tracks no longer add a grid row, and command promotion
uses the same process-row spacing. New error text or expanded content may grow
a row; chronological position remains unchanged.

## Automated verification

- Frontend: **333 tests across 54 files passed** (`npm test`). New ordering tests
  cover ten tool kinds, reverse completion, nested and late parents, failures,
  grouping boundaries, command promotion, missing approval IDs, disclosure state,
  output offsets, explicit command failures and retained-history reconstruction.
- Production typecheck/build passed (`npm run build`), followed by
  `python -m tools.publish_frontend` and its `--check` verification.
- **48 scoped Python/browser tests passed** across tool-stream order, process
  toggles, narrow error layouts and manual Retry. The new browser tests use the
  shipped assets and real renderer. They check ordinary completion geometry,
  nested call order, inline failures, command promotion and durable read replay.
  The retained-output refresh explicitly uses `Process.replace`; duplicate
  terminal SSE facts remain intentionally deduplicated.
- **42 additional scoped tests passed** for frontend publication/fingerprints,
  packaged browser assets and SSE deduplication/reset/buffer gaps.
- Scoped Ruff and frontend formatting checks passed. No full pytest run.

Commands, from the repository root unless noted:

```powershell
# From frontend/
npm.cmd test
npm.cmd run build

# From the repository root
.\.venv\Scripts\python.exe -m tools.publish_frontend
.\.venv\Scripts\python.exe -m tools.publish_frontend --check
.\.venv\Scripts\python.exe -m pytest -q tests/test_ui_tool_stream_order.py tests/test_ui_process_group_toggle_updates_latest_without_page_errors.py tests/test_ui_tool_error_layout.py tests/test_ui_manual_retry.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_frontend_build.py tests/test_frontend_source_fingerprint.py tests/test_ui_workflow_uses_packaged_document_and_assets.py tests/test_sse_browser_dedup_reset_and_buffer_gap.py
```

## Native Windows verification

The review launched the current production assets in a pywebview/WebView2 Codey
window at 1380 × 950 requested size (1366 × 943 captured on this desktop).
It used an isolated `LocalService` state home, real HTTP/SSE, and durable
conversation entries. Computer Use opened the process and read group, and
inspected the retained command output. No provider or real shell execution was
needed: tool results were controlled fixtures, matching the approved eight-stage
preview. This verifies presentation and delivery, not model coding quality or
actual command execution.

Observed sequence:

1. CodeMode and two reads start; the read group shows `2 files · 2 running`.
2. The second read completes first; it stays below the first, and the open group
   shows `2 files · 1 running`.
3. An edit follows those reads; the subsequent read stays after the edit.
4. A command begins below CodeMode while the parent remains running.
5. CodeMode completes above that command, retaining its child sequence and the
   user's expanded read group.
6. The command fails in its original slot. The summary shows `1 failed tool`;
   expanding the card reveals `One test failed` and `Exit code 1`.
7. A separate follow-up command succeeds and the final answer appears outside
   the process history. The earlier failed command remains inspectable.

The interaction follows the approved simulation. The implementation retains
Codey's existing quiet typography and disclosure controls; fixed UI text stays
English. Durable history refresh and reconstruction are covered by the scoped browser
test; the native review concerns the live eight-stage interaction.

Evidence:

- [Reverse read completion](reports/tool-stream-20261106/01-reverse-read-completion.jpg)
- [Command starts below the running parent](reports/tool-stream-20261106/02-before-parent-completion.jpg)
- [Completed parent remains above the command](reports/tool-stream-20261106/03-after-parent-completion.jpg)
- [Inline command failure and retained output](reports/tool-stream-20261106/04-failed-command-output.jpg)
- [Final answer and completed summary, native accessibility observation](reports/tool-stream-20261106/05-final-answer-accessibility.txt)
- [Controlled event sequence](reports/tool-stream-20261106/events.json)

The final stage was retained as native accessibility text. Its window was no
longer present for an additional final bitmap; the four earlier images record
the live ordering, grouping and inline-failure checks.

The reproducible fixture is
[`tests/manual/tool_stream_review.py`](../tests/manual/tool_stream_review.py).
Run it with a **new** artifact directory, then write successive stage numbers
`0` through `7` to that directory's `advance.txt`. Write `quit` to close it.
The script reserves its own isolated run and never sends work to a model.
