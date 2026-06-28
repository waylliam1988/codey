# Codey

**Use web AI models as a local coding assistant.**

[中文说明](README.zh-CN.md)

Codey connects to AI chat websites you already use, such as DeepSeek, Qwen, and Xiaomi MiMo, and gives them a controlled local tool loop: read files, edit files, run tests, show diffs, and restore changes.

It is a local-first, low-cost AI coding workspace built for multiple web models.

No API key required. No model subscription wiring. Log in to the web AI in Edge, pick a local project folder, and start building.

Version: `0.1.2`

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
- Restore snapshot changes without requiring Git
- Use Git when available, but not require it
- Retry with another model when one model fails
- Use two open web models together: one writes, the other reviews
- Record compact provider failure diagnostics for debugging web-page breakage

---

## Supported Models

| Model | Status |
|---|---|
| DeepSeek Web | Tested |
| Xiaomi MiMo Chat | Tested |
| Qwen Studio | Tested |

Codey uses browser automation, so websites may break after UI changes. The current design keeps provider-specific code isolated so those adapters can be repaired without changing the agent core. Recent live tests also hardened MiMo submission so the driver clicks the real send button instead of nearby upload controls, and clarified the JSON tool protocol for Qwen.

Version `0.1.2` makes the model picker show live availability with green/gray dots, keeps UI sends able to auto-open a missing model page, and changes the composer to `Enter` to send / `Shift+Enter` for a newline.

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

### 4. Pick a project and ask

Example:

```text
Create a small Python snake game in one file. Make it runnable with python snake.py.
```

Codey will ask the web AI for structured tool calls, apply edits locally, and show what changed.

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
  browser.py                Edge/CDP connection helpers
  browser_worker.py         Playwright thread scheduler
  changes.py                snapshot diff and restore support
  deepseek.py               DeepSeek page driver
  mimo.py                   MiMo page driver
  qwen.py                   Qwen page driver
  provider_diagnostics.py   compact provider failure records
  protocols/
    json_codec.py           JSON-only tool protocol
  providers/
    registry.py             provider registry
    *_web.py                provider adapters
  server.py                 local HTTP + SSE backend
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
