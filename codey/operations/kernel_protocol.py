"""Single turn protocol for the unified kernel: JSON text and native calls.

Web JSON replies and native ``AssistantTurn`` values converge here into one
canonical ``ToolPlan`` under the same ``TaskPolicy`` and controller state.
Unknown tools are denied, never passed as ``control``.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from codey.operations import browser_followups
from codey.operations.browser_copy import CopyTurn, copy_target, copy_turn
from codey.operations.browser_references import BrowserReferences, reference_spec
from codey.operations.browser_upload import (
    STRUCTURED_UPLOAD_TOOLS,
    lower_upload_action,
    structured_upload_spec,
    task_upload_scope,
    upload_model_spec,
    upload_phase,
)
from codey.operations.edit_base import (
    bind_symbol_read,
    bound_symbol_retry,
    lower_compact_symbol_edit,
    raw_symbol_content,
    symbol_read_refs,
)
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
from codey.protocols.text_turn import DecodedTextTurn, TextAnswer, TextError, TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import Control, ToolCall, ToolPlan, ToolResult
from codey.toolchain.tool_spec import (
    ToolSpec,
    freeze_spec_parameters,
    is_text_batch_tool,
    validate_args_with_spec,
)
from codey.types.json_value import JsonValue, ToolArgs, canonical_json_object, decode_json_object
from codey.types.protocol_window import ProtocolWindowRef

MAX_NATIVE_CALLS_PER_TURN = 8
InputContractVersion: TypeAlias = Literal[11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]

if TYPE_CHECKING:
    from codey.completion.verification_policy import VerificationCandidate
    from codey.operations.project_instructions import ProjectInstructionBinding
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.protocols.json_repair import JsonTailClosure
    from codey.research.answer_contract import AnswerScope


class KernelSessionView(Protocol):
    @property
    def project(self) -> str: ...

    @property
    def verification_candidates(self) -> tuple[VerificationCandidate, ...]: ...

    @property
    def selected_verification(self) -> VerificationCandidate | None: ...

    @property
    def verification_paths(self) -> tuple[str, ...]: ...

    @property
    def verification_forbidden(self) -> bool: ...

    @property
    def verification_candidates_refresh_failed(self) -> bool: ...

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
    input_contract_version: InputContractVersion = 14
    browser_followups: bool = False
    browser_fill_text: str | None = None
    browser_references: BrowserReferences | None = None
    project_instructions: tuple[ProjectInstructionBinding, ...] = ()
    navigation_verifies: bool = False
    data_file_reads: tuple[str, ...] = ()
    symbol_read_refs: tuple[tuple[str, str], ...] = ()

    @property
    def bound_source_url(self) -> str | None:
        return self.task_source_url if self.input_contract_version >= 15 else self.task_navigation_url

    @property
    def task_navigation_selector(self) -> bool:
        return self.input_contract_version >= 12

    @property
    def navigation_observes(self) -> bool:
        return (self.input_contract_version >= 14 and self.browser_followups
                and "browser_snapshot" in self.nested_tool_names)

    @property
    def inventory_names(self) -> tuple[str, ...]:
        names: dict[str, None] = dict.fromkeys((*self.tool_names, *self.nested_tool_names))
        return tuple(names)

    @property
    def has_project_tools(self) -> bool:
        return "codemode" in self.tool_names and bool({"edit", "read_file"}.intersection(self.tool_names))

    @property
    def model_specs(self) -> tuple[ToolSpec, ...]:
        from codey.codemode.verification import verification_model_spec

        binding = self.input_binding
        specs = tuple(binding.model_spec if binding is not None and spec.name == binding.model_spec.name else spec
                      for spec in self.frozen_specs if spec.name in self.tool_names)
        specs = tuple(reference_spec(spec, self.browser_references) for spec in specs)
        specs = tuple(verification_model_spec(spec) if spec.name == "run" else spec for spec in specs)
        if self.input_contract_version >= 13 and self.bound_source_url is not None:
            from codey.codemode.navigation import TASK_SOURCE_TOOLS, task_source_spec

            specs = tuple(task_source_spec(spec) if spec.name in TASK_SOURCE_TOOLS else spec for spec in specs)
        if not self.browser_followups:
            return specs
        if self.task_navigation_selector and self.task_navigation_url is not None:
            from codey.codemode.navigation import task_navigation_spec

            specs = tuple(task_navigation_spec(spec) if spec.name == "browser_navigate" else spec for spec in specs)
        plans = browser_followups.click_plans(self.policy.required_checks if self.policy else (),
                                              set(self.nested_tool_names))
        purposes = tuple(purpose for purpose in browser_followups.PURPOSES if purpose in plans)
        return tuple(browser_followups.model_spec(spec, self.browser_fill_text, click_purposes=purposes,
                     navigation_observes=self.navigation_observes,
                     navigation_verifies=self.navigation_verifies) for spec in specs)


def render_snapshot_contract(snapshot: TurnSnapshot) -> str:
    from codey.toolchain.tool_prompt import json_contract_text

    text = json_contract_text(snapshot.model_specs)
    if snapshot.browser_copy is not None and snapshot.browser_copy.phase == "question":
        text += "\nAsk which webpage source to use. The observation has no unique supported data format."
    return text


def refresh_snapshot_contract(snapshot: TurnSnapshot, *, compact_catalog: bool = False,
                              include_json_edit: bool = False) -> TurnSnapshot:
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
    return replace(snapshot, contract_text=render_snapshot_contract(snapshot))


def offer_browser_tools(snapshot: TurnSnapshot, session: KernelSessionView) -> TurnSnapshot:
    from codey.toolchain.browser_native_specs import browser_native_specs

    exposed = session.model_tool_names
    direct_names = tuple(name for name in (*browser_followups.DIRECT_TOOLS, "browser_wait", *browser_native_specs())
        if browser_followups.allows_surface(snapshot, (name,))
        and name not in session.controller_denied and (exposed is None or name in exposed))
    if not direct_names:
        return snapshot
    if browser_followups.codemode_only(session.task_text):
        return refresh_snapshot_contract(replace(snapshot, browser_followups=True))
    names = (*snapshot.tool_names, *(name for name in direct_names if name not in snapshot.tool_names))
    offered = replace(snapshot, tool_names=names, browser_followups=True,
                      browser_fill_text=browser_followups.explicit_fill_text(session.task_text))
    return refresh_snapshot_contract(offered)


def offer_browser_call(snapshot: TurnSnapshot, session: KernelSessionView, name: str, *, research: bool = False) -> TurnSnapshot:
    """Offer an existing leaf through the same single-step CodeMode lowering."""
    eligible = set(browser_followups.FOLLOWUP_TOOLS)
    if (name not in eligible
            or not browser_followups.allows_surface(snapshot, (name,), research=research)
            or name in session.controller_denied
            or (session.model_tool_names is not None and name not in session.model_tool_names)):
        return snapshot
    if browser_followups.codemode_only(session.task_text):
        return refresh_snapshot_contract(replace(snapshot, browser_followups=True))
    return refresh_snapshot_contract(replace(snapshot, browser_followups=True,
        tool_names=(*snapshot.tool_names, *((name,) if name not in snapshot.tool_names else ()))))


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
    from codey.research.answer_contract import whole_research
    from codey.research.questions import QUESTION_BOUND_TOOLS, requires_question_ref
    from codey.toolchain.tool_spec import visible_tool_names_for_snapshot

    denied = {"submit_research_review", *(name.strip().lower() for name in session.controller_denied)}
    if not whole_research(session):
        denied.add("submit_research_answer")
    if not requires_question_ref(session):
        denied.add("research_plan")
    elif not session.research_questions:
        # A missing plan blocks only tools whose own input needs a question.
        denied.update(QUESTION_BOUND_TOOLS)
    aliases = {"open_result": bool(session.search_results), "reopen_source": bool(session.source_ids),
               "open_hit": bool(session.hit_targets)}
    denied.update(name for name, observed in aliases.items() if not observed)
    return tuple(name for name in visible_tool_names_for_snapshot(session.policy, None) if name not in denied)


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


def _task_edit_specs(session: KernelSessionView, frozen: tuple[ToolSpec, ...]) -> tuple[ToolSpec, ...]:
    from codey.codemode.json_edit import requires_content_edit
    from codey.operations.file_field_binding import file_rules

    if requires_content_edit(session.task_text):
        return tuple(freeze_spec_parameters(replace(spec,
            parameters=tuple((key, value) for key, value in spec.parameters if key in {"path", "content", "base_result_ref"}),
            required=("path", "content", "base_result_ref"))) if spec.name == "edit" else spec for spec in frozen)
    if file_rules(session.policy.required_checks):
        return tuple(freeze_spec_parameters(replace(spec, parameters=(*spec.parameters,
            ("declared_fields", {"type": "boolean", "enum": [True], "description":
                "Copy only task-declared fields from complete browser receipts. Supply path and the original "
                "complete read's base_result_ref; do not supply content or changes."})))) if spec.name == "edit" else spec
            for spec in frozen)
    return frozen


def _task_tool_specs(session: KernelSessionView, frozen: tuple[ToolSpec, ...],
                     scope: AnswerScope | None) -> tuple[ToolSpec, ...]:
    """Select completion once; bind investigation tools on their separate route."""
    from codey.codemode.verification import verification_spec
    from codey.completion.verification_policy import (
        VerificationPlan,
        preferred_verification_candidate,
        select_verification_requirement,
    )
    from codey.operations.browser_waits import wait_spec
    from codey.operations.chat_knowledge import chat_knowledge_spec, hybrid_knowledge_spec, read_knowledge_versions
    from codey.operations.execution_claims import execution_claim_spec
    from codey.policies.output_requirement import explanation_spec, has_explanation
    from codey.research.answer_contract import answer_spec
    from codey.research.finding_choices import finding_choices
    from codey.research.questions import requires_question_ref
    from codey.research.tool_contract import done_spec_for_purpose, question_bound_spec
    from codey.toolchain.language_server import available_server

    frozen = tuple(wait_spec(spec, session.policy.required_checks) if spec.name == "browser_wait" else spec
                   for spec in frozen)

    explaining = has_explanation(session.policy.required_checks)
    if explaining and session.policy.strict_research:
        raise ValueError("json_explanation cannot replace the strict Research answer contract")
    purpose = ("evidence" if session.task_kind == "evidence_followup" else
               "report" if session.policy.strict_research and scope is None else "chat" if session.task_kind == "chat" else "ordinary")
    hybrid = purpose == "ordinary" and session.task_kind == "hybrid"
    versions = (read_knowledge_versions(session.executed, session._memory_results)
                if (purpose == "chat" or hybrid) and not explaining else [])
    bind_questions = requires_question_ref(session)
    question_refs = tuple(f"q{index}" for index in range(1, len(session.research_questions) + 1))
    selected: list[ToolSpec] = []
    for spec in frozen:
        if spec.name == "read_data":
            from codey.research.calculation import read_data_spec

            spec = read_data_spec(spec, scope)
        if spec.name == "semantic_definition" and (not session.project or not available_server()):
            continue
        if spec.name == "workspace_diff" and (not session.project or not session.policy.allows("project.write")):
            continue
        if (spec.name == "run" and session.policy.allows("project.verify") and not session.verification_forbidden
                and not session.verification_candidates_refresh_failed):
            requirement = select_verification_requirement(session.verification_candidates, session.verification_paths)
            candidate = requirement or session.selected_verification or preferred_verification_candidate(session.verification_candidates)
            if candidate is not None and (not isinstance(candidate, VerificationPlan) or candidate.obligations):
                spec = verification_spec(spec, candidate)
        if spec.name == "done":
            if explaining:
                spec = explanation_spec(spec)
            else:
                spec = done_spec_for_purpose(spec, purpose, task=session.task_text, questions=session.research_questions)
                if purpose == "chat":
                    spec = chat_knowledge_spec(spec, versions)
                elif hybrid:
                    spec = hybrid_knowledge_spec(spec, versions,
                        knowledge_required="knowledge_versions" in session.policy.required_checks)
            spec = execution_claim_spec(spec, session._memory_results)
        elif bind_questions:
            bound = question_bound_spec(spec, question_refs=question_refs)
            if bound is None:
                continue
            spec = bound
        if spec.name == "save_finding" and scope is not None:
            finding = finding_choices(spec, scope, session)
            if finding is None:
                continue
            spec = finding
        selected.append(answer_spec(spec, scope) if scope is not None else spec)
    return tuple(selected)


def build_turn_snapshot(session: KernelSessionView, *, native: bool = False) -> TurnSnapshot:
    """Build the round's authoritative snapshot; raises on failure."""
    # Fail closed: a snapshot computation failure never encodes as None
    # (unlimited). Callers must treat the exception as a per-turn config
    # error and stop.
    from codey.policies.output_requirement import has_explanation
    from codey.research.answer_contract import answer_scope, whole_research

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
    denied = {name.strip().lower() for name in session.controller_denied}
    denied_browser = {spec.name for spec in frozen if spec.executor == "browser" and spec.name in denied}
    names = tuple(name for name in names if name not in denied_browser)
    frozen = tuple(spec for spec in frozen if spec.name not in denied_browser)
    frozen = _task_tool_specs(session, frozen, scope)
    available = {spec.name for spec in frozen}
    names = tuple(name for name in names if name in available)
    nested: tuple[str, ...] = ()
    binding = None
    task_navigation_url: str | None = None
    task_source_url: str | None = None
    input_version: InputContractVersion = 17
    target = copy_target(session.task_text)
    frozen = _task_edit_specs(session, frozen)
    if "codemode" in names:
        from codey.codemode.catalog import build_codemode_catalog

        aggregate_browser = any(spec.executor == "browser" for spec in frozen)
        # Browser aggregation keeps the real project edit action in the same
        # CodeMode surface as browser/read leaves. Returning modified data is
        # still not a write; only the nested kernel edit receipt can satisfy
        # project completion. Project verification remains outside this
        # composable surface.
        upload = task_upload_scope(session.task_kind, session.task_text, session.required_source_urls, session.policy)
        if (upload is None and target is None
                and any(spec.name in {"browser_navigate", "open_url"} for spec in frozen)):
            task_navigation_url = _task_navigation_url(session)
            if task_navigation_url is not None and "open_url" in available:
                task_source_url = (_other_task_source_url(session, task_navigation_url)
                                   if len(session.required_source_urls) > 1 else task_navigation_url)
        catalog = build_codemode_catalog(frozen, semantic_edits=True, task_navigation_url=task_navigation_url,
            task_source_url=task_source_url, task_source_selector=input_version < 15 or task_source_url is not None,
            allow_writes=session.policy.allows("project.write"),
            allow_project_run=session.policy.allows("project.verify"))
        nested = tuple(entry.name for entry in catalog.entries)
        if aggregate_browser:
            # Browser leaves compose through CodeMode. Granting browsing must
            # not remove the existing source, knowledge or project entrypoints.
            # Both surfaces use these same frozen specs and kernel executors.
            browser_names = {spec.name for spec in frozen if spec.executor == "browser"}
            names = tuple(name for name in names if name not in browser_names)
            if browser_followups.codemode_only(session.task_text):
                names = tuple(name for name in names if name not in nested and not is_text_batch_tool(name))
        if upload is not None and set(nested) >= STRUCTURED_UPLOAD_TOOLS:
            phase = upload_phase(session._memory_results)
            binding = ModelInputBinding("upload", upload_model_spec(structured_upload_spec(
                next(spec for spec in frozen if spec.name == "codemode"), upload, phase)))
            frozen = tuple(structured_upload_spec(spec, upload, phase) if spec.name != "codemode" else spec
                           for spec in frozen)
    if "codemode" not in names and target is None and "open_url" in names:
        task_navigation_url = _task_navigation_url(session)
        task_source_url = task_navigation_url
    browser_copy = None
    copy_tools = {"browser_navigate", "browser_eval", "read_file", "edit"}
    if (not session.policy.strict_research and "codemode" in names and "done" in names
            and copy_tools <= set(nested) and target is not None):
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
                    "ref": {"type": "string", "minLength": 1},
                    "selectors" if target.additional_fields else "selector": {"type": "object"}},
                    "required": ["ref", "selectors" if target.additional_fields else "selector"], "additionalProperties": False}),
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
    data_file_reads: tuple[str, ...] = ()
    if scope is None and {"read_file", "read_data"} <= set(nested):
        from codey.research.calculation import file_selector_key

        input_version = 20
        data_file_reads = tuple(sorted({
            file_selector_key(path) for ref, result in session._memory_results.items()
            if ref in session.executed and result.ok and result.call.name == "read_file"
            and isinstance(path := result.call.args.get("path"), str)
        }))
    return refresh_snapshot_contract(TurnSnapshot(
        allowed=allowed,
        contract_text="",
        tool_names=names,
        policy=session.policy,
        frozen_specs=frozen,
        custom_executors=executors,
        nested_tool_names=nested,
        research_scope=scope,
        ignore_runtime_claims=session.task_kind == "chat" and not session.policy.strict_research
                              and not has_explanation(session.policy.required_checks),
        browser_copy=browser_copy,
        input_binding=binding,
        task_navigation_url=task_navigation_url, task_source_url=task_source_url,
        input_contract_version=_current_input_version(input_version, inventory, nested),
        data_file_reads=data_file_reads,
        symbol_read_refs=tuple(sorted(symbol_read_refs(session._memory_results, session.executed).items()))
                         if "edit" in inventory else (),
    ))


def _current_input_version(previous: InputContractVersion, inventory: tuple[str, ...],
                           nested: tuple[str, ...]) -> InputContractVersion:
    if "edit" in inventory:
        return 22
    return 21 if {"browser_click", "browser_wait", "browser_snapshot"} <= set(nested) else previous


def _task_navigation_url(session: KernelSessionView) -> str | None:
    from codey.policies.network import check_fetch_url
    from codey.task.web_intent import explicit_web_intent

    intent = explicit_web_intent(session.task_text)
    if intent.urls != session.required_source_urls:
        return None
    targets = intent.original_urls if len(intent.urls) == 1 else intent.original_navigation_urls
    if len(targets) != 1:
        return None
    url = next(iter(targets))
    return url if check_fetch_url(url, resolve=False) is None else None


def _other_task_source_url(session: KernelSessionView, navigation_url: str) -> str | None:
    from codey.policies.network import check_fetch_url
    from codey.task.web_intent import explicit_web_intent, source_url_key

    sources = explicit_web_intent(session.task_text).original_urls - {navigation_url}
    if len(sources) != 1:
        return None
    url = next(iter(sources))
    if source_url_key(url) not in session.required_source_urls or check_fetch_url(url, resolve=False) is not None:
        return None
    return url


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


def _lower_bound_inputs(
    items: list[tuple[str, ToolArgs, str]], snapshot: TurnSnapshot,
) -> list[tuple[str, ToolArgs, str]] | ToolPlan:
    from codey.codemode.navigation import TASK_SOURCE_TOOLS
    from codey.policies.tool_requirement import parse_tool_success_check

    binding = snapshot.input_binding
    models = {spec.name: spec for spec in snapshot.model_specs}
    checks = snapshot.policy.required_checks if snapshot.policy else ()
    lowered: list[tuple[str, ToolArgs, str]] = []
    for raw, args, call_id in items:
        name = _canonical_name(raw)
        if name == "save_finding" and name in models and snapshot.research_scope is not None:
            from codey.research.finding_choices import resolve_finding_passages

            args = resolve_finding_passages(args, snapshot.research_scope, models[name])
        if name in {"run", "edit"} and name in models:
            from codey.codemode.verification import resolve_verification

            try:
                args = (resolve_verification(args, {"properties": canonical_json_object(
                            dict(next(spec.parameters for spec in snapshot.frozen_specs if spec.name == "run")))})
                        if name == "run" else bind_symbol_read(
                            lower_compact_symbol_edit(args, models[name]), dict(snapshot.symbol_read_refs)))
            except ValueError as exc:
                return _invalid_plan(str(exc), tool=name)
        if name in TASK_SOURCE_TOOLS and name in models and snapshot.input_contract_version >= 13 and snapshot.bound_source_url is not None:
            from codey.codemode.navigation import resolve_task_source

            if error := validate_args_with_spec(models[name], args):
                return _invalid_plan(error, tool=name)
            lowered.append((raw, resolve_task_source(args, snapshot.bound_source_url), call_id))
            continue
        bound_observation = name in {"browser_snapshot", "browser_read"} and any(
            (rule := parse_tool_success_check(check)) is not None and rule.tool == name and rule.via == "codemode"
            for check in checks)
        direct = snapshot.browser_followups and name in models and (
            name in {"browser_fill", "browser_click", *browser_followups.SINGLE_STEP_TOOLS} or bound_observation
            or isinstance(args.get("ref"), dict) and snapshot.browser_references is not None
            or name == "browser_navigate" and snapshot.navigation_observes
            or name == "browser_navigate" and snapshot.task_navigation_selector and snapshot.task_navigation_url is not None
            or snapshot.research_scope is not None and name in {"browser_snapshot", "browser_read", "browser_navigate"})
        if not direct and (binding is None or name != binding.model_spec.name or name not in models):
            lowered.append((raw, args, call_id))
            continue
        if error := validate_args_with_spec(models[name], args):
            return _invalid_plan(error, tool=name)
        try:
            if direct:
                if len(items) != 1:
                    return _invalid_plan("Submit one direct browser action per turn.", tool=name)
                if name == "browser_fill":
                    inputs = browser_followups.fill_args(args, snapshot.browser_fill_text)
                    code = browser_followups.DIRECT_REF + browser_followups.FILL + browser_followups.OBSERVE
                elif name == "browser_navigate" and snapshot.navigation_observes:
                    inputs = dict(args)
                    verify = snapshot.navigation_verifies and (args.get("target") == "task"
                             or args.get("url") == snapshot.task_navigation_url)
                    code = browser_followups.NAVIGATE_VERIFY if verify else browser_followups.NAVIGATE
                elif name == "browser_wait" and args.get("condition") == "text_changed" and args.get("run_in_background") is not True:
                    inputs = dict(args)
                    code = "const action=unwrap(await tools.browser_wait(inputs));" + browser_followups.OBSERVE
                else:
                    lowered.append(("codemode", {"code": f"return unwrap(await tools.{name}(inputs));",
                                                  "inputs": dict(args)}, call_id))
                    continue
                lowered.append(("codemode", {"code": code, "inputs": inputs}, call_id))
                continue
            assert binding is not None
            if binding.kind == "upload":
                converted = lower_upload_action(args, checks)
            elif binding.kind == "declared_fields":
                from codey.operations.declared_field_input import lower

                converted = lower(args, next(spec for spec in snapshot.frozen_specs if spec.name == "edit"))
                lowered.append(("codemode", converted, call_id))
                continue
            else:
                if snapshot.browser_copy is None:
                    raise ValueError("browser copy input has no captured observation")
                converted = snapshot.browser_copy.lower(args)
            lowered.append((raw, converted, call_id))
        except (KeyError, TypeError, ValueError) as exc:
            return _invalid_plan(str(exc), tool=name)
    return lowered


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
    source = (_bound_symbol_source(reply) if snapshot is not None and bound_symbol_retry(snapshot) is not None else "")
    items: list[tuple[str, ToolArgs, str]] | ToolPlan
    items = [("edit", {"content": source}, "")] if source else _response_objects(reply)
    if isinstance(items, ToolPlan):
        if items.protocol_error and (closed := _try_close_done(reply, snapshot)) is not None:
            return closed
        return _validate_control_plan(items, snapshot, frozen)
    if snapshot is not None and snapshot.browser_copy is not None and len(items) != 1:
        return _invalid_plan("Submit one browser copy action per turn.")
    if snapshot is not None:
        lowered = _lower_bound_inputs(items, snapshot)
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
                              file_reads=snapshot.data_file_reads if snapshot.input_contract_version >= 20 else None)
    return plan


def _bound_symbol_source(reply: DecodedTextTurn | AssistantTurn) -> str:
    if (isinstance(reply, AssistantTurn) and reply.raw.get("text_protocol") is True
            and reply.finish is TurnFinish.COMPLETE and not reply.tool_calls):
        return raw_symbol_content(reply.text)
    return raw_symbol_content(reply.text) if isinstance(reply, TextAnswer) else ""


__all__ = [
    "MAX_NATIVE_CALLS_PER_TURN",
    "TurnSnapshot",
    "build_turn_snapshot",
    "controller_allowed_for_session",
    "frozen_custom_executor_map",
    "frozen_spec_map",
    "normalize_turn",
]
