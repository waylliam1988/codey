"""Normal-entry portability smoke; synthetic seed, one generation per cell."""
from __future__ import annotations

import argparse
import json
import time
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
from codey.providers.base import ChatProvider
from codey.providers.token_accounting import ApiExchangeUsage
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
CONFIGS: tuple[tuple[int, bool], ...] = ((128, False), (8192, False), (4096, True), (8192, True))
ARMS = ("off", "on", "override")


def save(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        super().__init__(ENDPOINT, MODEL, temperature=0, native_tools=True, timeout=60,
            output_tokens=output, stream=stream, context_window_tokens=262144,
            context_reserve_tokens=32768, context_keep_recent_tokens=32000,
            usage_parser=parse_usage, request_policy=SingleAttemptPolicy())

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


def cell(home: Path, *, output: int, stream: bool, arm: str) -> dict[str, object]:
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
    usage: list[ApiExchangeUsage] = []

    def capture_usage(row: ApiExchangeUsage) -> None:
        usage.append(row)

    provider.bind_usage(home.name, capture_usage)
    task = TASK + (" For this turn explicitly choose animations and a technical tone." if arm == "override" else "")
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
        answer = decoded_object(summary)
        expected_priority = "animations" if arm == "override" else "keyboard"
        expected_tone = "technical" if arm == "override" else "warm"
        wire = provider.wire.decode("utf-8")
        sources_present = all(text in wire for text in ("tone = warm and considerate", GOAL, SEED_REPLY))
        if sources_present != (arm != "off"):
            raise ValueError("wire source admission mismatch")
        return {"output_tokens": output, "stream": stream, "arm": arm, "requests": provider.requests,
            "recall_correct": answer.get("choice") == "opalpanel" and answer.get("reason") == "offline_cursor_sync",
            "priority_correct": answer.get("priority") == expected_priority,
            "preference_correct": answer.get("tone") == expected_tone,
            "sources_present": sources_present, "elapsed_ms": elapsed,
            "input_tokens": usage[0].usage.input_tokens, "generated_tokens": usage[0].usage.output_tokens}
    finally:
        provider.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output_value: object = args.output
    if not isinstance(output_value, Path):
        raise ValueError("output path required")
    home = output_value
    home.mkdir(parents=True, exist_ok=False)
    completed: list[dict[str, object]] = []
    try:
        _models(home, "before")
        for output, stream in CONFIGS:
            for arm in ARMS:
                name = f"output{output}-stream{int(stream)}-{arm}"
                row = cell(home / name, output=output, stream=stream, arm=arm)
                completed.append(row)
                save(home / "progress.json", completed)
                print(json.dumps(row), flush=True)
        _models(home, "after")
        save(home / "report.json", {"status": "COMPLETE", "kind": "portability_smoke", "model": MODEL,
            "independent_user_samples": 1, "cells": completed})
    except Exception as exc:
        save(home / "failure.json", {"status": "INCOMPLETE", "type": type(exc).__name__,
            "message": str(exc), "completed": len(completed)})
        raise


if __name__ == "__main__":
    main()
