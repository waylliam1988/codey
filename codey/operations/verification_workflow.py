"""Offer the next explicitly requested check using owned process receipts."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from codey.codemode.verification import project_check_call, verification_spec
from codey.completion.verification_policy import VerificationCandidate, VerificationObligation, VerificationPlan
from codey.durable.model import TASK_TERMINAL_STATUSES, TaskCheckpoint
from codey.durable.session import DurableSession
from codey.durable.tool_task import ToolTaskInput, execution_effect_id
from codey.knowledge.model import canonical_json
from codey.operations.kernel_protocol import TurnSnapshot, refresh_snapshot_contract
from codey.operations.kernel_provenance import _kernel_workspace_identity_of
from codey.operations.task_session import TaskSession
from codey.operations.tool_tasks import owned_tool_task, tool_execution_result
from codey.runtime.core.execution_receipt import execution_receipt
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.task.instruction_text import instruction_text
from codey.toolchain.tool_spec import spec_for_tool
from codey.types.json_value import JsonObject
from codey.workspace.revision import WorkspaceRevisionStore

_PHASE = re.compile(r"(?m)^([1-4])\. ([^\r\n]+)")
_COMMAND = r"(python -m pytest(?: [\w./-]+)* [\w./-]+\.py)"
_START = re.compile(r'(?:Start |Call run\(command=")' + _COMMAND + r'(?:"[, ]+run_in_background=true\)| in the background)\.')
_STOP = re.compile(r"\bOnce [`'\" ]*([\w.-]+)[`'\"]* appears, (?:stop only that diagnostic|use task_cancel).+?confirm")
_HEALTH = re.compile(r"Run " + _COMMAND + r" in the foreground\.")


def _declaration(task: str) -> tuple[str, str, str, str] | None:
    matches = list(_PHASE.finditer(task))
    executable = instruction_text(task)
    if [match[1] for match in matches] != ["1", "2", "3", "4"] or any(
        not executable[match.start():match.start() + 2].strip() for match in matches
    ):
        return None
    first, rerun, probe, final = (match[2] for match in matches)
    regression, diagnostic, cleanup, health = (_START.match(first), _START.match(probe), _STOP.search(probe),
                                              _HEALTH.match(final))
    if (regression is None or diagnostic is None or cleanup is None or health is None
            or re.search(r"\b(?:repeat|rerun) the same\b", rerun, re.I) is None
            or "background" not in rerun or "passes" not in rerun
            or len({regression[1], diagnostic[1], health[1]}) != 3):
        return None
    return regression[1], diagnostic[1], cleanup[1], health[1]


def _matching_tasks(durable: DurableSession, owner: str) -> dict[tuple[str, bool], TaskCheckpoint]:
    rows: dict[tuple[str, bool], tuple[tuple[int, int], TaskCheckpoint]] = {}
    for child in durable.owned_tasks(owner):
        if child.execution_kind != "tool":
            continue
        request = ToolTaskInput.from_payload(child.state["tool_input"])
        command = request.args.get("command")
        if (request.call_name != "run" or not isinstance(command, str)
                or request.args.get("path", ".") != "."):
            continue
        key = command, request.args.get("run_in_background") is True
        sequence = request.turn, request.tool_index
        if key not in rows or sequence > rows[key][0]:
            rows[key] = sequence, child
    return {key: owned_tool_task(durable, child.task_id, owner_task_id=owner) for key, (_, child) in rows.items()}


def _ready(store: ManagedOutputStore, child: TaskCheckpoint, marker: str) -> bool:
    try:
        output = ExecutionOutputStore.read_existing(store.root.parent, execution_effect_id(child.task_id))
    except KeyError:
        return False
    if output["run_id"] != child.task_id or output["session_id"] != child.conversation_id:
        return False
    return any(marker in stream["text"].splitlines() for stream in (output["stdout"], output["stderr"]))


def _cleaned_up(store: ManagedOutputStore, child: TaskCheckpoint, marker: str) -> bool:
    return child.status == "cancelled" and child.state.get("cleanup_unknown") is not True and _ready(store, child, marker)


def _foreground_observed(session: TaskSession, command: str) -> bool:
    result = next((row for row in reversed(tuple(session._memory_results.values()))
                   if row.call.name == "run" and row.call.args.get("command") == command
                   and row.call.args.get("path", ".") == "." and row.call.args.get("run_in_background") is False), None)
    if result is None or not result.ok:
        return False
    execution = execution_receipt(result)
    return (execution is not None and execution.status == "exit"
            and execution.exit_code_scope == "process" and execution.exit_code == 0)


def completion_workflow(session: TaskSession, durable: DurableSession, owner: str, store: ManagedOutputStore,
                        ) -> tuple[VerificationPlan | None, str]:
    declaration = _declaration(session.task_text)
    if declaration is None or session.verification_forbidden or not session.policy.allows("project.verify"):
        return None, ""
    regression, diagnostic, marker, health = declaration
    tasks = _matching_tasks(durable, owner)
    diag = tasks.get((diagnostic, True))
    if diag is None or not _cleaned_up(store, diag, marker):
        return None, "Finish the declared regression and confirm cancellation of the owned diagnostic after its ready marker."
    if not _foreground_observed(session, health):
        return None, "Run the declared foreground health check after confirming the diagnostic stopped."
    plan = VerificationPlan(tuple(VerificationObligation(VerificationCandidate(command), session.verification_paths)
                                  for command in (regression, health)))
    return plan, ""


def _offer_run(snapshot: TurnSnapshot, command: str, *, background: bool) -> tuple[TurnSnapshot, str]:
    spec = spec_for_tool("run")
    if spec is None or "run" not in snapshot.inventory_names:
        return snapshot, ""
    selected = verification_spec(spec, VerificationCandidate(command), background=background)
    updated = refresh_snapshot_contract(replace(snapshot, frozen_specs=tuple(
        selected if item.name == "run" else item for item in snapshot.frozen_specs)))
    return updated, "Next user-declared check:\n" + canonical_json(project_check_call(selected))


def _offer_control(snapshot: TurnSnapshot, tool: str, args: JsonObject, state: str) -> tuple[TurnSnapshot, str]:
    action = {"tool": tool, "args": args}
    waiting = refresh_snapshot_contract(replace(snapshot,
        tool_names=tuple(name for name in snapshot.tool_names if name != "run"),
        nested_tool_names=tuple(name for name in snapshot.nested_tool_names if name != "run"),
        frozen_specs=tuple(spec for spec in snapshot.frozen_specs if spec.name != "run"),
        custom_executors=tuple(row for row in snapshot.custom_executors if row[0] != "run")))
    return waiting, state + "\n" + canonical_json(action)


def verification_workflow(session: TaskSession, snapshot: TurnSnapshot, durable: DurableSession, owner: str,
                          store: ManagedOutputStore, workspace_store: WorkspaceRevisionStore | None, *,
                          ignored_paths: tuple[str, ...]) -> tuple[TurnSnapshot, str]:
    declaration = _declaration(session.task_text)
    if declaration is None or session.verification_forbidden or not session.policy.allows("project.verify"):
        return snapshot, ""
    regression, diagnostic, marker, health = declaration
    tasks = _matching_tasks(durable, owner)
    check = tasks.get((regression, True))
    if check is None:
        return _offer_run(snapshot, regression, background=True)
    if check.status not in TASK_TERMINAL_STATUSES:
        return _offer_control(snapshot, "task_output", {"task_ref": check.task_id, "wait_ms": 30000},
                              "Wait for the user-declared regression result before continuing.")
    original = tool_execution_result(durable, check, store=store, project_path=Path(session.project),
                                     workspace_store=workspace_store, ignored_paths=ignored_paths)
    execution = execution_receipt(original[1]) if original is not None else None
    identity = _kernel_workspace_identity_of(original[1]) if original is not None else None
    if (identity is None or (identity.revision, identity.fingerprint) != (
            session.workspace_revision, session.workspace_fingerprint)):
        return _offer_run(snapshot, regression, background=True)
    if execution is None or execution.status != "exit" or execution.exit_code_scope != "process" or execution.exit_code != 0:
        updated, command = _offer_run(snapshot, regression, background=True)
        return updated, "Inspect the failing regression and repair the workspace before repeating it.\n" + command
    child = tasks.get((diagnostic, True))
    if child is None:
        return _offer_run(snapshot, diagnostic, background=True)
    if _cleaned_up(store, child, marker):
        if _foreground_observed(session, health):
            return snapshot, f"Owned diagnostic {child.task_id} emitted {marker} and cancellation is confirmed."
        updated, command = _offer_run(snapshot, health, background=False)
        return updated, f"Owned diagnostic {child.task_id} emitted {marker} and cancellation is confirmed.\n" + command
    if child.status in TASK_TERMINAL_STATUSES:
        return snapshot, f"Owned diagnostic {child.task_id} ended without confirmed ready-and-cancelled evidence."
    if _ready(store, child, marker) and not child.state.get("cancel_requested"):
        return _offer_control(snapshot, "task_cancel", {"task_ref": child.task_id, "wait_ms": 10000},
                              f"Original live output from {child.task_id} contains {marker}. Cancel only this diagnostic.")
    return _offer_control(snapshot, "task_output", {"task_ref": child.task_id, "wait_ms": 1000},
                          f"Read live output or confirm cleanup of owned diagnostic {child.task_id}.")
