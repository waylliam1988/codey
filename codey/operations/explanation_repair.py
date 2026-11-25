"""Explanation repair projected from the existing durable rejection episode."""
from __future__ import annotations

import json
from dataclasses import replace

from codey.operations.completion_context import CompletionContext
from codey.operations.completion_output import prepare_completion_output
from codey.operations.kernel.kernel_protocol import TurnSnapshot, refresh_snapshot_contract
from codey.operations.kernel_contracts import KernelDecision
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import is_output_check, schema_for
from codey.policies.tool_requirement import parse_tool_success_check
from codey.providers.base import ProviderToolResult
from codey.runtime.core.progress import ProgressSnapshot
from codey.types.json_value import JsonObject, JsonValue, parse_json_object

FAILURE_KEY = "completion_explanation"


def active(progress: ProgressSnapshot) -> bool:
    return progress.repair_failure_key == FAILURE_KEY and progress.repair_rejections > 0


def repair_snapshot(snapshot: TurnSnapshot) -> TurnSnapshot:
    names = tuple(name for name in snapshot.tool_names if name in {"done", "read_tool_result"})
    return refresh_snapshot_contract(replace(snapshot, tool_names=names, nested_tool_names=(),
        frozen_specs=tuple(spec for spec in snapshot.frozen_specs if spec.name in names),
        custom_executors=tuple(row for row in snapshot.custom_executors if row[0] in names),
        browser_followups=False, browser_references=None, browser_fill_text=None,
        browser_copy=None, input_binding=None, task_navigation_url=None, task_source_url=None))


def repair_messages(messages: list[ProviderToolResult] | None, prompt: str, *, rejected_control: bool) -> list[ProviderToolResult] | None:
    if not messages:
        return messages
    return [replace(message, content="ERROR: only explanation repair is available; the proposal was not accepted."
                    if rejected_control else message.content,
                    user_content=prompt if index == len(messages) - 1 else "")
            for index, message in enumerate(messages)]


def repair_prompt(session: TaskSession, context: CompletionContext, snapshot: TurnSnapshot, *,
                  native: bool, decision: KernelDecision | None) -> str:
    from codey.operations.kernel_prompt import _completion_followup

    prompt = repair_context(session, context)
    if decision is not None and decision["purpose"] == "retry":
        return prompt + "\n\n" + decision["prompt"]
    done = next((spec for spec in snapshot.model_specs if spec.name == "done"), None)
    return _completion_followup(prompt, native=native, done_spec=done)


def repair_context(session: TaskSession, context: CompletionContext) -> str:
    output = prepare_completion_output(session, session.last_done_text, context)
    checks = session.policy.required_checks
    schema = next((schema_for(check) for check in checks if is_output_check(check)), None)
    properties = schema.get("properties") if schema is not None else None
    facts: list[JsonValue] = []
    for check in checks:
        rule = parse_tool_success_check(check)
        observation = output.observations.get(rule.check_id) if rule is not None else None
        if rule is not None and rule.answer_contains and observation is not None and observation.evaluation.passed:
            facts.append({"required_in_prose": observation.value, "receipts": list(observation.result_refs)})
    basis: JsonObject = {
        "explanation_schema": properties.get("explanation") if isinstance(properties, dict) else None,
        "supported_facts": facts,
        "draft": parse_json_object(output.text).get("explanation"),
    }
    return ("Only repair the explanation. Browser actions, independent fields and project edits are settled. "
            "Do not repeat them. Satisfy all constraints together. "
            + "\nRepair basis from committed receipts:\n" + json.dumps(basis, ensure_ascii=False))
