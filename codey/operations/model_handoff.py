"""Read original tool receipts when a user hands work to a new model run."""
from __future__ import annotations

from pathlib import Path

from codey.durable.model import TASK_TERMINAL_STATUSES, TaskCheckpoint, thaw_json
from codey.durable.session import DurableSession
from codey.knowledge.model import canonical_json
from codey.operations.kernel_receipts import restore_result_receipt
from codey.policies.redaction import looks_prompt_visible_secret
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.effects.effect_records import RuntimeEffectProjection, RuntimeEffectStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.task.model import HandoffRun, ModelHandoffRejected
from codey.toolchain.tool_spec import canonical_tool_name
from codey.types.json_value import JsonObject, decode_json_value

OBSERVATION_DENIALS = frozenset({
    "project.write", "project.verify", "browser.effect", "browser.interact", "shell.approval",
    "knowledge.write", "knowledge.link",
})
_HANDOFF_TOOLS = frozenset({
    "read_file", "edit", "run", "open_url", "open_result", "source_search", "browser_read",
    "browser_snapshot", "browser_click", "browser_wait", "browser_fill", "browser_navigate",
})


def authorize_model_handoff(durable: DurableSession, *, request: HandoffRun, target: ApiRunSelection | None,
                            session_id: str, run_id: str, project: str | None,
                            ) -> tuple[TaskCheckpoint, tuple[RuntimeEffectProjection, ...]]:
    if target is None or request.source_run_id == run_id:
        raise ModelHandoffRejected("Model handoff requires a new run and an explicit API model selection.")
    try:
        source = durable.task(request.source_run_id)
    except KeyError:
        raise ModelHandoffRejected("Handoff source is unavailable in this conversation.") from None
    source_project = source.state.get("project")
    if (source.conversation_id != session_id or source.execution_kind != "model"
            or _project_path(source_project) != _project_path(project)):
        raise ModelHandoffRejected("Handoff source is unavailable in this conversation and project.")
    children = tuple(child for child in durable.tasks() if child.owner_task_id == source.task_id)
    if source.status not in TASK_TERMINAL_STATUSES or any(child.status not in TASK_TERMINAL_STATUSES for child in children):
        raise ModelHandoffRejected("The original run must reach a settled boundary before handoff.")
    selection = source.provider_snapshot.get("model_selection")
    if selection is None:
        raise ModelHandoffRejected("Handoff source has no original API selection.")
    original = ApiRunSelection.from_payload(thaw_json(selection))
    if (original.connection_id, original.connection_revision, original.model_id) == (
            target.connection_id, target.connection_revision, target.model_id):
        raise ModelHandoffRejected("Select a different API connection or model for handoff.")
    store = RuntimeEffectStore(durable=durable)
    effects = store.load_effects(session_id, source.task_id)
    effects += tuple(effect for child in children if child.execution_kind == "tool"
                     for effect in store.load_effects(session_id, child.task_id)
                     if effect.intent.effect_category == "tool_call")
    if not request.observe_only and any(_unknown_action(effect) for effect in effects):
        raise ModelHandoffRejected("The original run has an uncertain action. Select observation-only handoff.")
    return source, effects


def _project_path(value: object) -> Path | None:
    return Path(value).expanduser().resolve() if isinstance(value, str) and value else None


def _unknown_action(effect: RuntimeEffectProjection) -> bool:
    intent = effect.intent
    return (intent.effect_category == "tool_call" and intent.replay_class == "unsafe"
            and (effect.settlement is None or effect.settlement.status == "interrupted"))


def render_model_handoff(source: TaskCheckpoint, effects: tuple[RuntimeEffectProjection, ...],
                         outputs: ManagedOutputStore | None) -> str:
    rows: list[JsonObject] = []
    unknown: list[JsonObject] = []
    for effect in reversed(effects):
        intent, settlement = effect.intent, effect.settlement
        if _unknown_action(effect):
            unknown.append({"tool": intent.tool_name, "effect_ref": intent.effect_id})
            continue
        if canonical_tool_name(intent.tool_name) not in _HANDOFF_TOOLS or settlement is None or len(rows) >= 12:
            continue
        result = restore_result_receipt(effect, store=outputs, session_id=source.conversation_id, run_id=intent.run_id)
        if result.kind == "accepted":
            continue
        row: JsonObject = {"result_ref": intent.effect_id, "tool": result.call.name, "ok": result.ok}
        for key in ("path", "url"):
            if isinstance(value := result.call.args.get(key), str):
                row[key] = value
        for key in ("file_version", "read_passages"):
            if key in result.canonical:
                row[key] = decode_json_value(result.canonical[key])
        if settlement.exit_code is not None:
            row["exit_code"] = settlement.exit_code
        if (result.ok and result.call.name in {"read_file", "open_url", "open_result", "browser_read"}
                and not result.truncated and len(result.model_text) <= 900
                and not looks_prompt_visible_secret(result.model_text)):
            row["observed_text"] = result.model_text
        rows.append(row)
    return ("User-authorized model handoff. These references describe original work from the source run. "
            "Check the current workspace or page before relying on its present state. Preserve completed work. "
            "An unknown action requires a read-only observation before any new action is authorized.\n"
            + canonical_json({"source_run": source.task_id, "receipts": rows, "unknown_actions": unknown}))
