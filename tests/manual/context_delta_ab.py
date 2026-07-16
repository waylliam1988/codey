"""Manual read-only A/B for repeated full context versus a delta follow-up.

The first stage establishes project context in one web-model conversation. The
second stage either repeats Codey's complete project intro (``full``) or uses
the existing short same-conversation follow-up (``delta``). Mutating tools are
disabled, and results are written to the system temporary directory by default.
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

from codey import provider_controls
from codey.agent import run
from codey.agent_tools import AgentToolFns
from codey.handoff import ConversationContext
from codey.project_map import render_project_map
from codey.protocols.json_codec import JsonToolCodec
from codey.providers.registry import connect_provider
from codey.tool_runtime import ToolOutcome

DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-context-delta-ab.json"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass


@dataclass(frozen=True)
class Case:
    name: str
    project: Path
    warmup_task: str
    followup_task: str
    expected: tuple[str, ...]


class CountingProvider:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.name = provider.name
        self.prompts: list[str] = []
        self.reply_chars = 0
        self.seconds = 0.0

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def send(self, text: str, timeout: float | None = None) -> str:
        self.prompts.append(text or "")
        started = time.monotonic()
        reply = self.provider.send(text, timeout=timeout)
        self.seconds += time.monotonic() - started
        self.reply_chars += len(reply or "")
        return reply


def cases(stockalarm: Path) -> dict[str, Case]:
    values = (
        Case(
            "codey_execution_evidence",
            ROOT,
            "Read-only orientation: trace a project task from the HTTP request "
            "through TaskRunner and the agent tool loop. Cite exact relative files "
            "and symbols. Do not modify files or run commands.",
            "Continue the same read-only analysis. Explain how a successful edit "
            "invalidates old checks, how later run results are recorded, and how "
            "Review consumes the fresh evidence. Cite exact files and symbols. "
            "Do not modify files or run commands.",
            ("task_runner.py", "execution_evidence.py", "record", "render_for_review"),
        ),
        Case(
            "stockalarm_training_followup",
            stockalarm,
            "只读梳理 master_trainer.py 的 main(start_from) 如何选择和启动训练阶段，"
            "引用准确文件和函数名，不要修改文件或运行命令。",
            "继续同一只读分析：现在只解释它如何连接 feature_engineering.py，"
            "以及失败或跳过阶段时采用什么控制流。引用准确文件和函数名，"
            "不要修改文件或运行命令。",
            ("master_trainer.py", "feature_engineering.py", "main"),
        ),
    )
    return {case.name: case for case in values}


def followup_run_kwargs(arm: str, conversation: ConversationContext) -> dict[str, object]:
    """Return benchmark-only wiring without changing Agent behavior."""
    if arm in {"delta", "contract-delta"}:
        return {"fresh_chat": False, "conversation": conversation}
    if arm == "full":
        # No local conversation object makes Agent render its complete project intro,
        # while fresh_chat=False preserves the already-open remote web conversation.
        return {"fresh_chat": False, "conversation": None}
    raise ValueError(f"unknown arm: {arm}")


def followup_request(arm: str, task: str) -> str:
    if arm == "contract-delta":
        return (
            f"{JsonToolCodec().system_prompt()}\n\n"
            "The previously supplied project root, project instructions, Project Map, "
            "and initial listing are unchanged. Reuse them without requesting the same "
            "information again.\n\n"
            f"Current user request:\n{task}"
        )
    if arm in {"full", "delta"}:
        return task
    raise ValueError(f"unknown arm: {arm}")


def _read_only_error(*_args, **_kwargs) -> ToolOutcome:
    return ToolOutcome.error("context A/B is read-only; edit and run are disabled")


def _tool_events(events) -> list:
    return [
        event
        for event in events
        if event.kind == "tool" and event.call is not None and event.outcome is not None
    ]


def _info_keys(events) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for event in _tool_events(events):
        name = event.call.name
        if name == "read":
            keys.add((name, str(event.call.args.get("path") or ""), ""))
        elif name in {"search", "references"}:
            value_name = "query" if name == "search" else "symbol"
            keys.add(
                (
                    name,
                    str(event.call.args.get("path") or "."),
                    str(event.call.args.get(value_name) or ""),
                )
            )
    return keys


def _stage_metrics(events, prior_info: set[tuple[str, str, str]]) -> dict[str, object]:
    tools = _tool_events(events)
    counts = Counter(event.call.name for event in tools)
    info = []
    repeats = 0
    for event in tools:
        name = event.call.name
        if name == "read":
            key = (name, str(event.call.args.get("path") or ""), "")
        elif name in {"search", "references"}:
            value_name = "query" if name == "search" else "symbol"
            key = (
                name,
                str(event.call.args.get("path") or "."),
                str(event.call.args.get(value_name) or ""),
            )
        else:
            continue
        info.append(key)
        repeats += key in prior_info
    return {
        "tool_counts": dict(sorted(counts.items())),
        "information_calls": len(info),
        "repeated_warmup_information_calls": repeats,
        "trace": [
            {
                "turn": event.turn,
                "tool": event.call.name,
                "args": event.call.args,
                "ok": event.outcome.ok,
                "truncated": event.outcome.truncated,
            }
            for event in tools
        ],
    }


def run_arm(case: Case, arm: str, provider_id: str, port: int, max_turns: int) -> dict:
    tool_fns = AgentToolFns(
        write_file=_read_only_error,
        edit_file=_read_only_error,
        run_command=_read_only_error,
    )
    provider_controls.begin_task_context(f"context-delta-ab:{case.name}:{arm}")
    raw = None
    started = time.monotonic()
    try:
        raw = connect_provider(provider_id, port=port)
        provider = CountingProvider(raw)
        conversation = ConversationContext()
        project_map = render_project_map(case.project)
        warmup_events = []
        warmup = run(
            provider,
            case.project,
            case.warmup_task,
            max_turns=max_turns,
            on_event=warmup_events.append,
            fresh_chat=True,
            conversation=conversation,
            provider_id=provider_id,
            project_map=project_map,
            tool_fns=tool_fns,
        )
        if warmup.stop_reason != "done" or warmup.changed:
            return {
                "case": case.name,
                "arm": arm,
                "provider": provider_id,
                "ok": False,
                "eligible": False,
                "warmup_stop_reason": warmup.stop_reason,
                "followup_stop_reason": "not_run",
                "warmup_turns": warmup.turns,
                "followup_turns": 0,
                "warmup": _stage_metrics(warmup_events, set()),
                "reason": "warmup did not complete cleanly; follow-up comparison skipped",
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        sends_before = len(provider.prompts)
        chars_before = sum(map(len, provider.prompts))
        seconds_before = provider.seconds
        replies_before = provider.reply_chars
        followup_events = []
        followup = run(
            provider,
            case.project,
            followup_request(arm, case.followup_task),
            max_turns=max_turns,
            on_event=followup_events.append,
            provider_id=provider_id,
            project_map=project_map,
            tool_fns=tool_fns,
            **followup_run_kwargs(arm, conversation),
        )
        followup_prompts = provider.prompts[sends_before:]
        expected_hits = [
            value for value in case.expected if value.lower() in followup.summary.lower()
        ]
        return {
            "case": case.name,
            "arm": arm,
            "provider": provider_id,
            "ok": (
                warmup.stop_reason == "done"
                and followup.stop_reason == "done"
                and not warmup.changed
                and not followup.changed
                and len(expected_hits) == len(case.expected)
            ),
            "eligible": True,
            "warmup_stop_reason": warmup.stop_reason,
            "followup_stop_reason": followup.stop_reason,
            "warmup_turns": warmup.turns,
            "followup_turns": followup.turns,
            "followup_sends": len(followup_prompts),
            "followup_first_prompt_chars": len(followup_prompts[0]) if followup_prompts else 0,
            "followup_sent_chars": sum(map(len, followup_prompts)),
            "followup_reply_chars": provider.reply_chars - replies_before,
            "followup_provider_seconds": round(provider.seconds - seconds_before, 3),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "expected_hits": expected_hits,
            "expected_total": len(case.expected),
            "warmup": _stage_metrics(warmup_events, set()),
            "followup": _stage_metrics(followup_events, _info_keys(warmup_events)),
            "summary": followup.summary,
            "total_sent_chars": sum(map(len, provider.prompts)),
            "warmup_sent_chars": chars_before,
        }
    finally:
        try:
            if raw is not None:
                raw.close()
        finally:
            provider_controls.end_task_context()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--arm", choices=("full", "delta", "contract-delta"), required=True)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=16)
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
