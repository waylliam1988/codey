"""Assemble the authorized tool inventory and domain contributions for a turn."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from codey.operations.browser import browser_followups
from codey.operations.browser.browser_copy import CopyTurn, copy_target, copy_turn
from codey.operations.browser.browser_upload import (
    STRUCTURED_UPLOAD_TOOLS,
    structured_upload_spec,
    task_upload_scope,
    upload_model_spec,
    upload_phase,
)
from codey.operations.browser.turn_inputs import BrowserTurnInputs
from codey.operations.kernel.turn_contract import ModelInputBinding, TurnSnapshot
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
from codey.operations.project.edit_base import symbol_read_refs
from codey.operations.project.turn_inputs import ProjectTurnInputs
from codey.operations.research.turn_inputs import ResearchTurnInputs
from codey.toolchain.tool_spec import freeze_spec_parameters, is_text_batch_tool

if TYPE_CHECKING:
    from codey.completion.verification_policy import VerificationCandidate
    from codey.operations.kernel.executor import ToolExecutor
    from codey.policies.task_policy import TaskPolicy
    from codey.research.answer_contract import AnswerScope
    from codey.runtime.core.execution_record import ExecutionRecord
    from codey.runtime.core.models import ToolResult
    from codey.toolchain.spec_model import ToolSpec


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


def controller_allowed_for_session(session: KernelSessionView) -> tuple[str, ...] | None:
    if session.task_kind == "research_review":
        raise ValueError("same-model research assessment execution has been retired")
    from codey.research.questions import requires_question_ref
    from codey.research.task_mode import whole_research
    from codey.research.tool_names import QUESTION_BOUND_TOOLS
    from codey.toolchain.tool_spec import visible_tool_names_for_snapshot

    denied = {"submit_research_review", *(name.strip().lower() for name in session.controller_denied)}
    if whole_research(session):
        denied.add("knowledge_commit")
    else:
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
    from codey.operations.browser.browser_waits import wait_spec
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
    from codey.research.answer_contract import answer_scope
    from codey.research.task_mode import whole_research

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
            task_source_url=task_source_url, task_source_selector=task_source_url is not None,
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
    data_file_reads: tuple[str, ...] | None = None
    if scope is None and {"read_file", "read_data"} <= set(nested):
        from codey.research.calculation import file_selector_key

        data_file_reads = tuple(sorted({
            file_selector_key(path) for ref, result in session._memory_results.items()
            if ref in session.executed and result.ok and result.call.name == "read_file"
            and isinstance(path := result.call.args.get("path"), str)
        }))
    return refresh_snapshot_contract(TurnSnapshot(
        allowed=allowed,
        model_specs=(), contract_text="",
        tool_names=names,
        policy=session.policy,
        frozen_specs=frozen,
        custom_executors=executors,
        nested_tool_names=nested,
        research=ResearchTurnInputs(scope, data_file_reads),
        ignore_runtime_claims=session.task_kind == "chat" and not session.policy.strict_research
                              and not has_explanation(session.policy.required_checks),
        browser=BrowserTurnInputs(copy=browser_copy, navigation_url=task_navigation_url, source_url=task_source_url),
        input_binding=binding,
        project=ProjectTurnInputs(symbol_reads=(
            tuple(sorted(symbol_read_refs(session._memory_results, session.executed).items()))
            if "edit" in inventory else ()
        )),
    ))


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
