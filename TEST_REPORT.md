# Codey 0.1.3 Test Report

Date: 2026-06-28  
Environment: Windows / Edge CDP reuse path / DeepSeek, MiMo, Qwen tabs open

## Scope

This pass reviewed Codey as a product system, not only as a unit-tested library:

- code elegance and duplication
- local unit tests
- single-model live smoke
- two-model writer/reviewer live smoke
- single-model self-bootstrap on a broken temporary Codey copy
- two-model self-bootstrap on a broken temporary Codey copy

All bootstrap tests used temporary copies. The main repository was not used as the repair target.

## Code Review

The current architecture is still reasonably clean:

- `agent.py` remains provider-independent.
- web page selectors stay isolated in provider drivers.
- two-model review lives in `review.py` and `server.py`, without adding a group-chat UI.
- snapshot diff / restore stays separate in `changes.py`.

I did not extract the repeated `_visible_locator` helpers from MiMo and Qwen. They look similar, but keeping provider DOM code local is currently more robust: if one website changes, the repair stays in one adapter.

Small issues found and fixed:

- Qwen could edit a file and then try to finish without running tests, even when the task explicitly requested tests.
- Qwen could still echo website-side "tool does not exist" noise.
- MiMo could produce invalid JSON when an `old_string` contained unescaped quotes.

Fixes:

- `agent.py` now requires a successful `run` after file writes when the user asked for verification.
- `json_codec.py` now states earlier and more explicitly that tool names are local-runner JSON commands, not website tools.
- `json_codec.py` now reminds models to escape JSON strings correctly, or use full-file `content` when exact replacement strings are hard to escape.
- Added `tools/bootstrap_smoke.py` so self-bootstrap checks are repeatable.

## Local Tests

```text
python -B -m unittest
Ran 176 tests
OK
```

`git diff --check` passed.

## Live Smoke

| Mode | Result | Notes |
|---|---:|---|
| DeepSeek single create | Pass | Created code and tests, ran unittest, finished cleanly |
| MiMo single edit | Pass | Fixed bug, ran unittest, no upload-button misclick |
| Qwen single edit | Pass | After protocol fix, no "tool does not exist" noise; ran unittest before done |
| DeepSeek writer + MiMo reviewer | Pass | Writer fixed bug; reviewer approved |
| MiMo writer + DeepSeek reviewer | Pass | Reverse pair approved |
| Qwen writer + DeepSeek reviewer | Pass | Qwen entered review chain cleanly |

## Bootstrap Smoke

Bug injected into temporary Codey copies:

```text
difflib.unified_diff(after_lines, before_lines, ...)
```

Expected fix:

```text
difflib.unified_diff(before_lines, after_lines, ...)
```

| Mode | Result | Turns | Final full tests |
|---|---:|---:|---:|
| DeepSeek single | Pass | 5 | 160 OK |
| MiMo single | Pass | 5 | 160 OK |
| Qwen single | Pass | 5 | 160 OK |
| DeepSeek writer + MiMo reviewer | Pass | 5 | 160 OK |
| MiMo writer + DeepSeek reviewer | Pass | 5 after prompt fix | 160 OK |
| Qwen writer + DeepSeek reviewer | Pass | 5 | 160 OK |

## Does Two-Model Help?

For this specific injected bug, single-model repair was already strong: all three models fixed it in about five turns. The two-model path did not reduce writer turns because the bug was simple and tests were clear.

The advantage is confidence and failure recovery:

- reviewer sees the actual diff, not just the writer's summary
- reviewer can catch concrete mistakes before the user accepts the result
- if reviewer is unavailable, Codey falls back to single-model mode
- no extra UI switch is exposed to beginners

So the two-model feature is useful, but it should stay quiet and automatic. It is a safety layer, not a new product surface.

## Residual Risks

- Web pages can change DOM structure and break provider drivers.
- DeepSeek sometimes adds prose before JSON; the parser tolerates this.
- Web models can still be verbose or choose larger edits than a human would.
- UI screenshot testing is still manual/lightweight.

## Release Notes

Version `0.1.3` is a durable model-browser release:

- model Edge/CDP browser is treated as long-lived user state
- Codey UI restarts do not intentionally close the model browser
- provider connections first reuse existing CDP browsers and model tabs
- if no usable CDP browser exists, Codey opens a new one automatically
- the last working CDP port is saved for quiet reuse after process restart
- no extra UI notification is shown for this recovery path

Version `0.1.2` was a usability and provider-status release:

- live green/gray model availability dots
- UI sends can still auto-open missing model pages
- connected models turn green after successful attach/open
- composer now uses Enter to send and Shift+Enter for newline
- root `DESIGN.md` is the single UI design source
