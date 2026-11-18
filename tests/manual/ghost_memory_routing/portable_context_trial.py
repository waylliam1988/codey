"""Normal-entry portability smoke; synthetic seed, one generation per cell."""
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from codey.app import provider_services
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.payload_types import decoded_object, mapping_fields
from codey.operations.task_entry import run_task_submission
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ChatProvider, ProviderToolDefinition
from codey.providers.local_usage import configure_usage
from codey.providers.token_accounting import ApiExchangeUsage, ReportedUsage
from codey.task.model import TaskSubmission
from tests.manual.ghost_memory_routing.project_experience_continuity import _models
from tests.manual.ghost_memory_routing.world_model_verification_review import (
    ENDPOINT,
    MODEL,
    SingleAttemptPolicy,
    parse_usage,
)
from tests.test_ghost_received_input_lifetime import _queue

GOAL = "Prioritize keyboard access over extra animations for this project interface."
QUOTE = "Please use a warm and considerate tone."
SOURCE = f"Project interface kickoff. {QUOTE} {GOAL}"
SEED_REPLY = "We chose opalpanel because offline_cursor_sync preserves the project interface cursor."
TASK = (
    "Recall our project interface choice and its reason, then choose the interface priority and response tone. "
    "Use saved local context if available; otherwise use unknown for choice/reason/priority and neutral for tone. "
    "Do not inspect files or call any other tools. Call done once with summary containing only a JSON object "
    "with string fields choice, reason, priority, tone. Use a short lowercase identifier for each field; "
    "priority is keyboard or animations. No changes required."
)
LABEL_TASK = (
    "Recall our project interface choice and its reason, then choose the interface priority and response tone. "
    "Use saved local context if available; otherwise use unknown for choice/reason/priority and neutral for tone. "
    "Do not inspect files or call any other tools. Call done once with summary containing only a JSON object "
    "with exactly four string fields. Each value must be exactly one of its listed labels, without explanations: "
    "choice: unknown, opalpanel, riverkit, emberview; "
    "reason: unknown, offline_cursor_sync, remote_multiwriter, local_cache; "
    "priority: unknown, keyboard, animations; tone: neutral, warm, technical. "
    "Map warm and considerate to the label warm. Choose values from the available context, "
    "not merely because they appear in this list. No changes required."
)
CONFIGS: tuple[tuple[int, bool], ...] = ((128, False), (8192, False), (4096, True), (8192, True))
PROTOCOL_CONFIGS = {"legacy": CONFIGS, "labels-v2": ((8192, False), (8192, True))}
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "choice": ("unknown", "opalpanel", "riverkit", "emberview"),
    "reason": ("unknown", "offline_cursor_sync", "remote_multiwriter", "local_cache"),
    "priority": ("unknown", "keyboard", "animations"),
    "tone": ("neutral", "warm", "technical"),
}
ARMS = ("off", "on", "override")


def save(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def configure_stream_usage(payload: dict[str, object]) -> None:
    """Keep the experiment callback precise at the shared Local adapter boundary."""
    configure_usage(payload)


def score_answer(summary: object, *, arm: str) -> dict[str, bool]:
    if arm not in ARMS:
        raise ValueError("unsupported arm")
    answer = decoded_object(summary)
    choice = answer.get("choice") == "opalpanel"
    reason = answer.get("reason") == "offline_cursor_sync"
    return {
        "format_valid": answer.keys() == FIELD_LABELS.keys() and all(
            isinstance(value, str) and value in FIELD_LABELS[key] for key, value in answer.items()),
        "choice_correct": choice,
        "reason_correct": reason,
        "recall_correct": choice and reason,
        "priority_correct": answer.get("priority") == ("animations" if arm == "override" else "keyboard"),
        "preference_correct": answer.get("tone") == ("technical" if arm == "override" else "warm"),
        "baseline_unknown": all(answer.get(key) == value for key, value in
                                (("choice", "unknown"), ("reason", "unknown"),
                                 ("priority", "unknown"), ("tone", "neutral"))),
    }


class SeedProvider:
    name = "Synthetic completed history"
    location = "fixture://portable-context"

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        return SEED_REPLY

    def close(self) -> None:
        pass


class CapturedProvider(ApiProvider):
    def __init__(self, home: Path, *, output: int, stream: bool) -> None:
        self.home, self.requests = home, 0
        self.wire = b""
        self.usage_records: list[ApiExchangeUsage] = []
        super().__init__(ENDPOINT, MODEL, temperature=0, native_tools=True, timeout=60,
            output_tokens=output, stream=stream, context_window_tokens=262144,
            context_reserve_tokens=32768, context_keep_recent_tokens=32000,
            usage_parser=self._parse_usage, configure_request=configure_stream_usage, request_policy=SingleAttemptPolicy())

    def _parse_usage(self, event: dict[str, object]) -> ReportedUsage | None:
        with (self.home / "response-events.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
        return parse_usage(event)

    def bind_usage(self, connection_id: str, sink: Callable[[ApiExchangeUsage], None]) -> None:
        def retain(record: ApiExchangeUsage) -> None:
            self.usage_records.append(record)
            sink(record)
        super().bind_usage(connection_id, retain)

    def send_turn(self, prompt: str, tools: list[ProviderToolDefinition] | None = None,
                  timeout: float | None = None, *, tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> AssistantTurn:
        turn = super().send_turn(prompt, tools, timeout, tool_call_observations=tool_call_observations,
            tool_call_history_groups=tool_call_history_groups, output_token_limit=output_token_limit,
            require_tool_call=require_tool_call)
        save(self.home / "reply.json", {"text": turn.text, "reasoning": turn.reasoning,
            "finish": turn.finish.value, "raw": dict(turn.raw)})
        return turn

    def _observe_http_attempt(self, *, attempt: int, data: bytes, phase: str,
                              response_bytes: int, seconds: float) -> None:
        if phase == "request":
            self.requests += 1
            self.wire = data
            (self.home / f"request-{self.requests}.json").write_bytes(data)


def run_task(state: AppContext, provider: ChatProvider, project: Path, *, task: str, identity: str,
             intent: str) -> dict[str, object]:
    session = "source-session" if identity == "source" else "next-session"
    request = TaskSubmission(session, str(project), task, 1, False, "deepseek", intent=intent, run_id=identity)
    _queue(state).create(identity, {"session_id": session, "project": str(project), "task": task,
        "provider": "deepseek", "run_id": identity, "intent": intent, "max_turns": 1, "project_changes_required": False})
    state.durable.claim_task(identity)
    state.durable.start_task(identity, state=dict(state.durable.task(identity).state))

    def connect(_identity: str) -> ChatProvider:
        return provider

    def no_advisors(_state: object) -> dict[str, bool]:
        return {}

    with patch.object(provider_services, "provider_availability", no_advisors):
        run_task_submission(build_task_deps(state, connect_provider=connect), request)
    if not state.wait_for_ghost_sleep(timeout=10):
        raise TimeoutError("local maintenance did not settle")
    event: object = state.run_registry.last_terminal_event()
    if not mapping_fields(event) or event.get("run_id") != identity:
        raise ValueError("missing terminal event")
    return dict(event)


def seed(home: Path, project: Path) -> None:
    with closing(AppContext(home, start_scheduler=False)) as state:
        result = run_task(state, SeedProvider(), project, task=SOURCE, identity="source", intent="chat")
        if result.get("stop_reason") != "done":
            raise ValueError("synthetic source did not complete")
        surface = GhostControlSurface.from_state_home(home)
        for action in (
            {"action": "propose_preference", "scope": "user", "conflict_key": "tone", "value_key": "warm",
             "evidence_quote": QUOTE},
            {"action": "propose_project_goal", "scope": "project", "goal": GOAL,
             "goal_key": "accessibility", "evidence_quote": GOAL},
        ):
            body: dict[str, object] = {**action, "id": "source", "session_id": "source-session", "project": str(project)}
            status, payload = surface.dispatch_action(body)
            candidate = payload.get("candidate")
            if status != 200 or not mapping_fields(candidate) or not isinstance(candidate.get("id"), str):
                raise ValueError("source proposal failed")
            status, payload = surface.dispatch_action({"action": "accept_candidate", "id": candidate["id"],
                "project": str(project), "session_id": "source-session"})
            if status != 200 or payload.get("ok") is not True:
                raise ValueError("source review failed")


def cell(home: Path, *, output: int, stream: bool, arm: str, protocol: str = "legacy") -> dict[str, object]:
    if protocol not in PROTOCOL_CONFIGS or arm not in ARMS:
        raise ValueError("unsupported protocol or arm")
    home.mkdir(parents=True)
    project = home / "project"
    project.mkdir()
    state_home = home / "state"
    seed(state_home, project)
    surface = GhostControlSurface.from_state_home(state_home)
    assert surface.inbox is not None
    if not surface.inbox.set_control("admit_hints", arm != "off"):
        raise ValueError("control update failed")
    provider = CapturedProvider(home, output=output, stream=stream)
    usage = provider.usage_records
    task = (LABEL_TASK if protocol == "labels-v2" else TASK) + (
        " For this turn explicitly choose animations and a technical tone." if arm == "override" else "")
    started = time.monotonic()
    try:
        with closing(AppContext(state_home, start_scheduler=False)) as state:
            result = run_task(state, provider, project, task=task, identity="next", intent="project")
        elapsed = (time.monotonic() - started) * 1000
        save(home / "terminal.json", result)
        save(home / "usage.json", [row.to_payload() for row in usage])
        if result.get("stop_reason") != "done" or provider.requests != 1 or len(usage) != 1:
            raise RuntimeError("normal entry failed, incomplete response, or request count mismatch")
        summary = result.get("summary")
        if not isinstance(summary, str):
            raise ValueError("missing visible summary")
        scores = score_answer(summary, arm=arm)
        wire = provider.wire.decode("utf-8")
        payload = decoded_object(wire)
        usage_requested = payload.get("stream_options") == {"include_usage": True}
        if usage_requested != stream:
            raise ValueError("stream usage request configuration mismatch")
        sources_present = all(text in wire for text in ("tone = warm and considerate", GOAL, SEED_REPLY))
        if sources_present != (arm != "off"):
            raise ValueError("wire source admission mismatch")
        row: dict[str, object] = {"protocol": protocol, "output_tokens": output,
            "stream": stream, "arm": arm, "requests": provider.requests, **scores,
            "sources_present": sources_present, "elapsed_ms": elapsed,
            "usage_requested": usage_requested, "usage_status": usage[0].usage_status,
            "input_tokens": usage[0].usage.input_tokens, "generated_tokens": usage[0].usage.output_tokens}
        save(home / "metrics.json", row)
        if protocol == "labels-v2" and not scores["format_valid"]:
            raise ValueError("explicit label interface failed; reply and metrics retained")
        return row
    finally:
        provider.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol", choices=tuple(PROTOCOL_CONFIGS), default="legacy")
    parser.add_argument("--resume-after-authorized-retry", action="store_true")
    args = parser.parse_args()
    output_value: object = args.output
    if not isinstance(output_value, Path):
        raise ValueError("output path required")
    home = output_value
    protocol_value: object = args.protocol
    if not isinstance(protocol_value, str) or protocol_value not in PROTOCOL_CONFIGS:
        raise ValueError("unsupported protocol")
    protocol = protocol_value
    configs = PROTOCOL_CONFIGS[protocol]
    resume_value: object = args.resume_after_authorized_retry
    resume = resume_value is True
    completed: list[dict[str, object]] = []
    if resume:
        raw: object = json.loads((home / "progress.json").read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("invalid saved progress")
        rows: list[object] = raw
        for item in rows:
            if not mapping_fields(item):
                raise ValueError("invalid saved cell")
            if item.get("protocol", "legacy") != protocol:
                raise ValueError("cannot mix frozen protocols")
            completed.append(dict(item))
        expected = [(output, stream, arm) for output, stream in configs for arm in ARMS]
        if len(completed) >= len(expected) or any(
            (row.get("output_tokens"), row.get("stream"), row.get("arm")) != config
            for row, config in zip(completed, expected, strict=False)
        ):
            raise ValueError("saved cells do not match the frozen matrix")
        save(home / "authorized-resume.json", {"completed_before_resume": len(completed),
            "failed_attempt_retained": True, "retry_limit": 1, "authorization": "explicit user request"})
    else:
        home.mkdir(parents=True, exist_ok=False)
    completed_before = len(completed)
    position = 0
    try:
        _models(home, "resume" if resume else "before")
        for output, stream in configs:
            for arm in ARMS:
                position += 1
                if position <= completed_before:
                    continue
                name = f"output{output}-stream{int(stream)}-{arm}"
                cell_home = home / name
                retry = resume and position == completed_before + 1 and cell_home.exists()
                if retry:
                    cell_home = cell_home / "authorized-retry-1"
                row = cell(cell_home, output=output, stream=stream, arm=arm, protocol=protocol)
                if retry:
                    row["authorized_retry"] = True
                completed.append(row)
                save(home / "progress.json", completed)
                print(json.dumps(row), flush=True)
        _models(home, "after")
        save(home / "report.json", {"status": "COMPLETE_WITH_AUTHORIZED_RETRY" if resume else "COMPLETE",
            "protocol": protocol, "kind": "portability_smoke", "model": MODEL,
            "independent_user_samples": 1, "cells": completed})
    except Exception as exc:
        save(home / ("resume-failure.json" if resume else "failure.json"), {"status": "INCOMPLETE", "type": type(exc).__name__,
            "message": str(exc), "completed": len(completed)})
        raise


if __name__ == "__main__":
    main()
