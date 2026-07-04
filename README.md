# Codey

**Use web AI models as a local coding assistant.**

[中文说明](README.zh-CN.md)

Codey connects to AI chat websites you already use, such as DeepSeek, Qwen, and Xiaomi MiMo, and gives them a controlled local tool loop: read files, edit files, run tests, show diffs, and restore changes.

It is a local-first, low-cost AI coding workspace built for multiple web models.

No API key required. No model subscription wiring. Log in to the web AI in Edge, pick a local project folder, and start building.

Version: `0.1.14`

---

## Why

AI-assisted programming should not require expensive API credits or complicated setup.

Codey is an experiment in making AI coding more accessible:

- use browser-based AI models
- keep code on your own machine
- see exactly what changed
- restore changes when needed
- let beginners start before they understand Git

The goal is not magic. The goal is a small, usable bridge from idea to working code.

---

## What It Can Do

- Chat with DeepSeek, MiMo, or Qwen from a local control panel
- Let the model read and modify files in a selected project folder
- Run tests and feed results back to the model
- Show red/green diffs
- Show a compact task receipt after each run, such as `DONE · 2 files changed · checks passed · restore available`
- Restore snapshot changes without requiring Git
- Use Git when available, but not require it
- Retry with another model when one model fails
- Use two open web models together: one writes, the other reviews
- Continue long conversations quietly with an automatic factual summary and fresh model chat
- Reuse project commands that have already succeeded locally
- Resume the same chat after a Codey restart from one bounded factual snapshot
- Keep non-Git diff and restore available across Codey restarts
- Recover from small provider-page DOM changes with bounded, verified discovery
- Stop a running provider wait, review, recovery, or test command promptly
- Preserve both the beginning and end of long command output
- Recover the active run, approval, or teaching prompt after a UI reconnect
- Prevent uncertain provider submissions from being sent twice
- Record compact provider failure diagnostics for debugging web-page breakage

---

## Supported Models

| Model | Status |
|---|---|
| DeepSeek Web | Tested |
| Xiaomi MiMo Chat | Tested |
| Qwen Studio | Tested |

Codey uses browser automation, so websites may break after UI changes. The current design keeps provider-specific code isolated so those adapters can be repaired without changing the agent core.

Version `0.1.14` keeps the interface unchanged while making the local tool protocol more consistent, safer, and more context-efficient during tasks. One compact tool contract now supplies the model-facing names, aliases, examples, read-only properties, and result labels. `parallel` accepts only bounded `list_dir`, `read_file`, and `grep` calls and rejects an unsafe batch before any item runs. Large files are returned in complete-line pages with an explicit next offset and a 16,000-character budget for file content; small files keep their exact previous output. A single `edit` call can apply up to eight replacements to one file, but writes only after every replacement validates in memory. The release passed 363 tests, all 16 real Edge UI E2E checks, and live edit tasks on DeepSeek, Qwen, and MiMo in four turns each.

Version `0.1.13` keeps the interface unchanged while tightening the runtime underneath it. Git and snapshot change handling now live together in `changes.py`; local runtime files share one state root; and every UI, CLI, and smoke task owns an explicit provider context that is cleared after success, cancellation, connection failure, or CDP close failure. Qwen now commits its controlled composer state with one real trailing key and verifies the exact submitted text before sending. The release passed 344 tests, all 16 real Edge UI E2E checks, and live edit tasks on DeepSeek, Qwen, and MiMo.

Version `0.1.12` keeps the UI and backend aligned across a browser refresh or brief SSE interruption. The backend creates one atomic run ID, exposes one bounded in-memory run snapshot, and lets the UI quietly recover Stop state, pending Shell approval or control teaching, and the final receipt without duplicating `task_done`. One state reconciliation runs at a time; newer SSE events wait briefly and replay after the snapshot, so a slow stale response cannot restore an already completed task to Running. Clearing a chat also revokes its saved terminal receipt. A reconnect stays invisible unless it lasts five seconds, when the existing status line briefly shows `Reconnecting…`. Shell approval results are applied from both the HTTP response and SSE with the same deduplication key; the latest result also remains in the bounded snapshot, and resolving it removes the old approval card. Recovery preserves execution order, so a Shell result appears before the completion of the task it resumed. DeepSeek, Qwen, and MiMo share one one-shot submission boundary: the send method is selected before interaction, the remote action runs once, and an uncertain attempt continues watching the original answer for the full response window without resubmitting. No automatic resend or new UI concept was added. The release passed 331 tests, all 16 real Edge UI E2E checks, and live one-submission probes on all three providers.

Version `0.1.11` makes the existing Stop action responsive during provider polling, recovery, review, and controlled test commands. One shared task-local cancellation signal interrupts those waits; a stopped provider session is discarded so the next task starts in a fresh conversation. An individual synchronous browser navigation, click, or fill still finishes under its own Playwright timeout. Codey does not click a website's stop-generation control and adds no UI. Long `run` and approved Shell output now keeps both its beginning and end within the same bounded context budget instead of losing the final error summary. The release passed 310 tests, all 10 real Edge UI E2E checks, and live cancellation probes on DeepSeek, Qwen, and MiMo.

Version `0.1.10` adds ProfileDoctor as a quiet second recovery step. When bounded local discovery cannot choose safely, one already-open healthy model receives at most eight strictly sanitized structural candidates and may return only one candidate ID. It cannot provide selectors, code, text, or coordinates; it is called once without recursive assistance, and its choice is remembered only after the normal input, submission, or answer validation succeeds. If it declines or fails, human click teaching remains the final fallback. No UI was added. The release passed 294 tests, the complete Edge UI E2E flow, and forced ProfileDoctor send recovery on DeepSeek, Qwen, and MiMo.

Version `0.1.9` adds bounded recovery for small provider-page changes without adding UI. DeepSeek, Qwen, and MiMo now share versioned selector profiles and a conservative fallback that can rediscover the message box, send button, and newest answer. Codey requires a unique actionable match, verifies the resulting page state before remembering it, and forgets learned controls after repeated failures. If automatic recovery is not confident, the existing one-click human teaching remains the final fallback. The release passed 277 tests, the complete Edge UI E2E flow, normal live tasks on all three providers, and forced core-selector recovery on all three sites.

Version `0.1.8` adds a hidden local continuity layer. Each project keeps only a small set of commands that really succeeded, each recent chat keeps one bounded factual snapshot, and the current non-Git recovery baseline is written atomically before project files change. These records add no UI controls or notices and do not store cookies, page DOM, or full chat transcripts. The release passed 260 tests, the real Edge UI flow, and the DeepSeek / Qwen / MiMo edit matrix.

Version `0.1.7` keeps the product experience unchanged while making the runtime easier to maintain. Local tools now return structured outcomes, the agent emits structured events, task orchestration is separate from HTTP transport, and the UI no longer parses human-readable logs. The release passed 224 automated tests plus real Edge, provider, review, handoff, and self-bootstrap flows.

Version `0.1.6` adds a hidden context safety net. Near one shared context budget, Codey asks the current model for a compact factual summary, opens a fresh model chat, and continues without adding controls or notices to the UI. If summarizing fails, Codey falls back to local task facts; if the fresh chat or its first message fails, the old budget and summary remain available for retry.

Version `0.1.4` adds compact task receipts in the chat stream, so beginners can see what happened, whether checks passed, and whether restore is available without opening a new panel. Version `0.1.3` keeps the model browser durable across Codey UI restarts: Codey first reuses an existing Edge CDP browser and model tab, and only opens a new model browser when no usable CDP browser exists.

If a provider page changes, Codey first attempts the bounded recovery above. When it still cannot identify a control safely, it pauses and asks you to click that control once. It stores only the latest verified control record, never the page DOM or your conversation, so the main workflow stays quiet.

---

## Two-Model Assistance

One AI model can write code, but it can also miss small mistakes. Two models make the loop steadier: one model focuses on building, and another model looks over the changed code like a second pair of eyes.

You do not need to learn a new mode. If you open two supported AI pages in Edge, Codey can automatically use them together:

- The model you select in Codey is the writer.
- Another open supported model becomes the reviewer.
- The writer reads files, edits code, and runs tests.
- The reviewer does not touch your files. It only reads the diff and points out concrete problems.
- If the reviewer approves, Codey finishes.
- If the reviewer finds a real issue, Codey asks the writer to repair it once more.

If only one model page is open, Codey simply works in single-model mode. If the second model is closed, logged out, or fails to answer, Codey quietly falls back to the single-model result.

In plain words: open one model for simple work; open two model pages when you want a little extra confidence. No group chat, no extra switch, no new concepts on the main screen.

---

## Quick Start

### 1. Install dependencies

```powershell
pip install -r requirements.txt
```

### 2. Start Codey

```powershell
python -m codey
```

Codey opens a local UI at `http://127.0.0.1:<port>/`.

### 3. Log in once

When Codey opens the selected AI website in a dedicated Edge profile, log in manually once.

The browser profile is separate from your normal Edge profile:

```text
C:\Users\<you>\.codey\edge-profile
```

You can leave that model Edge window open. Restarting the Codey UI does not close it; the next run will quietly reconnect to the existing CDP browser and tab when possible.

### 4. Pick a project and ask

Example:

```text
Create a small Python snake game in one file. Make it runnable with python snake.py.
```

Codey will ask the web AI for structured tool calls, apply edits locally, and show what changed.

When a task finishes, Codey summarizes the local facts in one quiet line:

```text
DONE · 2 files changed · checks passed · restore available        View diff
```

`View diff` opens the right-side changes drawer for the detailed red/green diff.

---

## Safety Model

Codey is not an unrestricted shell.

- File operations are limited to the selected project folder.
- Normal edits are shown as diffs.
- Snapshot restore works even without Git.
- Git integration is optional.
- Shell commands require approval.
- The UI keeps failure recovery simple: `ERROR · Could not send the message  Retry`.

You should still review diffs before trusting generated code.

---

## Git Is Optional

Codey is designed to work before a beginner understands Git.

| Environment | Behavior |
|---|---|
| No Git installed | create/edit files, show red/green diffs, save local snapshots, restore changes |
| Git installed, not a repository | same local diff flow, with a path to initialize Git later |
| Git repository | full Git diff, commit workflow, stronger history and rollback |

Git is an upgrade path, not an entry requirement.

---

## Self-Bootstrap Proof

Codey has been tested repairing broken copies of itself using DeepSeek, MiMo, and Qwen.

Each model:

1. ran failing tests,
2. read Codey source files,
3. edited the broken code,
4. reran tests,
5. reached a green state.

See [BOOTSTRAP_PROOF.md](BOOTSTRAP_PROOF.md).

The current release also includes [TEST_REPORT.md](TEST_REPORT.md), which records the latest single-model, two-model, and self-bootstrap smoke results.

This does not prove Codey will never break. It proves the core repair loop exists: when Codey breaks in a testable way, it can use connected web AI, local tools, diff, restore, and tests to help repair itself.

## End-to-end tests

The real Edge UI flow can be replayed with a deterministic test provider. The
test covers project selection, provider switching, SSE, file edits, test
execution, review, task receipts, diff, and snapshot restore:

```powershell
python -B tools/ui_e2e.py --artifacts .e2e-artifacts --json
```

With all three model pages logged in through Edge CDP, run the real-provider
matrix below. Every result is independently checked with a functional
assertion and unittest after the agent finishes:

```powershell
python -B tools/live_smoke.py --provider all --case edit --port 9222 --max-turns 10 --json
```

---

## Example Tasks

Generate a small program:

```text
Write a complete classic Snake game in pygame as a single file snake.py.
The file must run with: python snake.py
```

Fix a bug:

```text
There is a file buggy.py with a subtle bug. Read it, fix the bug,
write the corrected version back to buggy.py, then run the test.
```

---

## CLI

You can also run Codey without the web control panel:

```powershell
# Single chat message
python -m codey chat "Explain Python's GIL in one sentence"

# Use Qwen
python -m codey chat --provider qwen "Explain Python's GIL in one sentence"

# Run the agent directly
python -m codey agent --provider qwen --project E:\my-project --max-turns 10 "Fix the failing tests"
```

---

## Architecture

```text
UI / CLI
   |
Server / Orchestrator
   |
Agent Runtime -- JsonToolCodec
   |
ChatProvider -- DeepSeekWebProvider
             -- QwenWebProvider
             -- MimoWebProvider
   |
Browser Session + provider DOM driver
```

`agent.py` only knows about `ChatProvider`, `ProtocolCodec`, and tool calls. Browser automation and website selectors live in provider-specific adapters.

---

## Project Structure

```text
codey/
  agent.py                  provider-independent agent runtime
  cancellation.py           shared task-local cancellation and process cleanup
  events.py                 structured run events and log rendering
  text_budget.py            bounded head-and-tail output clipping
  tool_runtime.py           local tools and structured outcomes
  task_runner.py            task, conversation, review, and receipt orchestration
  browser.py                Edge/CDP connection helpers
  browser_worker.py         Playwright thread scheduler
  changes.py                Git and snapshot diff / restore support
  local_store.py            shared local data root and atomic JSON writes
  project_facts.py          facts verified by successful local runs
  conversation_store.py     bounded factual conversation persistence
  provider_profiles.json    versioned selectors for supported model pages
  provider_profiles.py      validated profile loader
  provider_discovery.py     bounded DOM candidate discovery and scoring
  provider_controls.py      verified recovery, learning, and human teaching
  provider_submission.py    shared one-shot remote submission boundary
  profile_doctor.py         one-shot sanitized candidate selection
  deepseek.py               DeepSeek page driver
  mimo.py                   MiMo page driver
  qwen.py                   Qwen page driver
  provider_diagnostics.py   compact provider failure records
  receipt.py                task completion receipt builder
  protocols/
    json_codec.py           JSON-only tool protocol
  providers/
    registry.py             provider registry and sibling-tab borrowing
    *_web.py                provider adapters
  server.py                 local HTTP + SSE transport and runtime state
  web/
    index.html              single-file control panel
```

---

## Limitations

- Web AI pages can change and break automation.
- Model quality varies.
- Web models may produce verbose or imperfect code.
- Codey is a local developer tool, not a security sandbox.
- You still need to review changes before keeping them.

---

## Philosophy

Access matters.

If AI programming only works well for people who can pay for expensive API usage, many beginners are locked out. Codey tries a simpler path: use the web AI access people already have, connect it carefully to local files, and make the edit/test/diff/restore loop understandable.

This is not about replacing professional tools. It is about making the first step into programming and creation cheaper, simpler, and more local.
