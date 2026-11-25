"""Frozen inputs reach the same source, command and browser execution leaves."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.codemode.catalog import snapshot_catalog
from codey.codemode.host import CodeModeHost
from codey.completion.verification_policy import VerificationCandidate
from codey.knowledge.model import canonical_json
from codey.operations.browser.browser_references import reference_spec
from codey.operations.browser.browser_task_handoff import _input_after_click, query_result_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel_preparation import capture_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import spec_for_tool
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, parse_json_object
from tests.test_browser_context_delivery import URL as CONTROL_URL
from tests.test_browser_context_delivery import install
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_readable_references import binding, observed_snapshot, program
from tests.test_declared_work_continuation import record
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tests.test_task_navigation_selector import wire_navigation_schema
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target
from tools.unified_natural_browser_probe import PROJECT

URL = "https://example.org/manuals/device-v7?q=A%2FB+9#setup"


@pytest.mark.parametrize("verb", ["请结合", "请参考", "请参照", "请对照", "请核对", "请依据", "请根据",
                                  "Please consult", "Refer to", "Compare with"])
def test_explicit_reference_requests_freeze_the_original_source_without_forcing_navigation(verb: str) -> None:
    from codey.task.web_intent import explicit_web_intent, source_url_key

    task = f"{verb} {URL} 中的实际记录和项目文件，查明问题并修复。"
    intent = explicit_web_intent(task)
    assert intent.requested and intent.urls == {source_url_key(URL)} and intent.original_urls == {URL}
    assert not intent.browser_navigation_requested
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
                          task_text=task, required_source_urls=intent.urls)
    assert restore_snapshot(snapshot_description(build_turn_snapshot(session))).task_navigation_url == URL


@pytest.mark.parametrize("task", [f"额外参考链接：{URL}", f"请不要参考 {URL}", f"Do not consult {URL}",
    f'解释这个例子："请参考 {URL}"', f"```text\nRefer to {URL}\n```"])
def test_reference_examples_background_labels_and_negations_do_not_bind_sources(task: str) -> None:
    from codey.task.web_intent import explicit_web_intent

    assert not explicit_web_intent(task).urls


def test_restored_unbound_connection_has_no_document_without_reconnecting() -> None:
    import asyncio

    from codey.automation.browser_binding import BrowserBinding
    from codey.automation.browser_contracts import BrowserAction, BrowserConfig
    from codey.automation.browser_runtime import BrowserRuntime
    from tests.test_browser_async_runtime import invoke

    async def exercise() -> None:
        runtime = BrowserRuntime(BrowserConfig(), binding=BrowserBinding(1, "unused", "", ()))
        try:
            assert await invoke(runtime, BrowserAction.DOCUMENT) is None
            assert runtime._browser is None
        finally:
            await runtime.close(release=False)

    asyncio.run(exercise())


def test_document_identity_keeps_the_loss_of_a_previously_bound_target_explicit() -> None:
    import asyncio

    from codey.automation.browser_contracts import BrowserAction, BrowserConfig, BrowserToolOutput
    from codey.automation.browser_runtime import BrowserRuntime
    from tests.support.real_cdp import chromium_executable
    from tests.test_browser_async_runtime import fixture_policy, invoke

    async def exercise() -> None:
        runtime = BrowserRuntime(BrowserConfig(browser_path=chromium_executable()), network_policy=fixture_policy)
        try:
            opened = BrowserToolOutput.read(await invoke(runtime, BrowserAction.NAVIGATE, {"url": CONTROL_URL}))
            assert opened.ok
            await runtime._pages[runtime._active].page.close()
            lost = BrowserToolOutput.read(await invoke(runtime, BrowserAction.DOCUMENT))
            assert not lost.ok and lost.observation["reason"] == "browser_target_lost"
        finally:
            await runtime.close()

    asyncio.run(exercise())


def test_read_before_navigation_leaves_the_controller_without_a_document_and_able_to_navigate(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    _session, _tools, browser = production_browser
    assert browser.document_identity() is None
    with pytest.raises(RuntimeError, match="does not bind the browser"):
        browser.capture()
    assert browser.resource_binding() is not None
    assert browser.document_identity() is None
    assert browser.navigate(CONTROL_URL).ok
    assert browser.document_identity() is not None
    assert browser.snapshot().ok


@pytest.mark.parametrize("change", ["revision_only", "body", "document", "computed_revision"])
def test_full_snapshot_revisions_do_not_disguise_repeated_observations(change: str) -> None:
    from codey.operations.progress_comparison import progress_comparison
    from tests.test_codemode_observation_progress import DOCUMENT, observe, session_for

    session = session_for()
    parents: list[ToolResult] = []
    for index in range(2):
        parent = observe(session, index,
            text="New source content" if index and change == "body" else "Unchanged source",
            document={**DOCUMENT, "loader_id": "other"} if index and change == "document" else DOCUMENT,
            result_value=index if change == "computed_revision" else None)
        child = session._memory_results[f"receipt-{index}"]
        value = decode_json_object(child.canonical["value"])
        session._memory_results[f"receipt-{index}"] = replace(child,
            canonical={**child.canonical, "value": {**value, "revision": index + 10, "kind": "full"}})
        parents.append(parent)
    comparisons = [progress_comparison(parent, session._memory_results).information for parent in parents]
    assert (comparisons[0] == comparisons[1]) is (change == "revision_only")
    assert all(progress_comparison(session._memory_results[f"receipt-{index}"], session._memory_results
                                   ).browser_observation for index in range(2))
    assert decode_json_object(session._memory_results["receipt-1"].canonical["value"])["revision"] == 11


def test_successful_detail_navigation_does_not_suggest_resetting_the_task_source(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    from codey.operations.task_handoff import task_handoff
    from codey.task.web_intent import source_url_key

    session, _tools, browser = production_browser
    session.task_text = f"Start at {URL}, follow the relevant documentation link and read its details."
    session.required_source_urls = frozenset({source_url_key(URL)})
    assert browser.navigate(CONTROL_URL).ok
    install(browser, "<main>Selected documentation details.</main>")
    snapshot = observed_snapshot(session, browser)
    _offered, hint = task_handoff(session, snapshot,
        CompletionContext(browser_document=browser.document_identity()), browser)
    assert "current page differs" not in hint and "reuse its frozen target" not in hint


@pytest.mark.parametrize("state", ["ready", "not_repeated", "filled", "uncertain", "other_document", "disabled"])
@pytest.mark.parametrize("role", ["textbox", "searchbox"])
def test_natural_observation_recovery_does_not_require_a_declared_tool_sequence(state: str, role: str) -> None:
    from codey.operations.browser.browser_references import ObservedControl
    from codey.operations.browser.browser_task_handoff import observation_recovery_handoff
    from codey.operations.browser.turn_surface import offer_browser_tools

    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
                          task_text="Find the requested official documentation through this page.")
    document: JsonObject = {"url": URL, "loader_id": "search-document"}
    refs = replace(binding(), document_json=canonical_json(document), controls=(
        ObservedControl("search-node", role, "Search", "", "", state == "disabled"),))
    if state in {"filled", "uncertain"}:
        identity = record(session, "browser_fill", {"ref": "search-node", "value": "user query"}, document,
                          ok=state == "filled")
        session._memory_results[identity] = replace(session._memory_results[identity],
            canonical={"value": {"delivery": "verified" if state == "filled" else "uncertain"}})
    snapshot = replace(offer_browser_tools(build_turn_snapshot(session), session), browser_references=refs)
    before = dict(session.executed)
    context = CompletionContext(browser_document={**document, "loader_id": "other"}
                                if state == "other_document" else document)
    offered, hint = observation_recovery_handoff(session, snapshot, context, repeated=state != "not_repeated")
    assert bool(hint) is (state == "ready")
    assert session.executed == before and set(offered.inventory_names) == set(snapshot.inventory_names)
    if hint:
        assert '"name": "Search"' in hint and "supply value" in hint and "No input has been verified" in hint
        assert "user query" not in hint


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_research_budget_does_not_serialize_sampling_schema_as_prompt_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.providers.api_provider import ApiProvider
    from codey.providers.base import ProviderToolDefinition
    from codey.providers.token_accounting import RequestContextCount

    counted: list[str] = []
    original = ApiProvider.count_text

    def count(provider: ApiProvider, text: str, *, deadline: float,
              tools: list[ProviderToolDefinition] | None = None) -> RequestContextCount:
        counted.append(text)
        return original(provider, text, deadline=deadline, tools=tools)

    monkeypatch.setattr(ApiProvider, "count_text", count)
    directory = tmp_path / "budget"
    install_http(monkeypatch, iter([{"tool": "browser_snapshot", "args": {}}]), native=False)
    gate.run_case(directory, "chat", Target("http://scripted.test/v1", "fixture", 123456789, 10, 65536),
                  strict_research=True, declare_required_tools=False, max_turns=1)
    assert counted and all("unevaluatedProperties" not in text for text in counted)
    wire = requests(directory)
    assert wire and "unevaluatedProperties" in canonical_json(wire[-1])


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("codemode", [False, True])
def test_source_only_task_binds_exact_url_and_preserves_read_options(native: bool, codemode: bool) -> None:
    grants = frozenset({"control", "web.read"}) | ({"project.read"} if codemode else set[str]())
    session = TaskSession(TaskPolicy(grants),
        task_text=f"Read <{URL}>", required_source_urls=frozenset({URL.split('#')[0]}))
    from codey.task.web_intent import source_url_key

    session.required_source_urls = frozenset({source_url_key(URL)})
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    assert snapshot.task_navigation_url == URL
    args: JsonObject = {"target": "task", "offset": 83, "limit": 107}
    proposal = (AssistantTurn(tool_calls=(ProviderToolCall("source", "open_url", args),)) if native
                else decode_text_turn(canonical_json({"tool": "open_url", "args": args})))
    plan = normalize_turn(proposal, snapshot=snapshot)
    assert not plan.protocol_error and plan.calls[0].args == {"url": URL, "offset": 83, "limit": 107}
    other: JsonObject = {"target": {"url": "https://example.org/next-source"}, "offset": 17}
    alternate = normalize_turn(decode_text_turn(canonical_json({"tool": "open_url", "args": other})), snapshot=snapshot)
    assert not alternate.protocol_error and alternate.calls[0].args == {"url": "https://example.org/next-source", "offset": 17}
    search: JsonObject = {"target": "task", "query": "device setup"}
    searched = normalize_turn(decode_text_turn(canonical_json({"tool": "source_search", "args": search})), snapshot=snapshot)
    assert not searched.protocol_error and searched.calls[0].args == {"url": URL, "query": "device setup"}
    invalid: tuple[JsonObject, ...] = ({"url": URL.replace("device-v7", "device_v7")},
        {"target": "task", "url": URL}, {"target": "other"})
    for arguments in invalid:
        assert normalize_turn(decode_text_turn(canonical_json({"tool": "open_url", "args": arguments})),
                              snapshot=snapshot).protocol_error
    if not codemode:
        assert "codemode" not in snapshot.tool_names
        return
    calls: list[ToolCall] = []
    def execute(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(call, "Read exact source", ok=True)
    host = CodeModeHost(execution_id="source-binding", catalog=tuple(
        entry for entry in snapshot_catalog(snapshot).entries if entry.name == "open_url"),
                        execute=execute, task_navigation_url=snapshot.task_navigation_url)
    host.invoke("open_url", dict[str, object](args))
    assert calls[-1].args == plan.calls[0].args
    host.invoke("open_url", {"url": "https://example.org/another-authorized-source"})
    assert calls[-1].args == {"url": "https://example.org/another-authorized-source"}


@pytest.mark.parametrize("native", [False, True])
def test_project_check_uses_frozen_command_and_cwd_without_retyping(native: bool) -> None:
    candidate = VerificationCandidate('python -m unittest discover -s "unit tests" -v', "packages/one", "project config")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.verify"})),
        verification_candidates=(candidate,))
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    session.verification_candidates = (VerificationCandidate("npm test", "elsewhere"),)
    args: JsonObject = {"command": {"check": "project"}, "output_tail_lines": 37}
    proposal = (AssistantTurn(tool_calls=(ProviderToolCall("check", "run", args),)) if native
                else decode_text_turn(canonical_json({"tool": "run", "args": args})))
    plan = normalize_turn(proposal, snapshot=snapshot)
    assert not plan.protocol_error, plan
    assert plan.calls[0].args == {"command": candidate.command, "path": candidate.cwd, "output_tail_lines": 37}
    invalid: tuple[JsonObject, ...] = ({"command": {"check": "project"}, "path": "elsewhere"},
        {"command": {"check": "invented"}}, {"command": {"check": "project", "extra": True}})
    for arguments in invalid:
        assert normalize_turn(decode_text_turn(canonical_json({"tool": "run", "args": arguments})), snapshot=snapshot).protocol_error
    ordinary = normalize_turn(decode_text_turn(canonical_json({"tool": "run", "args": {"command": "npm test"}})), snapshot=snapshot)
    assert not ordinary.protocol_error and ordinary.calls[0].args == {"command": "npm test", "path": "."}
    session.verification_forbidden = True
    forbidden = build_turn_snapshot(session)
    assert normalize_turn(proposal, snapshot=forbidden).protocol_error


@pytest.mark.parametrize("tail", [1, 37, 2000])
def test_plain_run_preserves_the_declared_output_limit(tail: int) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.verify"})))
    snapshot = build_turn_snapshot(session)
    args: JsonObject = {"command": "python -m unittest", "output_tail_lines": tail}
    plan = normalize_turn(decode_text_turn(canonical_json({"tool": "run", "args": args})), snapshot=snapshot)
    assert not plan.protocol_error and plan.calls[0].args == {**args, "path": "."}
    for invalid in (0, 2001, True, "37"):
        rejected: JsonObject = {**args, "output_tail_lines": invalid}
        assert normalize_turn(decode_text_turn(canonical_json({"tool": "run", "args": rejected})), snapshot=snapshot).protocol_error


def test_check_refresh_precedes_capture_and_does_not_change_the_previous_request() -> None:
    original = VerificationCandidate("python -m unittest", ".", "project config")
    changed = VerificationCandidate("python -m unittest -v", ".", "project config")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.verify"})),
        verification_candidates=(original,), selected_verification=original)
    previous = build_turn_snapshot(session)
    session.edited_files["application.py"] = 1
    session.verification_candidate_loader = lambda: (changed,)
    current = capture_turn_snapshot(session)
    action = decode_text_turn(canonical_json({"tool": "run", "args": {"command": {"check": "project"}}}))
    assert normalize_turn(action, snapshot=current).calls[0].args["command"] == changed.command
    assert normalize_turn(action, snapshot=restore_snapshot(snapshot_description(previous))).calls[0].args["command"] == original.command
    def fail() -> tuple[VerificationCandidate, ...]:
        raise ValueError("changed manifest unavailable")
    session.edited_files["application.py"] = 2
    session.verification_candidate_loader = fail
    unavailable = capture_turn_snapshot(session)
    assert session.verification_candidates_refresh_failed
    assert normalize_turn(action, snapshot=unavailable).protocol_error


@pytest.mark.usefixtures("public_source_dns")
def test_evidence_view_budgets_the_contract_without_revoking_original_read_windows() -> None:
    from codey.providers.token_accounting import ContextBudget, RequestContextCount
    from codey.research.answer_context import answer_context, work_view_limits
    from codey.research.answer_contract import answer_scope, submit_answer
    from codey.research.ledger import ResearchLedger
    from codey.research.source_gateway import ResearchSourceGateway
    from codey.research.source_reading import render_source_read

    class Source:
        def search(self, query: str, *, limit: int) -> list[JsonObject]:
            return []
        def fetch(self, url: str) -> dict[str, object]:
            return {"status": "ok", "url": url, "title": "Device manual", "text": "Exact original device statement. " * 600,
                    "truncated": False}
    class Provider:
        context_budget = ContextBudget(40000, 4000, 1000, 4000)
        timeout = 5.0
        def count_text(self, text: str, *, deadline: float) -> RequestContextCount:
            return RequestContextCount(len(text), "tokenizer")
    ledger = ResearchLedger()
    url = "https://example.com/device-manual"
    opened = render_source_read(ResearchSourceGateway(Source(), ledger).open(url), ledger, offset=0, limit=8000)
    assert opened.ok
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"}), strict_research=True), task_text="Explain the device.")
    session._memory_results["read"] = ToolResult(ToolCall("open_url", {"url": url}), opened.model_text, ok=True,
        canonical=canonical_json_object({"read_passages": opened.read_passages}))
    scope = answer_scope(session)
    original = tuple(ledger.read_passages.items())
    provider = Provider()
    base_count, base_cap = work_view_limits(provider)
    full = answer_context(session, ledger, scope, count_tokens=base_count, token_budget=base_cap)
    count, cap = work_view_limits(provider, fixed_context="Existing contract declarations. " * 800)
    assert count(full) > cap
    fitted = answer_context(session, ledger, scope, count_tokens=count, token_budget=cap)
    assert count(fitted) <= cap and len(fitted) < len(full)
    assert tuple(ledger.read_passages.items()) == original and answer_scope(session) == scope
    assert submit_answer(scope, {"state": "answered", "markdown": "The original device statement was read [e1]."})["citations"]


def test_file_source_readiness_also_gates_answered_reports_without_requiring_an_edit(tmp_path: Path) -> None:
    from codey.research.answer_contract import AnswerScope, answer_scope, submit_answer
    from tests.test_declared_work_continuation import declared_session

    session, _context = declared_session(tmp_path)
    complete = answer_scope(session)
    proposal: JsonObject = {"state": "answered", "markdown": "The acquired balance was 173.25 units [e1]."}
    assert complete.declared_fields_ready and submit_answer(complete, proposal)["state"] == "answered"
    assert not session.edited_files
    session._memory_results["receipt-0"] = replace(session._memory_results["receipt-0"], truncated=True)
    pending = AnswerScope.from_payload(answer_scope(session).to_payload())
    assert not pending.declared_fields_ready
    with pytest.raises(ValueError, match="declared_fields_unavailable"):
        submit_answer(pending, {"state": "answered", "markdown": "The invented balance was 999 units [e1]."})
    assert submit_answer(pending, {"state": "partial", "markdown": "The complete balance is not yet established [e1]."})["state"] == "partial"
    with pytest.raises(ValueError, match="declared_fields_ready"):
        AnswerScope.from_payload({**pending.to_payload(), "declared_fields_ready": "true"})


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
def test_actual_wire_project_check_selector_executes_the_configured_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / "check"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)
    def replies() -> Iterator[JsonObject]:
        schema = wire_navigation_schema(requests(directory)[-1], native, "run")
        command = decode_json_object(decode_json_object(schema["properties"])["command"])
        assert "anyOf" in command and "x-codey-project-check" in command
        yield {"tool": "run", "args": {"command": {"check": "project"}, "output_tail_lines": 5}}
    install_http(monkeypatch, replies(), native=native)
    gate.run_case(directory, "hybrid", Target("http://scripted.test/v1", "fixture", 123456789, 10),
        declare_required_tools=False, max_turns=1, project_files=PROJECT,
        task_transform=lambda _task: "请先运行项目已有检查，诊断并修复结果文件的问题。")
    checks = [row for row in tool_receipts(directory / "state")
              if row.result.call.name == "run" and "execution" in row.result.canonical]
    assert len(checks) == 1 and not checks[0].result.ok and checks[0].exit_code == 1
    assert checks[0].result.call.args["command"] == "python -m unittest -v"
    assert checks[0].result.call.args["path"] == "." and checks[0].result.call.args["output_tail_lines"] == 5


@pytest.mark.parametrize("name,role", [("browser_upload", "file_input"), ("browser_download", "link"),
    ("browser_select", "combobox"), ("browser_check", "checkbox"), ("browser_press", "textbox"),
    ("browser_hover", "button"), ("browser_inspect", "textbox")])
def test_all_browser_ref_contracts_share_the_observed_selector(name: str, role: str) -> None:
    from codey.operations.browser.browser_references import ObservedControl

    refs = replace(binding(), controls=(ObservedControl("opaque-node-ref", role, "Requested control", "Record", "c0", False),))
    spec = spec_for_tool(name)
    assert spec is not None
    current = reference_spec(spec, refs)
    ref = canonical_json_object(dict(current.parameters)["ref"])
    assert "anyOf" in ref
    for version in (1, 2, 3):
        old = replace(refs, presentation_version=1 if version == 1 else 2 if version == 2 else 3)
        assert reference_spec(spec, old) == spec


def test_real_native_controls_use_the_original_nodes_and_reject_replaced_nodes(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(CONTROL_URL).ok
    install(browser, '<select aria-label="Package"><option value=small>Small</option><option value=large>Large</option></select>'
        '<label><input type=checkbox>Confirmed</label><button onmouseenter="this.dataset.hovered=1">Preview</button>')
    frozen = observed_snapshot(session, browser)
    for turn, (name, args) in enumerate((
        ("browser_select", {"ref": {"role": "combobox", "name": "Package"}, "values": ["large"]}),
        ("browser_check", {"ref": {"role": "checkbox", "name": "Confirmed"}, "checked": True}),
        ("browser_hover", {"ref": {"role": "button", "name": "Preview"}}),
    ), 1):
        result = program(session, tools, frozen, f"return unwrap(await tools.{name}({canonical_json(args)}));", turn=turn)
        assert result.ok, result.model_text
    assert browser.evaluate("document.querySelector('select').value").observation["value"] == "large"
    assert browser.evaluate("document.querySelector('input').checked").observation["value"] is True
    assert browser.evaluate("document.querySelector('button').dataset.hovered").observation["value"] == "1"
    install(browser, '<button onmouseenter="this.dataset.hovered=1">Preview</button>')
    rejected = program(session, tools, frozen,
        "return unwrap(await tools.browser_hover({ref:{role:'button',name:'Preview'}}));", turn=4)
    assert not rejected.ok
    assert browser.evaluate("document.querySelector('button').dataset.hovered||''").observation["value"] == ""


@pytest.mark.parametrize("case", ["changed", "first_input", "unchanged", "uncertain", "failed_fill", "other_document", "uncommitted"])
def test_query_choices_require_a_new_verified_input_and_committed_receipts(case: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})))
    document: JsonObject = {"url": CONTROL_URL, "loader_id": "current"}
    if case != "first_input":
        record(session, "browser_fill", {"ref": "input", "value": "before"}, document)
    click = record(session, "browser_click", {"ref": "query"}, document)
    if case == "uncertain":
        session._memory_results[click] = replace(session._memory_results[click], ok=False,
            canonical={"value": {"delivery": "uncertain"}})
    filled = record(session, "browser_fill", {"ref": "input", "value": "before" if case == "unchanged" else "after"},
        {**document, "loader_id": "other"} if case == "other_document" else document, ok=case != "failed_fill")
    session._memory_results[filled] = replace(session._memory_results[filled], canonical={"value": {"delivery": "verified"}})
    if case == "uncommitted":
        del session.executed[filled]
    assert _input_after_click(session.executed, session._memory_results, document) is (case in {"changed", "first_input"})


def test_input_after_an_early_click_offers_a_current_query_without_replaying_it(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(CONTROL_URL).ok
    install(browser, '<title>Account records</title><main><input aria-label="Account">'
        '<button onclick="if(document.querySelector(\'input\').value) document.querySelector(\'article\').textContent='
        '\'Requested account record is now ready. Account identifier: \'+document.querySelector(\'input\').value+'
        '\'. The account lookup completed successfully.\'">Lookup</button><article>No record has been loaded. '
        'Enter an account identifier and choose Lookup to retrieve the authorized account record.</article></main>')
    snapshot = observed_snapshot(session, browser)
    clicked = program(session, tools, snapshot,
        "unwrap(await tools.browser_click({ref:{role:'button',name:'Lookup'}}));return unwrap(await tools.browser_read({}));")
    assert clicked.ok, clicked.model_text
    snapshot = observed_snapshot(session, browser)
    filled = program(session, tools, snapshot,
        "return unwrap(await tools.browser_fill({ref:{role:'textbox',name:'Account'},value:'A-19'}));", turn=2)
    assert filled.ok, filled.model_text
    snapshot = observed_snapshot(session, browser)
    offered, hint = query_result_handoff(session, snapshot,
        CompletionContext(browser_document=browser.document_identity()), browser.generation, task_named=False)
    candidate = parse_json_object(hint.splitlines()[-1])
    action = decode_json_object(candidate["call"])
    assert candidate["name"] == "Lookup"
    assert decode_json_object(action["args"])["ref"] == {"role": "button", "name": "Lookup"}
    assert sum(row.call.name == "browser_click" for row in session._memory_results.values()) == 1
    completed = program(session, tools, offered,
        "return unwrap(await tools.browser_click(" + canonical_json(action["args"]) + "));", turn=3)
    assert completed.ok, completed.model_text
    assert any(row.call.name == "browser_read" and row.ok and "A-19" in row.model_text for row in session._memory_results.values())
