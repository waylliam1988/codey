"""Render outstanding requirements against the request's frozen action input."""
from __future__ import annotations

import json

from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.structured_actions import RequiredAction, action_covers, required_action
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import has_explanation
from codey.policies.tool_requirement import parse_tool_success_check
from codey.types.json_value import JsonObject


def action_guidance(session: TaskSession, snapshot: TurnSnapshot, *, native: bool,
                    browser_document: JsonObject | None = None, initial: bool = False) -> str:
    binding = snapshot.input_binding
    if binding is None or binding.kind != "actions" or snapshot.policy is None:
        return ""
    requirements = [rule for check in snapshot.policy.required_checks
                    if (rule := parse_tool_success_check(check)) is not None]
    completed: list[str] = []
    pending: list[tuple[str, RequiredAction]] = []
    details: list[str] = []
    for rule in requirements:
        observation = rule.observe(session.executed, results=session._memory_results, browser_document=browser_document)
        label = rule.check_id + " (" + rule.tool + ")"
        if initial and rule.answer_pointer is not None:
            details.append(rule.model_guidance(explanation=has_explanation(snapshot.policy.required_checks), execution=""))
        if observation.evaluation.passed:
            completed.append(label)
            continue
        reason = rule.feedback_reason(observation.evaluation)
        details.append(label + ": " + observation.evaluation.reason_code + (". " + reason if reason else ""))
        if rule.tool == "codemode" and rule.arguments_json == "{}" and not rule.via:
            details.append("A successful permitted action establishes the CodeMode execution requirement.")
            continue
        pending.append((label, required_action(rule, binding.model_spec, snapshot.task_navigation_url, snapshot.frozen_specs)))
    calls: dict[str, list[str]] = {}
    for label, candidate in pending:
        rule = next(rule for rule in requirements if label == rule.check_id + " (" + rule.tool + ")")
        candidate = next((other for _, other in pending if other.kind == candidate.kind
                          and action_covers(other, rule)), candidate)
        if candidate.error:
            details.append(label + ": " + candidate.error)
        elif candidate.missing:
            details.append(label + ": available action " + candidate.kind + "; known parameters "
                + json.dumps(candidate.action, ensure_ascii=False) + "; still determine " + ", ".join(candidate.missing)
                + ". This is incomplete and cannot be executed as shown.")
        else:
            args: JsonObject = {"action": candidate.action}
            payload = args if native else {"tool": binding.model_spec.name, "args": args}
            key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            calls.setdefault(key, []).append(label)
    parts = ["Declared execution and evidence status (committed receipts):",
             "Satisfied execution/evidence: " + (", ".join(completed) or "none"), *details]
    if calls:
        parts.append("Calls validated against this request's frozen interface and fixed lowering "
                     + ("(native codemode arguments):" if native else "(text calls):"))
        parts.extend(call + "\nFor: " + ", ".join(labels) for call, labels in calls.items())
    parts.append("Reuse satisfied evidence. Complete the outstanding requirements before done; revise only the answer "
                 "when completion feedback identifies an answer defect.")
    return "\n".join(parts)
