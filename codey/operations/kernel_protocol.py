"""Single turn protocol for the unified kernel: JSON text and native calls.

Web JSON replies and native ``AssistantTurn`` values converge here into one
canonical ``ToolPlan`` under the same ``TaskPolicy`` and controller state.
Unknown tools are denied, never passed as ``control``.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol

from codey.operations.browser_copy import CopyTurn, copy_target, copy_turn
from codey.operations.browser_upload import (
    STRUCTURED_UPLOAD_TOOLS,
    lower_upload_action,
    structured_upload_spec,
    task_upload_scope,
    upload_model_spec,
    upload_phase,
)
from codey.operations.kernel_contracts import ToolExecutor
from codey.protocols.text_turn import DecodedTextTurn, TextAnswer, TextError, TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import Control, ToolCall, ToolPlan, ToolResult
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.toolchain.tool_spec import _CONTROLLER_ALIAS_ID_ARG as _ALIAS_ARGS
from codey.toolchain.tool_spec import (
    SOURCE_READ_TOOL_NAMES,
    ToolSpec,
    freeze_spec_parameters,
    is_text_batch_tool,
    validate_args_with_spec,
)
from codey.types.json_value import JsonValue, ToolArgs, decode_json_object

MAX_NATIVE_CALLS_PER_TURN = 8

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.answer_contract import AnswerScope


class KernelSessionView(Protocol):
    @property
    def executed(self) -> Mapping[str, ExecutionRecord]: ...

    @property
    def research_acquisition_limits(self) -> tuple[int, int] | None: ...

    @property
    def _memory_results(self) -> Mapping[str, ToolResult]: ...

    @property
    def policy(self) -> TaskPolicy: ...

    @property
    def task_kind(self) -> str: ...

    @property
    def task_text(self) -> str: ...

    @property
    def required_source_urls(self) -> frozenset[str]: ...

    @property
    def controller_denied(self) -> tuple[str, ...]: ...

    @property
    def model_tool_names(self) -> tuple[str, ...] | None: ...

    @property
    def research_questions(self) -> tuple[str, ...]: ...

    @property
    def search_results(self) -> Mapping[str, str]: ...

    @property
    def opened_sources(self) -> Collection[str]: ...

    @property
    def source_ids(self) -> Mapping[str, str]: ...

    @property
    def hit_targets(self) -> Mapping[str, object]: ...

    @property
    def evidence(self) -> Sequence[object]: ...


@dataclass(frozen=True)
class ModelInputBinding:
    """The captured model input; conversion still targets the original executor."""

    kind: Literal["upload", "browser_copy"]
    model_spec: ToolSpec

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

    @property
    def inventory_names(self) -> tuple[str, ...]:
        names: dict[str, None] = dict.fromkeys((*self.tool_names, *self.nested_tool_names))
        return tuple(names)

    @property
    def model_specs(self) -> tuple[ToolSpec, ...]:
        binding = self.input_binding
        return tuple(binding.model_spec if binding is not None and spec.name == binding.model_spec.name else spec
                     for spec in self.frozen_specs if spec.name in self.tool_names)


def render_snapshot_contract(snapshot: TurnSnapshot) -> str:
    from codey.toolchain.tool_prompt import json_contract_text

    text = json_contract_text(snapshot.model_specs)
    if snapshot.browser_copy is not None and snapshot.browser_copy.phase == "question":
        text += "\nAsk which webpage source to use. The observation has no unique supported data format."
    return text


def refresh_snapshot_contract(snapshot: TurnSnapshot) -> TurnSnapshot:
    if snapshot.input_binding is not None and snapshot.input_binding.model_spec.name not in snapshot.tool_names:
        snapshot = replace(snapshot, input_binding=None)
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


def controller_allowed_for_session(session: KernelSessionView) -> tuple[str, ...] | None:
    if session.task_kind == "research_review":
        raise ValueError("same-model research assessment execution has been retired")
    policy = session.policy
    try:
        denied = {"submit_research_review"} | {str(n or "").strip().lower() for n in (session.controller_denied or ())
                  if str(n or "").strip()}
    except Exception:
        denied = {"submit_research_review"}
    observed_aliases = {"open_result": bool(session.search_results), "reopen_source": bool(session.source_ids),
                        "open_hit": bool(session.hit_targets)}
    if not policy.strict_research:
        denied.add("submit_research_answer")
        from codey.research.questions import requires_question_ref

        question_binding = requires_question_ref(session)
        if not question_binding:
            denied.add("research_plan")
        base: tuple[str, ...] | None = None
        if question_binding and not session.research_questions:
            base = ("research_plan", "done")
    else:
        from codey.research.answer_contract import ANSWER_TOOLS, whole_research

        if whole_research(session):
            from codey.toolchain.tool_spec import tool_specs

            # The answer contract stays unchanged. Explicitly authorized
            # operation resources compose with it in this one turn planner.
            browser = tuple(spec.name for spec in tool_specs().values()
                            if spec.executor == "browser" and policy.allows(spec.grant))
            tools: tuple[str, ...] = (*ANSWER_TOOLS, *browser, *(("codemode", "read_tool_result") if browser else ()))
            return tuple(name for name in tools if name not in denied and (
                name not in observed_aliases or observed_aliases[name]))
        # Other strict callers retain their scoped acquisition contract.
        # Whole-answer submission requires the Research task's frozen scope.
        denied.update({"knowledge_write", "knowledge_commit", "knowledge_link", "submit_research_answer"})
        results = dict(session.search_results or {})
        opened = set(session.opened_sources or ())
        evidence = list(session.evidence or ())
        if not session.research_questions:
            base = ("research_plan", "done")
        elif not results and not opened:
            base = ("knowledge_search", "knowledge_read", "web_search", "open_url", "save_inference", "done")
        elif not opened:
            base = ("knowledge_search", "knowledge_read", "web_search", "open_url", "open_result", "save_inference", "done")
        elif not evidence:
            base = (
                "knowledge_search",
                "knowledge_read",
                "web_search",
                "open_url",
                "open_result",
                "reopen_source",
                "open_hit",
                "source_search",
                "save_finding",
                "save_inference",
                "knowledge_write",
                "done",
            )
        else:
            base = None
        if base is not None and "research_plan" not in base:
            base = ("research_plan", *base)
    if base is not None:
        base = (*base, "ask_user", "read_tool_result")
    # Reference aliases require an observed target, in ordinary and strict
    # tasks alike. The resulting list drives both wire protocols and execution.
    for alias, observed in observed_aliases.items():
        if not observed:
            denied.add(alias)
    if not denied:
        return base
    if base is None:
        # 否决表存在时 unlimited 即不可用：按策略可见全集减去否决，
        # 收窄仍经同一快照进入提示、schema、解析与执行。
        from codey.toolchain.tool_spec import visible_tool_names_for_snapshot

        base = tuple(visible_tool_names_for_snapshot(policy, None))
    return tuple(name for name in base if name not in denied)


def _frozen_specs_for_names(names: tuple[str, ...]) -> tuple[ToolSpec, ...]:
    """Capture immutable definitions for exactly the advertised names."""
    import copy as _copy

    try:
        from codey.toolchain.tool_spec import freeze_spec_parameters as _freeze
        from codey.toolchain.tool_spec import tool_specs as _all_specs
    except Exception as exc:
        raise RuntimeError(f"tool snapshot unavailable: {exc}") from exc
    specs = _all_specs()
    frozen: list[ToolSpec] = []
    for name in names:
        spec = specs.get(name)
        if spec is None:
            continue
        try:
            copied = _copy.deepcopy(spec)
        except Exception as exc:
            raise RuntimeError(f"tool snapshot freeze failed for {name}: {exc}") from exc
        try:
            frozen.append(_freeze(copied))
        except Exception as exc:
            raise RuntimeError(f"tool snapshot freeze failed for {name}: {exc}") from exc
    return tuple(frozen)


def _frozen_custom_executors(names: tuple[str, ...]) -> tuple[tuple[str, ToolExecutor | None], ...]:
    """Freeze third-task executor bindings for this turn (same source)."""
    try:
        from codey.toolchain.tool_spec import custom_executor_for as _fn_for
    except Exception as exc:
        raise RuntimeError(f"tool snapshot executors unavailable: {exc}") from exc
    bindings: list[tuple[str, ToolExecutor | None]] = []
    for name in names:
        try:
            fn = _fn_for(name)
        except Exception as exc:
            raise RuntimeError(f"tool snapshot executor freeze failed for {name}: {exc}") from exc
        if fn is not None and not callable(fn):
            raise RuntimeError(f"tool snapshot executor is not callable for {name}")
        bindings.append((name, fn))
    return tuple(bindings)


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


def build_turn_snapshot(session: KernelSessionView, *, native: bool = False) -> TurnSnapshot:
    """Build the round's authoritative snapshot; raises on failure."""
    # Fail closed: a snapshot computation failure never encodes as None
    # (unlimited). Callers must treat the exception as a per-turn config
    # error and stop.
    from codey.research.answer_contract import AnswerScope, answer_scope, answer_spec, whole_research

    whole = whole_research(session)
    allowed = controller_allowed_for_session(session)
    scope = answer_scope(session) if whole else None
    from codey.toolchain.tool_spec import visible_tool_names_for_snapshot

    authorized = tuple(visible_tool_names_for_snapshot(session.policy, allowed, native=native))
    if scope is not None:
        authorized = tuple(name for name in authorized if name in (allowed or ()))
    exposure = session.model_tool_names
    if exposure is not None and (len(set(exposure)) != len(exposure) or not set(exposure) <= set(authorized)):
        raise ValueError("model exposure must narrow the authorized tool inventory")
    names = tuple(name for name in authorized if exposure is None or name in exposure)
    frozen = _frozen_specs_for_names(authorized)
    from codey.research.tool_contract import done_spec_for_purpose, question_bound_spec

    purpose = ("evidence" if session.task_kind == "evidence_followup" else
               "report" if session.policy.strict_research else "chat" if session.task_kind == "chat" else "ordinary")
    frozen = tuple(done_spec_for_purpose(spec, purpose, task=session.task_text,
                                        questions=session.research_questions) if spec.name == "done" else spec
                   for spec in frozen)
    if purpose == "chat":
        from codey.operations.chat_knowledge import chat_knowledge_spec, read_knowledge_versions

        versions = read_knowledge_versions(session.executed, session._memory_results)
        frozen = tuple(chat_knowledge_spec(spec, versions) if spec.name == "done" else spec for spec in frozen)
    from codey.policies.output_requirement import has_explanation

    if has_explanation(session.policy.required_checks):
        frozen = tuple(freeze_spec_parameters(replace(spec, parameters=tuple(
            (key, {"type": "string", "minLength": 0}) if key == "summary" else (key, value)
            for key, value in spec.parameters))) if spec.name == "done" else spec for spec in frozen)
    from codey.research.questions import requires_question_ref

    if requires_question_ref(session):
        question_refs = tuple(f"q{index}" for index in range(1, len(session.research_questions) + 1))
        frozen = tuple(question_bound_spec(spec, question_refs=question_refs) for spec in frozen)
    if isinstance(scope, AnswerScope):
        frozen = tuple(answer_spec(spec, scope) for spec in frozen)
    nested: tuple[str, ...] = ()
    binding = None
    if "codemode" in names:
        from codey.codemode.catalog import build_codemode_catalog, render_declarations

        aggregate_browser = any(spec.executor == "browser" for spec in frozen)
        # Browser aggregation keeps the real project edit action in the same
        # CodeMode surface as browser/read leaves. Returning modified data is
        # still not a write; only the nested kernel edit receipt can satisfy
        # project completion. Project verification remains outside this
        # composable surface.
        catalog = build_codemode_catalog(frozen,
            allow_writes=session.policy.allows("project.write"),
            allow_project_run=session.policy.allows("project.write") and not aggregate_browser)
        nested = tuple(entry.name for entry in catalog.entries)
        if aggregate_browser:
            # One declaration per leaf: browser-enabled tasks use the existing
            # CodeMode entry for composable operations, keeping controls and
            # answer submission on the outer surface. Inventory stays frozen.
            # Text batches lower to the project read leaves. Once those leaves
            # are nested-only, their old outer wrappers would advertise calls
            # that fail this same snapshot's parser/execution checks.
            names = tuple(name for name in names if name not in nested and not is_text_batch_tool(name))
        declarations = render_declarations(catalog)
        frozen = tuple(replace(spec, declarations=declarations)
                       if spec.name == "codemode" else spec for spec in frozen)
        upload = task_upload_scope(session.task_kind, session.task_text, session.required_source_urls, session.policy)
        if upload is not None and set(nested) >= STRUCTURED_UPLOAD_TOOLS:
            phase = upload_phase(session._memory_results)
            binding = ModelInputBinding("upload", upload_model_spec(structured_upload_spec(
                next(spec for spec in frozen if spec.name == "codemode"), upload, phase)))
            frozen = tuple(structured_upload_spec(spec, upload, phase) if spec.name != "codemode" else spec
                           for spec in frozen)
    browser_copy = None
    copy_tools = {"browser_navigate", "browser_eval", "read_file", "edit"}
    if (not session.policy.strict_research and "codemode" in names and "done" in names
            and copy_tools <= set(nested) and (target := copy_target(session.task_text)) is not None):
        browser_copy = copy_turn(target, session.task_text, session._memory_results)
        if browser_copy.phase == "done":
            names, nested = ("done",), ()
        elif browser_copy.phase == "question" and "ask_user" in names:
            names, nested = ("ask_user",), ()
        else:
            names = ("codemode", "ask_user") if browser_copy.phase == "edit" and "ask_user" in names else ("codemode",)
            nested = tuple(name for name in nested if name in copy_tools or name == "open_url")
            frozen = tuple(freeze_spec_parameters(replace(spec, parameters=(
                ("path", {"type": "string", "enum": [target.path]}),
                ("base_result_ref", {"type": "string", "minLength": 1}),
                ("browser_source", {"type": "object", "properties": {
                    "ref": {"type": "string", "minLength": 1}, "selector": {"type": "object"}},
                    "required": ["ref", "selector"], "additionalProperties": False}),
            ), required=("path", "base_result_ref", "browser_source"))) if spec.name == "edit" else spec for spec in frozen)
            if browser_copy.phase == "question":
                browser_copy = CopyTurn(target)
    # JSON 与 native 契约同源：都从同一冻结定义生成，不再查实时注册表。
    inventory_names: dict[str, None] = dict.fromkeys((*names, *nested))
    inventory = tuple(inventory_names)
    by_name = {spec.name: spec for spec in frozen}
    frozen = tuple(by_name[name] for name in inventory)
    if browser_copy is not None and "codemode" in names:
        binding = ModelInputBinding("browser_copy", browser_copy.spec(by_name["codemode"]))
    executors = _frozen_custom_executors(inventory)
    return refresh_snapshot_contract(TurnSnapshot(
        allowed=allowed,
        contract_text="",
        tool_names=names,
        policy=session.policy,
        frozen_specs=frozen,
        custom_executors=executors,
        nested_tool_names=nested,
        research_scope=scope,
        ignore_runtime_claims=purpose == "chat",
        browser_copy=browser_copy,
        input_binding=binding,
    ))


_CONTROLLER_ALIASES = frozenset(_ALIAS_ARGS)


def _grant_for_tool(tool: str) -> str:
    try:
        from codey.toolchain.tool_spec import spec_for_tool
    except Exception:
        return ""
    try:
        spec = spec_for_tool(tool)
    except Exception:
        return ""
    if spec is None or not spec.grant:
        # Unknown tools are denied, never passed as control.
        return ""
    return spec.grant


def _canonical_name(tool: str) -> str:
    try:
        from codey.toolchain.tool_spec import canonical_tool_name
    except Exception:
        return (tool or "").strip().lower()
    try:
        name = canonical_tool_name(tool)
        return name
    except Exception:
        return (tool or "").strip().lower()


def _is_coding_only_tool(tool: str) -> bool:
    name = _canonical_name(tool)
    if not name or name == "done":
        return False
    try:
        from codey.toolchain.tool_spec import spec_for_tool
    except Exception:
        return False
    try:
        spec = spec_for_tool(name)
    except Exception:
        return False
    return spec is not None and spec.executor == "project"


def _controller_allows(tool: str, allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    name = (tool or "").strip().lower()
    if _is_coding_only_tool(name):
        return True
    if name == "open_url":
        return bool(SOURCE_READ_TOOL_NAMES & allowed)
    return name in allowed


def _policy_allows(policy: TaskPolicy | None, tool: str, frozen_specs: Mapping[str, ToolSpec] | None = None) -> bool:
    if policy is None:
        return False
    try:
        spec = frozen_specs.get(tool) if frozen_specs is not None else None
        grant = spec.grant if spec is not None else "" if frozen_specs is not None else _grant_for_tool(tool)
        return bool(grant and policy.allows(grant))
    except Exception:
        return False


def _disallowed_plan(tool: str, *, controller: bool = False) -> ToolPlan:
    if controller:
        message = f"{tool} is not allowed by the current controller state"
        kind = "disallowed_tool"
    else:
        message = f"disallowed tool for this task policy: {tool}"
        kind = "disallowed_tool"
    return ToolPlan(calls=[], control=None, protocol_error=message, protocol_error_kind=kind,
                    protocol_tool_name=(tool or ""))


def _invalid_plan(message: str, *, tool: str = "", kind: str = "invalid_args") -> ToolPlan:
    return ToolPlan(calls=[], control=None, protocol_error=message, protocol_error_kind=kind,
                    protocol_tool_name=(tool or ""))


def _tool_and_args(obj: Mapping[str, JsonValue]) -> tuple[str, ToolArgs]:
    tool = str(obj.get("tool", "") or "").strip().lower()
    raw_args = obj.get("args", None)
    args: ToolArgs
    if isinstance(raw_args, dict):
        args = dict(raw_args)
    elif raw_args is None:
        args = {k: v for k, v in obj.items() if k != "tool"}
    else:
        args = {}
    return tool, args


def _validate_coding_args(tool: str, args: ToolArgs) -> tuple[ToolArgs, str]:
    if (tool or "").strip().lower() == "edit" and isinstance(args, dict):
        # Top-level old/new fields are not part of the canonical edit shape.
        if "old_string" in args or "new_string" in args:
            return {}, "edit requires replacements for existing files"
        replacements = args.get("replacements")
        if isinstance(replacements, list):
            for item in replacements:
                if isinstance(item, dict) and ("search" in item or "replace" in item):
                    return {}, "edit rejects legacy replacement fields: use old_string/new_string"
        for legacy_top in ("search", "replace"):
            if legacy_top in args:
                return {}, f"edit rejects legacy alias: {legacy_top}"
    try:
        from codey.toolchain import definition as tool_defs
        from codey.toolchain.runtime import MAX_REPLACEMENTS, READ_MAX_LINES
        from codey.toolchain.tool_args_repair import (
            ToolArgLimits,
            ToolArgsRepairError,
            normalize_tool_args,
        )
    except Exception as exc:
        return {}, f"coding validator unavailable: {exc}"
    spec = tool_defs.TOOL_DEFINITION_BY_NAME.get(tool)
    if spec is None or spec.runtime_name is None:
        return {}, f"unknown tool: {tool}"
    try:
        repair = normalize_tool_args(
            spec.runtime_name, dict(args),
            limits=ToolArgLimits(max_replacements=MAX_REPLACEMENTS, read_max_lines=READ_MAX_LINES),
        )
    except ToolArgsRepairError as exc:
        return {}, str(exc)
    except Exception as exc:
        return {}, f"{tool} args invalid: {exc}"
    return decode_json_object(repair.args), ""


def _validate_research_args(tool: str, args: ToolArgs) -> tuple[ToolArgs, str]:
    if tool in _CONTROLLER_ALIASES:
        id_key = _ALIAS_ARGS[tool][1]
        value = str(args.get(id_key, "") or "").strip()
        if not value:
            return {}, f"{tool} missing required arg '{id_key}'"
        return {**args, id_key: value}, ""
    if tool == "source_search" and "source_id" in args:
        # Controller alias form: source_id resolves via controller state at
        # execution time; here only require a non-empty id and query.
        sid = str(args.get("source_id", "") or "").strip()
        query = str(args.get("query", "") or "").strip()
        if not sid:
            return {}, "source_search missing required arg 'source_id'"
        if not query:
            return {}, "source_search missing required arg 'query'"
        return {**args, "source_id": sid, "query": query}, ""
    try:
        from codey.research.tool_contract import validate_tool_args
    except Exception as exc:
        return {}, f"research validator unavailable: {exc}"
    try:
        result = validate_tool_args(tool, dict(args))
    except Exception as exc:
        return {}, f"{tool} args invalid: {exc}"
    if not result.ok:
        return {}, result.error or f"{tool} args invalid"
    # The contract validator fills optional defaults for older callers. The
    # shared kernel retains only arguments the model actually supplied, so
    # restricted subflows can still reject explicitly forbidden fields.
    return decode_json_object({key: value for key, value in result.args.items() if key in args}), ""


def _lookup_spec(name: str, frozen_specs: Mapping[str, ToolSpec] | None) -> tuple[ToolSpec | None, str]:
    try:
        if frozen_specs is not None:
            spec = frozen_specs.get(name)
            return spec, (spec.executor if spec is not None else "")
        from codey.toolchain.tool_spec import spec_for_tool as _spec_for

        spec = _spec_for(name)
        return spec, (spec.executor if spec is not None else "")
    except Exception:
        return None, ""


def _spec_error_for(
    name: str,
    args: ToolArgs,
    spec: ToolSpec,
    frozen_specs: Mapping[str, ToolSpec] | None,
) -> str:
    payload = args
    try:
        if frozen_specs is not None:
            from codey.toolchain.tool_spec import validate_args_with_spec as _frozen_validate

            return _frozen_validate(spec, payload) or ""
        from codey.toolchain.tool_spec import validate_args_against_spec as _live_validate

        return _live_validate(name, payload) or ""
    except Exception as exc:
        detail = str(exc).strip()[:200] or type(exc).__name__
        return f"{name} args invalid: spec validator failed ({detail})"


def _validate_tool_args(
    tool: str, args: ToolArgs, *, frozen_specs: Mapping[str, ToolSpec] | None = None,
) -> tuple[ToolArgs, str]:
    name = _canonical_name(tool) or (tool or "").strip().lower()
    # ToolSpec is the single authoritative definition: required-arg presence
    # is decided here so JSON/native schemas and validation cannot drift.
    # Project/research legacy validators are additional type-repair constraints
    # only; third-task (custom) tools pass on the generic spec alone.
    spec, executor = _lookup_spec(name, frozen_specs)
    if spec is None:
        return {}, f"unknown tool: {tool or '?'}"
    spec_error = _spec_error_for(name, args if isinstance(args, dict) else {}, spec, frozen_specs)
    if spec_error:
        return {}, spec_error
    if name == "ask_user":
        from codey.task.questions import question_args
        try:
            return decode_json_object(question_args(args)), ""
        except ValueError as exc:
            return {}, str(exc)
    if name == "done":
        # ``done`` is a control tool with no runtime executor. Its
        # canonical ToolSpec validation above is the complete contract.
        return dict(args), ""
    if name == "edit" and "browser_source" in args and any(key == "browser_source" for key, _ in spec.parameters):
        return dict(args), ""
    if name == "codemode":
        try:
            from codey.codemode.protocol import parse_codemode_args

            program = parse_codemode_args(args if isinstance(args, dict) else {})
            from codey.runtime.core.models import canonical_json_mapping

            normalized: ToolArgs = {
                "code": program.code,
                "language": program.language,
                "inputs": canonical_json_mapping(program.inputs),
            }
            if program.repair_of:
                normalized["repair_of"] = program.repair_of
            return normalized, ""
        except ValueError as exc:
            return {}, str(exc)
    # Custom/third-task tools: generic spec validation is sufficient.
    # They run via injected executors; no legacy coding/research repair.
    if executor not in {"project", "source", "knowledge"}:
        # Controller aliases always lower via research validation.
        is_alias = name in _CONTROLLER_ALIASES
        if is_alias or (name == "source_search" and isinstance(args, dict) and "source_id" in args):
            return _validate_research_args(name, args if isinstance(args, dict) else {})
        return dict(args) if isinstance(args, dict) else {}, ""
    if name in _CONTROLLER_ALIASES or (isinstance(args, dict) and name == "source_search" and "source_id" in args):
        return _validate_research_args(name, args)
    if executor == "project":
        return _validate_coding_args(name, args)
    if executor in {"source", "knowledge"}:
        return dict(args), ""
    return {}, "unsupported tool executor"


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


def _drop_legacy_done_declarations(args: ToolArgs, *, runtime_owned: bool) -> ToolArgs:
    ignored = {"source_gaps", "execution_claims"} if runtime_owned else set()
    return {key: value for key, value in args.items() if key not in ignored}


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
                args = _drop_legacy_done_declarations(args, runtime_owned=ignore_runtime_claims)
            validated, error = _validate_tool_args(tool, args, frozen_specs=frozen_specs)
            if error:
                return _invalid_plan(error, tool=tool)
            text = str(validated.get("summary" if tool == "done" else "question") or "")
            from codey.policies.output_requirement import has_explanation

            if tool != "done" or policy is None or not has_explanation(policy.required_checks):
                text = text.strip()
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
    items = _response_objects(reply)
    if isinstance(items, ToolPlan):
        if (snapshot is not None and snapshot.browser_copy is not None and items.control is not None
                and items.control.kind == "done" and "done" not in snapshot.tool_names):
            return _invalid_plan("Finish the browser copy before reporting completion.", tool="done")
        if snapshot is not None and snapshot.research_scope is not None and items.control is not None and items.control.kind == "done":
            return _invalid_plan("Submit the answer with submit_research_answer under the frozen contract; global done is not allowed")
        done_spec = frozen.get("done") if frozen is not None else None
        if (items.control is not None and items.control.kind == "done" and not items.protocol_error
                and done_spec is not None and "report_sections" in done_spec.required):
            return _invalid_plan("Research completion requires done.report_sections.coverage under the frozen contract; "
                                 "plain text and summary-only reports are not accepted.", tool="done")
        return items
    if snapshot is not None and snapshot.browser_copy is not None and len(items) != 1:
        return _invalid_plan("Submit one browser copy action per turn.")
    if snapshot is not None and snapshot.input_binding is not None:
        binding = snapshot.input_binding
        lowered: list[tuple[str, ToolArgs, str]] = []
        for raw, args, call_id in items:
            name = _canonical_name(raw)
            if name != binding.model_spec.name or name not in snapshot.tool_names:
                lowered.append((raw, args, call_id))
                continue
            spec = binding.model_spec
            if error := validate_args_with_spec(spec, args):
                return _invalid_plan(error, tool=name)
            try:
                if binding.kind == "upload":
                    converted = lower_upload_action(args, snapshot.policy.required_checks if snapshot.policy else ())
                else:
                    if snapshot.browser_copy is None:
                        raise ValueError("browser copy input has no captured observation")
                    converted = snapshot.browser_copy.lower(args)
                lowered.append((raw, converted, call_id))
            except (KeyError, TypeError, ValueError) as exc:
                return _invalid_plan(str(exc), tool=name)
        items = lowered
    return _plan_from_tool_objects(
        items, policy=policy, controller_allowed=allowed,
        snapshot_names=snapshot_names, frozen_specs=frozen,
        ignore_runtime_claims=snapshot.ignore_runtime_claims if snapshot is not None else False,
    )


__all__ = [
    "MAX_NATIVE_CALLS_PER_TURN",
    "TurnSnapshot",
    "build_turn_snapshot",
    "controller_allowed_for_session",
    "frozen_custom_executor_map",
    "frozen_spec_map",
    "normalize_turn",
]
