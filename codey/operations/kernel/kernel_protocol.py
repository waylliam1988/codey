"""Single turn protocol for the unified kernel: JSON text and native calls.

Web JSON replies and native ``AssistantTurn`` values converge here into one
canonical ``ToolPlan`` under the same ``TaskPolicy`` and controller state.
Unknown tools are denied, never passed as ``control``.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from codey.operations.browser.browser_copy import CopyTurn
from codey.operations.browser.browser_references import BrowserReferences
from codey.operations.kernel_contracts import ToolExecutor
from codey.operations.kernel_tool_args import (
    _canonical_name,
    _controller_allows,
    _disallowed_plan,
    _invalid_plan,
    _policy_allows,
    _tool_and_args,
    _validate_tool_args,
)
from codey.operations.project.edit_base import (
    bound_symbol_retry,
)
from codey.protocols.text_turn import DecodedTextTurn, TextAnswer, TextError, TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.runtime.core.models import Control, ToolCall, ToolPlan
from codey.toolchain.tool_spec import (
    ToolSpec,
    freeze_spec_parameters,
    is_text_batch_tool,
)
from codey.types.json_value import JsonValue, ToolArgs, decode_json_object
from codey.types.protocol_window import ProtocolWindowRef

MAX_NATIVE_CALLS_PER_TURN = 8

if TYPE_CHECKING:
    from codey.operations.project.project_instructions import ProjectInstructionBinding
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.protocols.json_repair import JsonTailClosure
    from codey.research.answer_contract import AnswerScope


@dataclass(frozen=True)
class ModelInputBinding:
    """The captured model input; conversion still targets the original executor."""

    kind: Literal["upload", "browser_copy", "declared_fields"]
    model_spec: ToolSpec
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_spec", freeze_spec_parameters(self.model_spec))


@dataclass(frozen=True)
class TurnSnapshot:
    """One authoritative per-round observation: policy ∩ current facts.

    Built once at round start; any build failure raises and the kernel
    terminates the turn as controller_failure (fail-closed), never a stale
    fallback list. Carries the allowed tools, frozen tool definitions plus
    both protocols' contracts for the round. The snapshot object贯穿发送、
    解析与执行：注册表变化下一轮生效，本轮解析与执行共用同一冻结定义。
    ``frozen_specs`` are nested-immutable (mapping proxies inside); the
    ``custom_executors`` table freezes third-task bindings so a mid-turn
    registry swap cannot reroute this turn's execution.
    """

    allowed: tuple[str, ...] | None
    contract_text: str
    tool_names: tuple[str, ...] = ()
    policy: TaskPolicy | None = None
    frozen_specs: tuple[ToolSpec, ...] = ()
    custom_executors: tuple[tuple[str, ToolExecutor | None], ...] = ()
    implementation_compatible: bool = True
    nested_tool_names: tuple[str, ...] = ()
    research_scope: AnswerScope | None = None
    ignore_runtime_claims: bool = False
    browser_copy: CopyTurn | None = None
    input_binding: ModelInputBinding | None = None
    task_navigation_url: str | None = None
    task_source_url: str | None = None
    browser_followups: bool = False
    browser_fill_text: str | None = None
    browser_references: BrowserReferences | None = None
    project_instructions: tuple[ProjectInstructionBinding, ...] = ()
    navigation_verifies: bool = False
    data_file_reads: tuple[str, ...] | None = None
    symbol_read_refs: tuple[tuple[str, str], ...] = ()
    captured_model_specs: tuple[ToolSpec, ...] | None = None

    @property
    def navigation_observes(self) -> bool:
        return self.browser_followups and "browser_snapshot" in self.nested_tool_names

    @property
    def inventory_names(self) -> tuple[str, ...]:
        names: dict[str, None] = dict.fromkeys((*self.tool_names, *self.nested_tool_names))
        return tuple(names)

    @property
    def has_project_tools(self) -> bool:
        return "codemode" in self.tool_names and bool({"edit", "read_file"}.intersection(self.tool_names))

    @property
    def model_specs(self) -> tuple[ToolSpec, ...]:
        if self.captured_model_specs is not None:
            return self.captured_model_specs
        from codey.operations.kernel.turn_presentation import model_tool_specs

        return model_tool_specs(self)


def render_snapshot_contract(snapshot: TurnSnapshot) -> str:
    from codey.toolchain.tool_prompt import json_contract_text

    text = json_contract_text(snapshot.model_specs)
    if snapshot.browser_copy is not None and snapshot.browser_copy.phase == "question":
        text += "\nAsk which webpage source to use. The observation has no unique supported data format."
    return text


def refresh_snapshot_contract(snapshot: TurnSnapshot, *, compact_catalog: bool = False,
                              include_json_edit: bool = False) -> TurnSnapshot:
    snapshot = replace(snapshot, captured_model_specs=None)
    if snapshot.input_binding is not None and snapshot.input_binding.model_spec.name not in snapshot.tool_names:
        snapshot = replace(snapshot, input_binding=None)
    order = {name: index for index, name in enumerate(snapshot.inventory_names)}

    def spec_order(spec: ToolSpec) -> int:
        return order.get(spec.name, len(order))

    def executor_order(item: tuple[str, ToolExecutor | None]) -> int:
        return order.get(item[0], len(order))

    snapshot = replace(snapshot,
        frozen_specs=tuple(sorted(snapshot.frozen_specs, key=spec_order)),
        custom_executors=tuple(sorted(snapshot.custom_executors, key=executor_order)))
    if "codemode" in snapshot.tool_names and (
            snapshot.input_binding is None or snapshot.input_binding.model_spec.name != "codemode"):
        from codey.codemode.catalog import render_declarations, snapshot_catalog

        specs = snapshot.model_specs
        declarations = render_declarations(snapshot_catalog(snapshot),
            outer_descriptions={spec.name: spec.description for spec in specs}, outer_specs=specs,
            detail_budget_bytes=2_400 if compact_catalog else None, detail_names=("edit",),
            include_json_edit=include_json_edit)
        snapshot = replace(snapshot, frozen_specs=tuple(
            replace(spec, declarations=declarations) if spec.name == "codemode" else spec for spec in snapshot.frozen_specs))
    snapshot = replace(snapshot, captured_model_specs=snapshot.model_specs)
    return replace(snapshot, contract_text=render_snapshot_contract(snapshot))


@dataclass(frozen=True)
class ReceivedProviderTurn:
    """A received response with its original request's declarations and turn."""

    reply: AssistantTurn | str
    snapshot: TurnSnapshot
    prompt: str
    owner_session: TaskSession
    logical_turn: int = 1
    window_ref: ProtocolWindowRef | None = None
    effect_scope: str = ""


def frozen_spec_map(snapshot: TurnSnapshot | None) -> dict[str, ToolSpec] | None:
    if snapshot is None:
        return None
    mapping = {spec.name: spec for spec in snapshot.frozen_specs}
    if set(mapping) != set(snapshot.inventory_names):
        raise ValueError("incomplete tool snapshot definitions")
    return mapping


def frozen_custom_executor_map(snapshot: TurnSnapshot | None) -> dict[str, ToolExecutor | None] | None:
    if snapshot is None:
        return None
    mapping = dict(snapshot.custom_executors)
    if set(mapping) != set(snapshot.inventory_names):
        raise ValueError("incomplete tool snapshot executor bindings")
    return mapping


def _expand_text_batch(tool: str, args: ToolArgs) -> tuple[list[tuple[str, ToolArgs, str]], str]:
    """Lower bounded read-only wrappers; validate the whole batch before effects."""
    from codey.toolchain.definition import MAX_PARALLEL_CALLS, TOOL_DEFINITION_BY_NAME

    if tool == "read_files":
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths or len(paths) > MAX_NATIVE_CALLS_PER_TURN:
            return [], f"read_files requires 1..{MAX_NATIVE_CALLS_PER_TURN} paths"
        if any(not isinstance(path, str) or not path.strip() for path in paths):
            return [], "read_files paths must be non-empty strings"
        return [("read_file", {"path": path}, "") for path in paths], ""
    calls = args.get("calls")
    if not isinstance(calls, list) or not calls or len(calls) > MAX_PARALLEL_CALLS:
        return [], f"parallel requires 1..{MAX_PARALLEL_CALLS} read-only calls"
    expanded = []
    for row in calls:
        if not isinstance(row, dict):
            return [], "every parallel call must be an object"
        name, child_args = _tool_and_args(row)
        definition = TOOL_DEFINITION_BY_NAME.get(name)
        if definition is None or not definition.parallel_safe:
            return [], "parallel accepts only list_dir, read_file, and grep"
        if "args" in row and not isinstance(row["args"], dict):
            return [], f"{name} args must be an object"
        expanded.append((name, child_args, ""))
    return expanded, ""


def _lower_text_batches(items: list[tuple[str, ToolArgs, str]]) -> tuple[list[tuple[str, ToolArgs, str]], str]:
    expanded = []
    for raw_tool, args, call_id in items:
        name = _canonical_name(raw_tool)
        if is_text_batch_tool(name):
            if call_id:
                return [], "batch wrappers are text-only; native calls require individual ids"
            from codey.toolchain.tool_spec import validate_args_against_spec

            error = validate_args_against_spec(name, args)
            if error:
                return [], error
            children, error = _expand_text_batch(name, args)
            if error:
                return [], error
            expanded.extend(children)
        else:
            expanded.append((raw_tool, args, call_id))
    if len(expanded) > MAX_NATIVE_CALLS_PER_TURN:
        return [], "expanded batch exceeds the turn call limit"
    return expanded, ""


def _applicable_done_declarations(args: ToolArgs, *, runtime_owned: bool, spec: ToolSpec | None) -> ToolArgs:
    ignored = {"source_gaps", "execution_claims"} if runtime_owned else set()
    if spec is not None and "memory_candidates" in dict(spec.parameters):
        # Optional observational metadata is validated after accepted delivery;
        # malformed metadata cannot cause execution/protocol retries.
        ignored.add("memory_candidates")
    # Ordinary completion still validates execution claims. Source-gap metadata
    # is inapplicable there; use the captured contract, never the live registry.
    if spec is not None and "execution_claims" in dict(spec.parameters) and "source_gaps" not in dict(spec.parameters):
        ignored.add("source_gaps")
    return {key: value for key, value in args.items() if key not in ignored}


def _control_text(tool: str, validated: ToolArgs, policy: TaskPolicy | None) -> tuple[str, str]:
    from codey.policies.output_requirement import explanation_proposal, has_explanation

    text = str(validated.get("summary" if tool == "done" else "question") or "")
    if tool != "done" or policy is None or not has_explanation(policy.required_checks):
        return text.strip(), ""
    try:
        return explanation_proposal(validated), ""
    except ValueError as exc:
        return "", str(exc)


def _plan_from_tool_objects(
    items: list[tuple[str, ToolArgs, str]],
    *,
    policy: TaskPolicy | None,
    controller_allowed: set[str] | None,
    snapshot_names: tuple[str, ...] | None = None,
    frozen_specs: Mapping[str, ToolSpec] | None = None,
    ignore_runtime_claims: bool = False,
) -> ToolPlan:
    items, batch_error = _lower_text_batches(items)
    if batch_error:
        return _invalid_plan(batch_error)
    if sum(1 for raw_tool, _args, _call_id in items if _canonical_name(raw_tool) == "shell") and len(items) != 1:
        return _invalid_plan("shell must be the only tool call in a turn", kind="mixed_shell_batch", tool="shell")
    if len(items) != 1 and any(_canonical_name(name) in {"submit_research_review", "submit_research_answer"} for name, _, _ in items):
        return _invalid_plan("research submission must be the only tool call in a turn")
    calls: list[ToolCall] = []
    text_keys: set[str] = set()
    for raw_tool, args, call_id in items:
        tool = _canonical_name(raw_tool)
        if not tool:
            return _invalid_plan(f"unknown tool: {raw_tool or '?'}", kind="unknown_tool", tool=(raw_tool or ""))
        if snapshot_names is not None and tool not in snapshot_names:
            return _invalid_plan(
                f"{tool} is not allowed by the current controller state (not in the turn snapshot: {tool})",
                kind="snapshot_tool_mismatch",
                tool=tool,
            )
        if tool in {"done", "ask_user"}:
            if len(items) != 1:
                return _invalid_plan(f"{tool} must be the only call in a turn", tool=tool, kind="too_many_tools")
            if not _policy_allows(policy, tool, frozen_specs):
                return _disallowed_plan(tool)
            if not _controller_allows(tool, controller_allowed):
                return _disallowed_plan(tool, controller=True)
            if tool == "done":
                args = _applicable_done_declarations(args, runtime_owned=ignore_runtime_claims,
                    spec=frozen_specs.get(tool) if frozen_specs is not None else None)
            validated, error = _validate_tool_args(tool, args, frozen_specs=frozen_specs)
            if error:
                return _invalid_plan(error, tool=tool)
            text, error = _control_text(tool, validated, policy)
            if error:
                return _invalid_plan(error, tool=tool)
            return ToolPlan(calls=[], control=Control(kind=tool, body=text), control_args=validated)
        if not _policy_allows(policy, tool, frozen_specs):
            return _disallowed_plan(tool)
        if not _controller_allows(tool, controller_allowed):
            return _disallowed_plan(tool, controller=True)
        validated, error = _validate_tool_args(tool, args, frozen_specs=frozen_specs)
        if error:
            return _invalid_plan(error, tool=tool)
        if not call_id:
            key_payload: list[JsonValue] = [tool, validated]
            key = json.dumps(key_payload, sort_keys=True, ensure_ascii=False)
            if key in text_keys:
                continue
            text_keys.add(key)
        calls.append(ToolCall(name=tool, args=validated, call_id=(call_id or "")))
    if not calls:
        return _invalid_plan("no JSON tool call found", kind="no_json")
    body = "Need tool result" if len(calls) == 1 else "Need tool results"
    return ToolPlan(calls=calls, control=Control(kind="continue", body=body))


def _native_tool_objects(
    calls: Sequence[ProviderToolCall],
) -> list[tuple[str, ToolArgs, str]] | ToolPlan:
    """Unwrap native calls without losing or inventing provider call ids."""
    if len(calls) > MAX_NATIVE_CALLS_PER_TURN:
        return _invalid_plan(
            f"too many native tool calls in one turn ({len(calls)}); send at most {MAX_NATIVE_CALLS_PER_TURN}",
            kind="too_many_tools",
        )
    missing = [str(c.name or "?") for c in calls if not str(c.id or "")]
    if missing:
        return _invalid_plan(
            "native tool call without an id cannot be answered: " + ", ".join(missing),
            kind="invalid_args",
        )
    ids = [call.id for call in calls]
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in ids):
        return _invalid_plan("native tool call ids must be nonempty strings of at most 256 characters", kind="invalid_args")
    if len(ids) != len(set(ids)):
        return _invalid_plan("duplicate native tool call ids cannot be answered unambiguously", kind="invalid_args")
    names = [str(c.name or "").strip().lower() for c in calls]
    if "done" in names and len(calls) != 1:
        return _invalid_plan("done must be the only call in a turn", tool="done", kind="too_many_tools")
    items = []
    for item in calls:
        name = str(item.name or "").strip().lower()
        call_id = str(item.id or "")
        try:
            args = decode_json_object(item.arguments)
        except ValueError:
            return _invalid_plan(f"{name} args must be an object", tool=name)
        items.append((name, dict(args), call_id))
    return items


def _decoded_text_objects(
    decoded: TextRequests | TextAnswer | TextError,
) -> list[tuple[str, ToolArgs, str]] | ToolPlan:
    if isinstance(decoded, TextError):
        return _invalid_plan(decoded.message, tool=decoded.tool, kind=decoded.kind)
    if isinstance(decoded, TextAnswer):
        return ToolPlan(calls=[], control=Control(kind="done", body=decoded.text))
    return [(item.tool, dict(item.args), "") for item in decoded.items]


def _response_objects(reply: object) -> list[tuple[str, ToolArgs, str]] | ToolPlan:
    if isinstance(reply, (TextRequests, TextAnswer, TextError)):
        return _decoded_text_objects(reply)
    if isinstance(reply, AssistantTurn):
        if reply.finish is TurnFinish.PROTOCOL_ERROR:
            detail = reply.raw.get("protocol_error")
            tool = reply.raw.get("protocol_tool_name")
            return _invalid_plan(detail[:500] if isinstance(detail, str) and detail else "provider returned malformed tool calls",
                                 tool=tool[:80] if isinstance(tool, str) else "", kind="invalid_args")
        if reply.finish is not TurnFinish.COMPLETE:
            return _invalid_plan("assistant response was truncated", kind="output_limit")
        if not reply.tool_calls:
            if reply.raw.get("text_protocol") is True:
                return _decoded_text_objects(decode_text_turn(reply.text))
            if not reply.text.strip():
                return _invalid_plan("empty assistant answer", kind="empty_answer")
            return ToolPlan(calls=[], control=Control(kind="done", body=reply.text.strip()))
    if isinstance(reply, str):
        return _invalid_plan(
            "raw text must be decoded by the text protocol before entering the kernel",
            kind="raw_text",
        )
    if isinstance(reply, AssistantTurn) and reply.tool_calls:
        return _native_tool_objects(reply.tool_calls)
    return _invalid_plan("unsupported provider reply type", kind="invalid_args")


def _closed_done_request(reply: DecodedTextTurn | AssistantTurn) -> tuple[JsonTailClosure, DecodedTextTurn | AssistantTurn] | None:
    from codey.protocols.json_repair import close_json_tail
    from codey.types.json_value import parse_json_object

    if isinstance(reply, TextError) and reply.tail_closure is not None:
        closure = reply.tail_closure
        value = parse_json_object(closure.candidate)
        if set(value) == {"tool", "args"} and value["tool"] == "done" and isinstance(value["args"], dict):
            return closure, decode_text_turn(closure.candidate)
    if not isinstance(reply, AssistantTurn):
        return None
    if reply.finish is TurnFinish.COMPLETE and reply.raw.get("text_protocol") is True and not reply.tool_calls:
        return _closed_done_request(decode_text_turn(reply.text))
    raw_calls = reply.raw.get("malformed_native_calls")
    if reply.finish is not TurnFinish.PROTOCOL_ERROR or reply.tool_calls or not isinstance(raw_calls, list) or len(raw_calls) != 1:
        return None
    try:
        call = decode_json_object(raw_calls[0])
        function = decode_json_object(call.get("function"))
    except ValueError:
        return None
    identity, name, arguments = call.get("id"), function.get("name"), function.get("arguments")
    if (call.get("type") != "function" or name != "done" or not isinstance(identity, str)
            or not identity.strip() or len(identity) > 256 or not isinstance(arguments, str)):
        return None
    native_closure = close_json_tail(arguments)
    if native_closure is None:
        return None
    return native_closure, AssistantTurn(tool_calls=(ProviderToolCall(identity, "done", parse_json_object(native_closure.candidate)),))


def _try_close_done(reply: DecodedTextTurn | AssistantTurn, snapshot: TurnSnapshot | None) -> ToolPlan | None:
    if snapshot is None:
        return None
    done = next((spec for spec in snapshot.model_specs if spec.name == "done"), None)
    if done is None or done.required != ("paragraphs",) or set(dict(done.parameters)) != {"paragraphs"}:
        return None
    closed = _closed_done_request(reply)
    if closed is None:
        return None
    closure, candidate = closed
    plan = normalize_turn(candidate, snapshot=snapshot)
    if plan.protocol_error or plan.calls or plan.control is None or plan.control.kind != "done":
        return None
    return replace(plan, syntax_closure=closure, arg_repair_counts={**plan.arg_repair_counts, "syntax_closed": 1})


def _validate_control_plan(
    items: ToolPlan, snapshot: TurnSnapshot | None, frozen: Mapping[str, ToolSpec] | None,
) -> ToolPlan:
    if (snapshot is not None and bound_symbol_retry(snapshot) is not None
            and items.control is not None and not items.protocol_error):
        return _invalid_plan("The bound edit requires a complete Python definition.", tool="edit")
    if (snapshot is not None and snapshot.browser_copy is not None and items.control is not None
            and items.control.kind == "done" and "done" not in snapshot.tool_names):
        return _invalid_plan("Finish the browser copy before reporting completion.", tool="done")
    done_spec = frozen.get("done") if frozen is not None else None
    if (items.control is not None and items.control.kind == "done" and not items.protocol_error
            and done_spec is not None and "report_sections" in done_spec.required):
        return _invalid_plan("Research completion requires done.report_sections.coverage under the frozen contract; "
                             "plain text and summary-only reports are not accepted.", tool="done")
    if (items.control is not None and items.control.kind == "done" and not items.protocol_error
            and done_spec is not None and "paragraphs" in done_spec.required):
        return _invalid_plan("Explanation completion requires done.paragraphs under the frozen contract; "
                             "plain text is not accepted.", tool="done")
    return items


def normalize_turn(
    reply: DecodedTextTurn | AssistantTurn,
    *,
    policy: TaskPolicy | None = None,
    controller_allowed: Collection[str] | None = None,
    snapshot_names: tuple[str, ...] | None = None,
    snapshot: TurnSnapshot | None = None,
) -> ToolPlan:
    frozen: dict[str, ToolSpec] | None = None
    if snapshot is not None:
        try:
            if policy is not None and policy != snapshot.policy:
                return _invalid_plan("turn policy differs from the captured authorization")
            policy = snapshot.policy
            allowed_raw = snapshot.allowed
            allowed = None if allowed_raw is None else {str(n or "").strip().lower() for n in allowed_raw}
            snapshot_names = snapshot.tool_names
            frozen = frozen_spec_map(snapshot)
        except Exception as exc:
            return _invalid_plan(f"invalid tool snapshot: {exc}")
    else:
        allowed = None if controller_allowed is None else {(n or "").strip().lower() for n in controller_allowed}
    from codey.operations.kernel.turn_inputs import bound_symbol_source, lower_bound_inputs

    source = bound_symbol_source(reply, snapshot) if snapshot is not None else ""
    items: list[tuple[str, ToolArgs, str]] | ToolPlan
    items = [("edit", {"content": source}, "")] if source else _response_objects(reply)
    if isinstance(items, ToolPlan):
        if items.protocol_error and (closed := _try_close_done(reply, snapshot)) is not None:
            return closed
        return _validate_control_plan(items, snapshot, frozen)
    if snapshot is not None and snapshot.browser_copy is not None and len(items) != 1:
        return _invalid_plan("Submit one browser copy action per turn.")
    if snapshot is not None:
        lowered = lower_bound_inputs(items, snapshot)
        if isinstance(lowered, ToolPlan):
            return lowered
        items = lowered
    plan = _plan_from_tool_objects(
        items, policy=policy, controller_allowed=allowed,
        snapshot_names=snapshot_names, frozen_specs=frozen,
        ignore_runtime_claims=snapshot.ignore_runtime_claims if snapshot is not None else False,
    )
    if snapshot is not None and any(call.name == "codemode" and not call.args.get("repair_of") for call in plan.calls):
        from codey.codemode.catalog import snapshot_catalog
        from codey.codemode.shape_repair import preflight_plan

        return preflight_plan(plan, snapshot_catalog(snapshot),
                              file_reads=snapshot.data_file_reads)
    return plan


__all__ = [
    "MAX_NATIVE_CALLS_PER_TURN",
    "TurnSnapshot",
    "frozen_custom_executor_map",
    "frozen_spec_map",
    "normalize_turn",
]
