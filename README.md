# Codey

**Local-first AI coding and research for people who already have web AI access.**

[![Version](https://img.shields.io/badge/version-0.5.7-blue)](CHANGELOG.md)
[![License: GPL v2](https://img.shields.io/badge/license-GPL--2.0--only-blue)](LICENSE)
[![Local first](https://img.shields.io/badge/local--first-AI%20workspace-2ea44f)](#safety-model)

[中文说明](README.zh-CN.md)

Version: `0.5.7`

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
- A research loop that cites opened sources instead of treating search results as evidence.
- A bounded local memory layer that can be inspected, exported, deleted, reset, or disabled.

Codey is not a cloud coding agent, not a plugin marketplace, and not a way to
give websites hidden access to your whole machine.

## Quick Start

Install dependencies:

```powershell
pip install -r requirements.txt
```

Start Codey:

```powershell
python -m codey
```

Codey opens a local UI at `http://127.0.0.1:<port>/`. When a provider browser
opens, log in once with the web AI account you already use. Then choose a
project folder and ask for a change, or stay in `New Chat` for ordinary
conversation with no project access.

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

## Documentation

- [Detailed capabilities](docs/codey_capabilities.md)
- [Roadmap](ROADMAP.zh-CN.md)
- [Changelog](CHANGELOG.md)
- [Ghost future direction](docs/ghost_future_direction.zh-CN.md)

## Safety Model

Models can work only inside the project folder you choose. Local actions pass
through Codey's tool contract, permission profile, action policy, completion
proof, and research evidence checks. Codey records bounded local facts for
audit and recovery, but avoids saving raw prompts, full transcripts, source
files, webpage bodies, cookies, or secrets.

Browser providers can change their websites. Codey keeps provider adapters
isolated so a broken web page integration can be fixed without changing the
agent core.

## Development

```powershell
pip install -r requirements.txt
python -m pytest
```

## License

GPL-2.0-only
