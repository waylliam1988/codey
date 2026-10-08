# Codey

**Local-first AI coding and research for people who already have web AI access.**

[![Version](https://img.shields.io/badge/version-0.5.11-blue)](CHANGELOG.md)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![Local first](https://img.shields.io/badge/local--first-AI%20workspace-2ea44f)](#safety-model)

[中文说明](README.zh-CN.md)

Version: `0.5.11`

Codey connects browser AI accounts you already use, such as DeepSeek, MiMo,
StepFun, Qwen, and GLM, a local OpenAI-compatible model, or supported free
OpenCode Zen models, to a controlled
workspace on your own computer.

Its purpose is access equity. AI-assisted programming should not require paid
API credits before someone can learn, experiment, or build a useful local
project. Codey keeps the work local, makes changes visible, and gives beginners
a path from plain language to files, tests, diffs, restore, and evidence-backed
research.

## What It Is

- A desktop/local UI for chat, coding, review, and research.
- A bridge from web AI chat products to local project folders.
- A controlled tool loop for reading, editing, testing, diffing, reviewing, and restoring.
- Research requirements on the shared task loop: cite opened sources rather than search summaries.
- A bounded local memory layer that can be inspected, exported, deleted, reset, or disabled.

Codey is not a cloud coding agent, not a plugin marketplace, and not a way to
give websites hidden access to your whole machine.

## Quick Start

Install dependencies:

```powershell
pip install -e .
```

Start Codey:

```powershell
python -m codey
```

Codey opens a local UI at `http://127.0.0.1:<port>/`. Model websites open only
when you send to them; startup and model selection do not launch them. When a
provider browser opens, log in once with the web AI account you already use. Choose a
project folder and ask for a change, or stay in `New Chat` for ordinary
conversation with no project access.

The local UI authorizes itself automatically when Codey opens it. If the native
window cannot open, use the complete launch link printed by Codey. That link is
single-use and expires after five minutes; restart Codey to obtain a new link
when it has expired or been consumed. This does not require another AI account.

Settings → Models controls which models appear in the composer. Websites start
with the five registered models selected; API sources start disabled and empty.
Each source has the same master toggle and model checkboxes. Off preserves its
subset; zero selected turns it off. Select a model or use Select all to enable it,
then Save changes. Refresh models never automatically selects new arrivals.
All sources off preserves drafts and history, with Send disabled.

Draft text and caret/selection are saved locally with each chat and restored
after restart. Clear messages keeps unsent input; deleting a chat removes it.
Save failures keep your input with an explicit Retry. Scrolling up stops output
following even near the bottom; reach the bottom or use Back to latest to resume.
Select text in an answer and choose Quote in reply to append an editable Markdown
quote to your draft without sending it.

Save changes covers model choices; Save connection covers connection fields.
Each becomes available when its own values change. Refresh models preserves
unsaved choices, search and focus. Changes refresh keeps the current diff readable;
failure labels previous results and blocks Restore until an update succeeds.

To use a local model, open Settings → Local → Connection and provide an
OpenAI-compatible base URL, model ID and optional API key. Save connection, then
Refresh models, choose models and Save changes. Names come from actual model
metadata or your optional display name. Connection → Advanced → `API protocol`
explicitly selects Chat Completions (`/chat/completions`) or Responses
(`/responses`). `Tool calling` separately selects native or text-based requests.

To use free Zen models, expand its source in Settings, Refresh models and choose
the models you want. Only enabled, selected models appear in the composer. The
catalog combines the public directory and live endpoint listing; a network
failure retains the bounded cache. No registration or personal key is
required by this partner connection, but eligibility and tool restrictions are
checked by the service for each request. A free listing does not guarantee access
to every task mode. Muse coding and Space Bunny review were verified; see
[test results and access limits](TEST_REPORT.md).

See [model management and optional connection removal](docs/model-management.md).

## CLI

```powershell
# Single chat message
python -m codey chat "Explain Python's GIL in one sentence"

# Use Qwen
python -m codey chat --provider qwen "Explain Python's GIL in one sentence"

# Run the agent directly
python -m codey agent --provider qwen --project E:\my-project --max-turns 10 "Fix the failing tests"

# Emit JSONL events for scripts or CI wrappers
python -m codey agent --json --provider qwen --project E:\my-project "Fix the failing tests"

# Choose a free API model; protocol follows its current catalog entry
python -m codey agent --provider zen --model muse-spark-1.3-contributor-free --project E:\my-project "Fix the failing tests"
```

`agent` uses the same task services and authorization rules as the desktop.
It defaults to `project`; use `--auto` for automatic routing or `--intent` for
`chat`, `research`, `hybrid`, `review` or `planning_readonly`. A project folder
is optional for chat/Research. `--readonly` selects read-only planning;
`--allow-web` and `--allow-write` supply explicit grants, subject to task denials.
`--session-id` selects a session and `--continue` continues it. Review reuse
requires the same session and `--review-source-run-id` with a prior run's ID.
Noninteractive shell approval remains denied. The single-message `chat` command
is a provider utility; `agent --intent chat` uses the recorded task flow.
API tasks accept `--model`; `--effort` requires it and must match advertised
capabilities. Desktop, CLI and headless admission persist the chosen model,
protocol and generation settings. Later Settings changes do not alter that run;
an unavailable original connection blocks recovery explicitly.

Chat Completions and Responses share cancellation and result-delivery rules.
Removing the optional Zen connection preserves Local API support and stored history.

CLI, browser events and headless JSONL share run identity and tool status.
Recovery retains the original requirements and can deliver settled results;
unsettled dangerous writes are not blindly retried.

## Documentation

Read-only review uses the selected Reviewer, validates findings against the
actual input scope and checks whether the workspace changed. Partial or
unavailable results are explicit; review approval does not replace tests.
An explicit `review_source_run_id` can reuse a finished result only in the same
session/project with matching input, known API target/settings and a current
snapshot. No source means a fresh review. Run Details shows bounded status;
`GET /api/run_review` returns verified stored findings through the authenticated
run API. See [current review ownership](docs/project_structure.md).

Desktop and CLI/headless project runs use the same automatic review phase.
Under the default policy, an available web Reviewer is preferred. Otherwise an
API Writer first selects a different model from its connection when available;
the existing policy decides whether fresh self-review is allowed when no
independent model is available. A standalone read-only API review uses the
selected model itself. Unknown delivery is never retried through another model.
Concrete findings enter at most one Writer repair. Plain non-Git folders are
supported. `--review-policy
require_web` requires a web Reviewer; embedded gates can explicitly pin a
Reviewer connector. Review approval never replaces fresh verification.
See [desktop/CLI parity evidence](docs/desktop-cli-task-parity-2026-10-04.zh-CN.md).

- [Detailed capabilities](docs/codey_capabilities.md)
- [Roadmap](ROADMAP.zh-CN.md)
- [Changelog](CHANGELOG.md)
- [Project structure and ownership](docs/project_structure.md)
- [0.5.11 release review (Chinese)](docs/release_0.5.11.zh-CN.md)
- [Ghost future direction](docs/ghost_future_direction.zh-CN.md)

## Safety Model

Models can work only inside the project folder you choose. Local actions pass
through Codey's tool contract, permission profile, action policy, completion
proof, and research evidence checks. Audit views use bounded summaries and
references. Local managed outputs and recovery receipts may retain read source
text, webpage bodies and tool results. Conversation and enabled Ghost experience
storage retain user messages/answers locally; this differs from a prompt trace,
which records manifests and digests rather than raw prompts.

Browser providers can change their websites. Codey keeps provider adapters
isolated so a broken web page integration can be fixed without changing the
agent core.

Zen's agreed upstream headers and tool-name mapping live only in its connection
package. They are absent from Local requests and assistant prompts. Generation
requests are sent once; interrupted/unknown replies are not automatically replayed.
Completion proof and final result delivery are recorded separately.

Zen's temporary request profile adds unavailable transport declarations without
granting file or command access. Standalone API review keeps the explicitly
selected model even when a web model is open. See the [request profile and live
validation](docs/zen_request_profile_2026-10-08.md).

## Development

```powershell
pip install -e .[dev]
python -m pytest -q -o faulthandler_timeout=120
```

The offline kernel parity gate is `python tools/kernel_parity.py --report parity.json`.
Its pinned legacy oracle, coverage and reviewed differences are described in the
[parity audit](docs/kernel_parity.zh-CN.md).

CI also gates Ruff, full-tree mypy, JavaScript syntax and supported-platform
regressions. For a matching development toolchain, install `requirements-ci.txt`
before `pip install -e . --no-deps`. Release checks and their live-model scope
are defined in the [release gate](docs/release_gate.zh-CN.md).

## License

GPL-2.0-only
