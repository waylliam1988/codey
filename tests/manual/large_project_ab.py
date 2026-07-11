"""Manual, read-only A/B benchmark against real medium/large projects.

This is not collected by pytest. It disables every mutating Agent runtime
entry point and writes its JSON results outside the repositories by default.
"""

# ruff: noqa: E402 - direct script execution must add the repository root first.

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codey.agent as agent_module
from codey import provider_controls
from codey.agent import run
from codey.project_map import render_project_map
from codey.protocols.json_codec import JsonToolCodec, SYSTEM_PROMPT, TOOL_SPEC_BY_NAME
from codey.providers.registry import connect_provider
from codey.tool_runtime import ToolOutcome

DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-large-project-ab.json"
NAVIGATION_TOOLS = ("find_references",)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass


@dataclass(frozen=True)
class Case:
    name: str
    project: Path
    task: str
    expected: tuple[str, ...]


class CountingProvider:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.name = provider.name
        self.sends = 0
        self.sent_chars = 0
        self.reply_chars = 0
        self.seconds = 0.0

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def send(self, text: str, timeout: float | None = None) -> str:
        self.sends += 1
        self.sent_chars += len(text or "")
        started = time.monotonic()
        reply = self.provider.send(text, timeout=timeout)
        self.seconds += time.monotonic() - started
        self.reply_chars += len(reply or "")
        return reply


def baseline_prompt() -> str:
    """Render the pre-navigation contract without maintaining a second prompt."""
    prompt = SYSTEM_PROMPT
    for name in NAVIGATION_TOOLS:
        spec = TOOL_SPEC_BY_NAME[name]
        examples = "\n".join(f"  {example}" for example in spec.examples)
        prompt = prompt.replace(f"{examples}\n    {spec.description}\n\n", "")
    prompt = prompt.replace(
        "    old_string. Use read_file with line offsets before editing.\n",
        "",
    )
    prompt = prompt.replace(
        "  - find_references output is lexical reference hints only, not semantic\n"
        "    resolution or a complete call graph. Use read_file before editing.\n",
        "",
    )
    return prompt


class BaselineCodec(JsonToolCodec):
    def system_prompt(self) -> str:
        return baseline_prompt()


def cases(stockalarm: Path) -> dict[str, Case]:
    values = (
        Case(
            "codey_task_flow",
            ROOT,
            "Read-only architecture question. Explain how an HTTP project-task "
            "request moves from codey/server.py through TaskRunner, the agent "
            "loop, Review, and the final receipt. Cite exact relative files and "
            "function/class names. Do not modify files.",
            ("server.py", "task_runner.py", "agent.py", "review.py", "TaskRunner"),
        ),
        Case(
            "stockalarm_training_flow",
            stockalarm,
            "只读架构分析：解释 master_trainer.py 的 main(start_from) 如何选择、跳过并启动训练阶段，"
            "以及它怎样连接 feature_engineering.py。引用准确的相对文件和函数名，不要修改文件。",
            ("master_trainer.py", "main", "feature_engineering.py"),
        ),
        Case(
            "stockalarm_backtest_masks",
            stockalarm,
            "只读定位并解释 stockalarm.py 中回测 entry/exit tradable mask 的构建函数、"
            "它们的主要调用位置和各自职责。引用准确函数名和相对文件，不要修改文件。",
            (
                "stockalarm.py",
                "_build_stockalarm_backtest_entry_tradable_mask",
                "_build_stockalarm_backtest_exit_tradable_mask",
            ),
        ),
    )
    return {case.name: case for case in values}


def _read_only_error(*_args, **_kwargs) -> ToolOutcome:
    return ToolOutcome.error("A/B benchmark is read-only; edit and run are disabled.")


def run_arm(
    case: Case,
    arm: str,
    provider_id: str,
    port: int,
    max_turns: int,
) -> dict[str, object]:
    original = (agent_module.write_file, agent_module.edit_file, agent_module.run_command)
    agent_module.write_file = _read_only_error
    agent_module.edit_file = _read_only_error
    agent_module.run_command = _read_only_error
    provider_controls.begin_task_context(f"large-ab:{case.name}:{arm}")
    raw = None
    events = []
    started = time.monotonic()
    try:
        raw = connect_provider(provider_id, port=port)
        provider = CountingProvider(raw)
        result = run(
            provider,
            case.project,
            case.task,
            codec=BaselineCodec() if arm == "baseline" else JsonToolCodec(),
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            project_map="" if arm == "baseline" else render_project_map(case.project),
        )
        tool_events = [
            event for event in events
            if event.kind == "tool" and event.call is not None and event.outcome is not None
        ]
        counts = Counter(event.call.name for event in tool_events)
        trace = [
            {
                "turn": event.turn,
                "tool": event.call.name,
                "args": event.call.args,
                "ok": event.outcome.ok,
                "truncated": event.outcome.truncated,
                "output_chars": len(event.outcome.output),
            }
            for event in tool_events
        ]
        statuses = [event.message for event in events if event.kind == "status"]
        reply_tails = [
            event.reply[-300:]
            for event in events
            if event.kind == "turn" and event.reply
        ]
        summary_lower = result.summary.lower()
        return {
            "case": case.name,
            "arm": arm,
            "provider": provider_id,
            "ok": result.stop_reason == "done" and not result.changed,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "provider_seconds": round(provider.seconds, 3),
            "sends": provider.sends,
            "sent_chars": provider.sent_chars,
            "reply_chars": provider.reply_chars,
            "tool_counts": dict(sorted(counts.items())),
            "trace": trace,
            "statuses": statuses,
            "reply_tails": reply_tails,
            "expected_hits": [
                item for item in case.expected if item.lower() in summary_lower
            ],
            "expected_total": len(case.expected),
            "summary": result.summary,
        }
    finally:
        agent_module.write_file, agent_module.edit_file, agent_module.run_command = original
        try:
            if raw is not None:
                raw.close()
        finally:
            provider_controls.end_task_context()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--arm", choices=("baseline", "current"), required=True)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--stockalarm", type=Path, default=Path("E:/stockalarm/stockalarm"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    available = cases(args.stockalarm.resolve())
    if args.case not in available:
        parser.error(f"unknown case {args.case!r}; choose from {', '.join(available)}")

    row = run_arm(available[args.case], args.arm, args.provider, args.port, args.max_turns)
    previous = []
    if args.output.exists():
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps([*previous, row], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(row, ensure_ascii=False, indent=2), flush=True)
    return 0 if row["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
