# Codey

**Local-first AI coding and research for people who already have web AI access.**

[![Version](https://img.shields.io/badge/version-0.5.11-blue)](CHANGELOG.md)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![Local first](https://img.shields.io/badge/local--first-AI%20workspace-2ea44f)](#safety-model)

[中文说明](README.zh-CN.md)

Version: `0.5.11`

Codey connects browser AI accounts you already use, such as DeepSeek, MiMo,
StepFun, Qwen, and GLM, or a local OpenAI-compatible model, to a controlled
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

Codey opens a local UI at `http://127.0.0.1:<port>/`. When a provider browser
opens, log in once with the web AI account you already use. Then choose a
project folder and ask for a change, or stay in `New Chat` for ordinary
conversation with no project access.

The local UI authorizes itself automatically when Codey opens it. If the native
window cannot open, use the complete launch link printed by Codey. That link is
single-use and expires after five minutes; restart Codey to obtain a new link
when it has expired or been consumed. This does not require another AI account.

To use a local model, choose `Local` and provide an OpenAI-compatible base URL,
model id, and optional API key.

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
```

CLI, browser events and headless JSONL share run identity and tool status.
Recovery retains the original requirements and can deliver settled results;
unsettled dangerous writes are not blindly retried.

## Documentation

Read-only review uses the selected Reviewer, validates findings against the
actual input scope and checks whether the workspace changed. Partial or
unavailable results are explicit; review approval does not replace tests.
An explicit `review_source_run_id` can reuse a finished result only in the same
session/project with matching input, known local target/settings and a current
snapshot. No source means a fresh review. Run Details shows bounded status;
`GET /api/run_review` returns verified stored findings through the authenticated
run API. See [current review ownership](docs/project_structure.md).

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
