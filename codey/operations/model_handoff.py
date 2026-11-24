"""Read original tool receipts when a user hands work to a new model run."""
from __future__ import annotations

from pathlib import Path

from codey.durable.model import TASK_TERMINAL_STATUSES, TaskCheckpoint, thaw_json
from codey.durable.session import DurableSession
from codey.knowledge.model import canonical_json
from codey.operations.kernel_receipts import restore_result_receipt
from codey.policies.redaction import looks_prompt_visible_secret
from codey.research.passages import receipt_passages
from codey.runtime.core.api_selection import ApiRunSelection
from codey.runtime.core.models import ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectProjection, RuntimeEffectStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.task.model import HandoffRun, ModelHandoffRejected
from codey.toolchain.tool_spec import canonical_tool_name
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    decode_json_value,
    require_json_boolean,
    require_json_string,
)

OBSERVATION_DENIALS = frozenset({
    "project.write", "project.verify", "browser.effect", "browser.interact", "shell.approval",
    "knowledge.write", "knowledge.link",
})
_HANDOFF_TOOLS = frozenset({
    "read_file", "edit", "run", "open_url", "open_result", "source_search", "browser_read",
    "browser_snapshot", "browser_click", "browser_wait", "browser_fill", "browser_navigate",
})
_HANDOFF_RECEIPT_LIMIT = 12


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


def _project_receipts(source: TaskCheckpoint, effects: tuple[RuntimeEffectProjection, ...],
                      outputs: ManagedOutputStore | None,
                      inherited: dict[str, tuple[RuntimeEffectProjection, ToolResult]],
                      ) -> dict[str, tuple[RuntimeEffectProjection, ToolResult]]:
    receipts: dict[str, tuple[RuntimeEffectProjection, ToolResult]] = {}
    for effect in reversed(effects):
        intent = effect.intent
        if intent.effect_category != "tool_call" or effect.settlement is None or _unknown_action(effect):
            continue
        tool = canonical_tool_name(intent.tool_name)
        inherited_read = tool == "read_tool_result" and effect.settlement.status == "ok" and bool(inherited)
        if tool not in _HANDOFF_TOOLS and not inherited_read:
            continue
        result = restore_result_receipt(effect, store=outputs, session_id=source.conversation_id, run_id=intent.run_id)
        if inherited_read:
            page = result.canonical.get("receipt_page")
            if isinstance(page, dict) and isinstance(ref := page.get("result_ref"), str) and ref in inherited:
                receipts.setdefault(ref, inherited[ref])
        elif result.kind != "accepted":
            receipts[intent.effect_id] = (effect, result)
        if len(receipts) == _HANDOFF_RECEIPT_LIMIT:
            break
    return receipts


def _prior_handoff(durable: DurableSession, target: TaskCheckpoint,
                   ) -> tuple[TaskCheckpoint, tuple[RuntimeEffectProjection, ...]] | None:
    raw = thaw_json(target.state.get("input"))
    if not isinstance(raw, dict):
        return None
    input_row = decode_json_object(raw)
    if input_row.get("kind") != "handoff_run" or target.provider_snapshot.get("model_selection") is None:
        return None
    request = HandoffRun(require_json_string(input_row.get("source_run_id"), "source run"),
                         require_json_string(input_row.get("instruction"), "handoff instruction"),
                         require_json_boolean(input_row.get("observe_only"), "observe only"))
    selection = ApiRunSelection.from_payload(thaw_json(target.provider_snapshot["model_selection"]))
    project = target.state.get("project")
    return authorize_model_handoff(durable, request=request, target=selection, session_id=target.conversation_id,
        run_id=target.task_id, project=project if isinstance(project, str) else None)


def _handoff_receipts(durable: DurableSession, source: TaskCheckpoint, effects: tuple[RuntimeEffectProjection, ...],
                      outputs: ManagedOutputStore | None) -> dict[str, tuple[RuntimeEffectProjection, ToolResult]]:
    # Rebuild each hop from its original receipts. An intermediate run can
    # forward an inherited receipt only through its own successful read.
    chain = [(source, effects)]
    seen = {source.task_id}
    while any(effect.intent.effect_category == "tool_call" and effect.intent.tool_name == "read_tool_result"
              and effect.settlement is not None and effect.settlement.status == "ok" for effect in effects):
        prior = _prior_handoff(durable, source)
        if prior is None:
            break
        source, effects = prior
        if source.task_id in seen:
            raise ModelHandoffRejected("Model handoff cannot contain a cycle.")
        seen.add(source.task_id)
        chain.append(prior)
    receipts: dict[str, tuple[RuntimeEffectProjection, ToolResult]] = {}
    for source, effects in reversed(chain):
        receipts = _project_receipts(source, effects, outputs, receipts)
    return receipts


def read_handoff_receipt(durable: DurableSession, *, session_id: str, run_id: str, identity: str,
                         outputs: ManagedOutputStore | None) -> tuple[str, ToolResult] | None:
    target = durable.task(run_id)
    if target.conversation_id != session_id or target.execution_kind != "model":
        return None
    prior = _prior_handoff(durable, target)
    if prior is not None and (receipt := _handoff_receipts(durable, *prior, outputs).get(identity)) is not None:
        effect, result = receipt
        return effect.intent.run_id, result
    return None


def render_model_handoff(source: TaskCheckpoint, effects: tuple[RuntimeEffectProjection, ...],
                         outputs: ManagedOutputStore | None, *, durable: DurableSession) -> str:
    return model_handoff_context(source, effects, outputs, durable=durable)[0]


def model_handoff_context(source: TaskCheckpoint, effects: tuple[RuntimeEffectProjection, ...],
                          outputs: ManagedOutputStore | None, *, durable: DurableSession) -> tuple[str, tuple[str, ...]]:
    rows: list[JsonObject] = []
    unknown: list[JsonObject] = [{"tool": effect.intent.tool_name, "effect_ref": effect.intent.effect_id}
                                for effect in reversed(effects) if _unknown_action(effect)]
    source_urls: dict[str, None] = {}
    for effect, result in _handoff_receipts(durable, source, effects, outputs).values():
        intent, settlement = effect.intent, effect.settlement
        assert settlement is not None
        row: JsonObject = {"result_ref": intent.effect_id, "tool": result.call.name, "ok": result.ok}
        for key in ("path", "url"):
            if isinstance(value := result.call.args.get(key), str):
                row[key] = value
        for key in ("file_version", "read_passages"):
            if key in result.canonical:
                row[key] = decode_json_value(result.canonical[key])
        if settlement.exit_code is not None:
            row["exit_code"] = settlement.exit_code
        if result.ok and result.canonical.get("read_passages"):
            passages = receipt_passages(result.canonical["research_observation"], result.canonical["read_passages"])
            originals: list[JsonValue] = []
            for passage in passages[:3]:
                excerpt = passage.excerpt[:500]
                if excerpt and not looks_prompt_visible_secret(excerpt) and not looks_prompt_visible_secret(passage.source.final_url):
                    originals.append({"passage_ref": passage.ref, "url": passage.source.final_url,
                        "source_hash": passage.source.text_hash, "start": passage.start,
                        "end": passage.start + len(excerpt), "excerpt": excerpt})
                    source_urls[passage.source.final_url] = None
            if originals:
                row["original_passages"] = originals
        elif (result.ok and result.call.name == "read_file"
                and not result.truncated and len(result.model_text) <= 900
                and not looks_prompt_visible_secret(result.model_text)):
            row["observed_text"] = result.model_text
        rows.append(row)
    text = ("User-authorized model handoff. Follow the current user task. The source run's receipts are evidence "
            "of completed work. Reuse their observed contents and verify the current version of any file or page you change. "
            "Use read_tool_result with an original result_ref to recover details beyond this excerpt. "
            "An unknown action requires a read-only observation before any new action is authorized.\n"
            + canonical_json({"source_run": source.task_id, "receipts": rows, "unknown_actions": unknown}))
    return text, tuple(source_urls)
