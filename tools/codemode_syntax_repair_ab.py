"""Deterministic A/B probe for a bounded CodeMode syntax-repair round.

The two arms use the production kernel and CodeMode runtime. Only the scripted
model response differs: baseline repeats the malformed program, while the
candidate submits a corrected program after receiving the syntax error.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn
from codey.runtime.core.models import ToolCall, ToolResult
from tests.support.kernel_runtime import run_task_kernel

BAD_CODE = "const value = await tools.read_file({path:'a.txt'});\n// fresh snapshot\return value.value.text;"
GOOD_CODE = "const value = await tools.read_file({path:'a.txt'}); return value.value.text;"


class ScriptedModel:
    native_tools = False

    def __init__(self, responses: tuple[str, ...]) -> None:
        self._responses = responses
        self._index = 0
        self.feedback: list[str] = []

    def _next(self, prompt: str) -> str:
        self.feedback.append(prompt)
        if self._index >= len(self._responses):
            raise AssertionError("scripted model exhausted before kernel completion")
        response = self._responses[self._index]
        self._index += 1
        if response == "bad":
            return json.dumps({"tool": "codemode", "args": {"code": BAD_CODE}})
        if response == "good":
            return json.dumps({"tool": "codemode", "args": {"code": GOOD_CODE}})
        if response == "done":
            return json.dumps({"tool": "done", "args": {"summary": "recovered"}})
        raise AssertionError(f"unknown scripted response: {response}")

    def send(self, prompt: str, timeout: float | None = None) -> str:
        return self._next(prompt)

    def send_turn(self, prompt: str, tools: object, timeout: float | None = None) -> AssistantTurn:
        raise AssertionError("non-native scripted model received native turn")

    def send_tool_results(self, results: object, tools: object, timeout: float | None = None) -> str:
        return self._next(str(results))

    def acknowledge_tool_results(self, results: object, declared_tools: object,
                                 timeout: float | None = None) -> AssistantTurn:
        return AssistantTurn()


def run_arm(name: str, responses: tuple[str, ...]) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"codemode-syntax-ab-{name}-") as temporary:
        project = Path(temporary)
        (project / "a.txt").write_text("OBSERVED-SYNTAX-AB\n", encoding="utf-8")
        model = ScriptedModel(responses)
        state = TaskSession(
            policy=TaskPolicy(frozenset({"control", "project.read"}),
                              required_checks=("tool_succeeded:codemode",)),
            project=str(project), max_turns=6,
        )
        executions: list[ToolCall] = []

        def execute(call: ToolCall) -> ToolResult:
            from codey.toolchain.runtime import _format_read_file_page

            executions.append(call)
            page = _format_read_file_page("a.txt", 1, 300, 1, ["OBSERVED-SYNTAX-AB"])
            return ToolResult(call=call, ok=True, model_text=page.model_text, canonical=page.canonical)

        result = run_task_kernel(state, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=model, run_id=f"syntax-{name}",
                provider_id="scripted", effect_scope="syntax-ab", user_task="Read a.txt with CodeMode."),
            execution=KernelExecutionDeps(executors={"read_file": execute}, project_path=project),
            observation=KernelObservationDeps(),
        ))
        syntax_feedback = sum("JavaScript compilation failed" in prompt for prompt in model.feedback)
        return {
            "arm": name,
            "completed": result.completed,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "model_steps": model._index,
            "syntax_feedback_rounds": syntax_feedback,
            "child_executions": len(executions),
            "child_names": [call.name for call in executions],
            "no_child_before_repair": name == "candidate" and len(executions) == 1,
            "summary": result.summary,
        }


def main() -> int:
    baseline = run_arm("baseline", ("bad", "bad", "bad", "bad"))
    candidate = run_arm("candidate", ("bad", "good", "done"))
    report = {
        "baseline": baseline,
        "candidate": candidate,
        "benefit": candidate["completed"] is True and baseline["completed"] is False,
        "safety": (baseline["child_executions"] == 0
                   and candidate["child_executions"] == 1
                   and candidate["no_child_before_repair"] is True),
        "scope": "scripted_model_behavior_over_production_kernel_and_codemode_runtime",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["benefit"] is True and report["safety"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
