# Codey

**Use web AI models as a local coding assistant.**

[![Version](https://img.shields.io/badge/version-0.1.46-blue)](CHANGELOG.md)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![Local first](https://img.shields.io/badge/local--first-web%20AI%20coding-2ea44f)](#safety-model)

[中文说明](README.zh-CN.md)

Codey connects to AI chat websites you already use, such as DeepSeek, Qwen, Xiaomi MiMo, and GLM, then gives them a controlled local coding loop: read files, edit files, run tests, show diffs, review changes, and restore safely.

It is a local-first, low-cost AI coding workspace for people who want useful coding help without wiring paid model APIs into every project.

No API key required. No model subscription wiring. Log in to the web AI in Edge or Chrome, pick a local project folder, and start building.

Version: `0.1.46`

[Version history](CHANGELOG.md)

---

## At a Glance

- **Use web AI accounts you already have**: DeepSeek, Qwen, Xiaomi MiMo, and GLM are supported.
- **Keep code local**: models access only the project folder you choose.
- **Controlled tool loop**: read, edit, test, diff, review, and restore.
- **Multi-model when useful**: one model can write while another reviews the final diff.
- **Beginner-friendly by design**: Git helps, but it is not required.

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

## Philosophy

Access matters.

If AI programming only works well for people who can pay for expensive API usage, many beginners are locked out. Codey tries a simpler path: use the web AI access people already have, connect it carefully to local files, and make the edit/test/diff/restore loop understandable.

This is not about replacing professional tools. It is about making the first step into programming and creation cheaper, simpler, and more local.

---

## What It Can Do

- Use New Chat for normal conversation without granting access to a project
- Discuss, inspect, and edit inside one project conversation; files change only when requested
- Let the model read and modify files in a selected project folder
- Run allowed tests, builds, linters, and type checks, then feed results back
  to the model
- Show red/green diffs
- Show a compact task receipt after each run, such as `DONE · 2 files changed · checks passed · restore available`
- Restore snapshot changes without requiring Git
- Use Git when available, but not require it
- Retry with another model when one model fails
- Use already-open models as hidden advisors for chat, empty-project planning, and read-only project audits
- Use two open web models together: one writes, the other reviews
- Use hidden task briefs so Writer and Reviewer share the same bounded intent
- Give Writer, hidden advisors, and Reviewer a bounded local Project Map before
  they inspect files
- Let the model request bounded lexical reference hints before changing a symbol
- Flag a newly introduced Python syntax error immediately after a successful
  replacement edit, without rolling back or pretending a check passed
- Continue long conversations quietly with an automatic factual summary and fresh model chat
- Reuse project commands that have already succeeded locally
- Remember recent successful changes only from verified local checks
- Resume the same chat after a Codey restart or model switch from one bounded
  factual handoff plus recent visible conversation context
- Keep non-Git diff and restore available across Codey restarts
- Recover changed composer controls through bounded local discovery or a
  healthy sibling model, then verify, promote, and roll back the local bundle
- Recover one bounded web-chat state rule from boolean-only evidence when a
  verified control bundle is not enough; Qwen uses a real stop-state
  transition and MiMo uses an explicit typing transition
- Hand an interrupted project Writer to a healthy sibling model from bounded
  local checkpoint facts, with at most two switches and no repeated uncertain
  submission to the failed model
- Keep a small local provider-health circuit so rate limits, login pages, and
  repeated structural failures do not keep receiving hidden work
- Repair broken provider adapter code in a sandboxed background worker when
  bounded control and Flow recovery are not enough, then verify the candidate
  with an isolated worker canary before enabling it
- Stop a running provider wait, review, recovery, or test command promptly
- Give longer timeouts to full verification suites while keeping quick commands
  bounded
- Preserve both the beginning and end of long command output
- Tell the model the exact next `read_file` JSON call when a large file read
  returns only one page
- Suggest narrowing a literal grep when search results hit the match limit
- Fold obvious dependency stack frames from Python and Node command output so
  user-code failures survive the existing output budget
- Render assistant replies with quiet Markdown basics and copy buttons on code
  blocks, without syntax highlighting or new colors
- Recover the active run, approval, or teaching prompt after a UI reconnect
- Prevent uncertain provider submissions from being sent twice
- Record compact provider failure diagnostics for debugging web-page breakage
- Launch Edge by default, fall back to Chrome when needed, and keep the local
  HTTP UI available if the native WebView window cannot open

---

## Supported Models

| Model | Status |
|---|---|
| DeepSeek Web | Tested |
| Xiaomi MiMo Chat | Tested |
| Qwen Studio | Tested |
| GLM | Tested |

Codey uses browser automation, so websites may break after UI changes. The current design keeps provider-specific code isolated so those adapters can be repaired without changing the agent core.

If a provider page changes, Codey first attempts the bounded recovery above. When it still cannot identify a control safely, it pauses and asks you to click that control once. It stores only the latest verified control record, never the page DOM or your conversation, so the main workflow stays quiet.

Recovered controls are committed only after the original message is submitted
once and a new answer is read. The first successful bundle remains provisional;
the next natural success promotes it, while explicit repeated control failures
restore the previous bundle. Normal healthy sends do not call sibling models.

The same recovery bundle can contain one bounded Flow Recipe made only from
fixed boolean facts such as response stability and a verified stop-state
transition. Recipes cannot contain selectors, JavaScript, URLs, arbitrary
clicks, page text, or project data. Completion is never inferred from stable
text alone. Qwen uses a real stop-state transition, MiMo uses an explicit
typing transition, and GLM safely keeps its built-in behavior until it exposes
equally reliable terminal evidence.

If a project Writer fails with an explicit provider-page error, Codey can move
the unfinished task to a healthy sibling model. The new Writer starts a clean
conversation and receives only the original task plus bounded local checkpoint
facts such as changed-file hashes and still-valid checks. Provider health stores
only counters, timestamps, and failure kinds; it never stores page text, URLs,
prompts, source code, cookies, or chat content. Cooled-down providers receive one
data-free canary only when they are about to handle real work again.

If a provider adapter itself breaks after a larger website change, Codey can
queue a background adapter self-repair. The repair path runs in a separate
Python process, asks a healthy helper model to modify only the broken provider's
adapter files in a temporary sandbox, validates the candidate with policy
checks, static checks, provider unit tests, and a neutral marker canary, then
loads the candidate only through a child Provider worker. The worker uses a
fresh background tab in the same logged-in Codey browser profile, so it does
not need to copy cookies or block your current task. Candidates start as
provisional, become active only after natural successes, and roll back after
repeated structural failures. Core files such as `agent.py`, `task_runner.py`,
`tool_runtime.py`, `server.py`, and the recovery/safety modules are not
self-modified by this v1.

---

## Hidden MoA Advice

MoA (Mixture of Agents) is Codey's invisible advisory layer. It adds no button, mode, or dashboard. In New Chat, the selected owner model drafts first, up to two other open models privately critique or supplement it, and the owner produces the one answer you see. Empty or placeholder-only projects use the same owner-first pattern for planning. In an existing project, advisors instead perform bounded read-only audits before the Writer acts.

Advisors cannot edit files, run commands, request Shell approval, access anything outside the selected project, or read sensitive and excluded paths. Their reports are suggestions: the Writer must verify them against real files. Advisor failures quietly fall back to the owner model.

This hidden MoA layer is separate from the post-change second-model Diff Review described below. MoA helps before or during the owner's reasoning; Diff Review checks the actual final changes.

---

## Two-Model Assistance

One AI model can write code, but it can also miss small mistakes. Two models make the loop steadier: one model focuses on building, and another model looks over the changed code like a second pair of eyes.

You do not need to learn a new mode. If you open two supported AI pages in the Codey browser, Codey can automatically use them together:

- The model you select in Codey is the writer.
- Another open supported model becomes the reviewer.
- The writer reads files, edits code, and runs tests.
- The reviewer does not touch your files. It only reads the diff and points out concrete problems.
- If the reviewer approves, Codey finishes.
- If the reviewer finds a real issue, Codey asks the writer to repair it once more.

The reviewer runs only after the selected model actually changes a file. Project
questions and read-only analysis return the selected model's answer directly,
without turning a conversation into a code review.

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

When Codey opens the selected AI website in a dedicated browser profile, log in manually once.

The browser profile is separate from your normal daily browser profile:

```text
C:\Users\<you>\.codey\edge-profile
C:\Users\<you>\.codey\chrome-profile
```

You can leave that model browser window open. Restarting the Codey UI does not close it; the next run will quietly reconnect to the existing CDP browser and tab when possible.

### 4. Pick a project and ask

Example:

```text
Create a small Python snake game in one file. Make it runnable with python snake.py.
```

Codey will ask the web AI for structured tool calls, apply edits locally, and show what changed.

You can also ask questions in that same project chat before requesting code. If
you only want a general conversation with no project access, use **New Chat**.

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

The current release also includes [TEST_REPORT.md](TEST_REPORT.md), which records the latest single-model, two-model, MoA, and self-bootstrap smoke results.

This does not prove Codey will never break. It proves the core repair loop exists: when Codey breaks in a testable way, it can use connected web AI, local tools, diff, restore, and tests to help repair itself.

## End-to-end tests

The real Edge UI flow can be replayed with a deterministic test provider. The
test covers project selection, provider switching, SSE, file edits, test
execution, review, task receipts, diff, and snapshot restore:

```powershell
python -B tools/ui_e2e.py --artifacts .e2e-artifacts --json
```

With all four model pages logged in through Edge CDP, run the real-provider
matrix below. Every result is independently checked with a functional
assertion and unittest after the agent finishes:

```powershell
python -B tools/live_smoke.py --provider all --case edit --port 9222 --max-turns 10 --json
```

The explicit MoA snake flow is kept under `tests/` because it is a real smoke
test, not a general tool. It writes its checkpoints and timing log inside the
target project under `.codey/smoke/moa-snake-flow`:

```powershell
python -B tests\moa_snake_flow.py --project E:\snake --reset --json
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
             -- GlmWebProvider
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
  bounded_scan.py           shared bounded local file traversal
  scan_report.py            compact scan omission facts and coverage rendering
  tool_runtime.py           local tools and structured outcomes
  execution_evidence.py     bounded in-memory execution fact ledger
  references.py             bounded lexical reference hints
  project_map.py            deterministic bounded project orientation
  project_task_context.py   project facts, map, checkpoint, and verification context
  verification_map.py       bounded review-time verification candidates
  change_brief.py           hidden task intent brief
  review_coordinator.py     bounded diff review lifecycle
  task_runner.py            task, conversation, review, and receipt orchestration
  browser.py                Chromium CDP connection helpers
  browser_worker.py         Playwright thread scheduler
  changes.py                Git and snapshot diff / restore support
  local_store.py            shared local data root and atomic JSON writes
  project_facts.py          facts verified by successful local runs
  work_checkpoint.py        durable facts for unfinished execution
  conversation_store.py     bounded factual conversation persistence
  provider_profiles.json    versioned selectors for supported model pages
  provider_profiles.py      validated profile loader
  provider_discovery.py     bounded DOM candidate discovery and scoring
  provider_controls.py      verified recovery, learning, and human teaching
  provider_flow.py          bounded boolean web-chat state rules
  provider_revival.py       atomic control bundles, promotion, and rollback
  provider_submission.py    shared one-shot remote submission boundary
  provider_supervisor.py    passive health circuit, Writer selection, and canary
  adapter_overrides.py      local adapter candidates, promotion, and rollback
  adapter_repair.py         sandboxed provider adapter repair runner
  repair_policy.py          strict adapter repair file and code policy
  repair_sandbox.py         temporary source copy for adapter repair
  repair_journal.py         bounded local adapter repair journal
  self_repair.py            deduplicated background repair queue
  self_repair_worker.py     repair subprocess entry point and helper selection
  provider_worker.py        parent-side isolated adapter worker wrapper
  provider_worker_child.py  child process adapter runner
  profile_doctor.py         one-shot sanitized candidate selection
  deepseek.py               DeepSeek page driver
  mimo.py                   MiMo page driver
  qwen.py                   Qwen page driver
  glm.py                    GLM page driver
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

## License

Codey is released under the GNU General Public License version 2 only
(`GPL-2.0-only`). See [LICENSE](LICENSE).
