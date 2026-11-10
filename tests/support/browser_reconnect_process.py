"""Separate Codey processes sharing only production durable receipts and live CDP."""
from __future__ import annotations

import argparse
import faulthandler
import os
from pathlib import Path

from playwright.sync_api import Route

from codey.automation.browser_session import BrowserSession
from codey.codemode.kernel import build_execute_turn_executor
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_execution import close_research_tools
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.requests import ProviderRequest
from codey.providers.transport import ProviderTransport
from codey.research.browser_search import BrowserSearchProvider
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, decode_json_object, require_json_string

URL = "https://example.com/reconnect-article"
HTML = """<article>Original article amount is 17 grams.</article>
<label>Draft<textarea></textarea></label><button onclick="this.textContent='Clicked'">Load</button>"""


class FixtureBrowser(BrowserSearchProvider):
    def _guard_request(self, route: Route) -> None:
        route.fulfill(status=200, content_type="text/html", body=HTML)


class NoModel(ProviderTransport):
    provider = object()

    def execute_request(self, request: ProviderRequest) -> object:
        raise AssertionError("receipt recovery must not call a model")


class ProcessArguments(argparse.Namespace):
    stage: str
    state: Path
    port: int
    expected_failure: str
    read_failure: str


def resources(state: Path, *, initialize: bool) -> KernelEffectSink:
    state.mkdir(parents=True, exist_ok=True)
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    mutations = RuntimeMutationLine(durable=durable)
    if initialize:
        durable.admit_input(conversation_id="s", request_id="request-r", task_id="r",
            text="Recover the existing browser", provider_snapshot={"provider_id": "local", "model_id": "fixture"},
            task_state={"task_kind": "chat"})
        durable.start_task("r", state={"task_kind": "chat"})
        mutations.accept_operation(session_id="s", run_id="r", project="", provider_id="local",
                                   turn_budget=20, max_repair_rounds=1, task_kind="chat")
        mutations.mark_writer_running("s", "r", provider_id="local")
    return KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local",
                            managed_outputs=ManagedOutputStore(state), durable=durable)


def new_session() -> TaskSession:
    return TaskSession(policy=TaskPolicy(grants=frozenset({
        "control", "web.read", "browser.observe", "browser.interact", "browser.effect",
    })), task_kind="chat", max_turns=20)


def observe(result: ToolResult) -> JsonObject:
    assert result.ok, result.model_text
    return decode_json_object(result.canonical["value"])


def element_ref(observation: JsonObject, role: str) -> str:
    elements = observation["elements"]
    assert isinstance(elements, list)
    for value in elements:
        row = decode_json_object(value)
        if row["role"] == role:
            return require_json_string(row["ref"], "ref")
    raise AssertionError(f"no {role} in snapshot")


def initialize(state: Path, port: int) -> None:
    sink = resources(state, initialize=True)
    session = new_session()
    provider = FixtureBrowser(headless=False, isolated=False, launch=False,
                              require_existing_session=True, cdp_port=port)
    tools = ResearchTools(search=provider, store=None, changes=None, browser_session=BrowserSession(provider))
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="r", effect_scope="task",
        turn=1, research_tools=tools, intent_sink=sink, managed_outputs=sink.managed_outputs, session_id="s")
    code = """
    unwrap(await tools.browser_navigate({url: inputs.url}));
    unwrap(await tools.browser_read({}));
    unwrap(await tools.browser_tab_new({url: inputs.url}));
    unwrap(await tools.browser_tab_new({url: inputs.url}));
    unwrap(await tools.browser_tab_switch({index: 0}));
    let observation = unwrap(await tools.browser_snapshot({}));
    const ref = observation.elements.find(item => item.role === 'textbox').ref;
    unwrap(await tools.browser_fill({ref, value: 'Unsaved draft survives Codey restart'}));
    observation = unwrap(await tools.browser_snapshot({}));
    unwrap(await tools.browser_click({ref: observation.elements.find(item => item.role === 'button').ref}));
    return unwrap(await tools.browser_snapshot({}));
    """
    results = execute_turn(session, [ToolCall("codemode", {"code": code, "inputs": {"url": URL}}, call_id="parent")],
        snapshot=snapshot, run_id="r", session_id="s", effect_scope="task", turn=1, research_tools=tools,
        executors={"codemode": parent}, intent_sink=sink, managed_outputs=sink.managed_outputs)
    assert results[0].ok, results[0].model_text
    assert session.source_ids == {"s1": URL}
    sink.durable.close()
    # Simulate process loss after commit, without terminal task cleanup.
    os._exit(0)


def recover(state: Path, port: int, expected_failure: str, read_failure: str, *, cleanup_only: bool) -> None:
    sink = resources(state, initialize=False)
    session = new_session()
    provider = FixtureBrowser(headless=False, isolated=False, launch=False,
                              require_existing_session=True, cdp_port=port)
    tools = ResearchTools(search=provider, store=None, changes=None)
    context = KernelContext(session, KernelRunRequest(
        transport=KernelTransportDeps(provider=NoModel(), run_id="r", effect_scope="task", provider_id="local"),
        execution=KernelExecutionDeps(intent_sink=sink, research_tools=tools)))
    context.restore_facts()
    assert provider._session is None  # Projection does not connect or navigate.
    assert session.source_ids == {"s1": URL}
    assert len(tools.ledger.opened_sources) == 1
    original = next(result for result in session._memory_results.values() if result.call.name == "codemode")

    def forbidden_replay(call: ToolCall) -> ToolResult:
        raise AssertionError("a committed browser mutation must not execute twice")

    replay = execute_turn(session, [original.call], snapshot=build_turn_snapshot(session),
        run_id="r", turn=1, effect_scope="task", research_tools=tools, executors={"codemode": forbidden_replay})[0]
    assert replay.ok, replay.model_text
    assert provider._session is None
    if cleanup_only:
        close_research_tools(tools)
        sink.durable.close()
        return
    historical = [row for row in session._memory_results.values() if row.call.name == "browser_snapshot"][-1]
    old_ref = element_ref(observe(historical), "button")
    turn = 1

    def execute(name: str, args: JsonObject) -> ToolResult:
        nonlocal turn
        turn += 1
        return execute_turn(session, [ToolCall(name, args)], snapshot=build_turn_snapshot(session), run_id="r",
            turn=turn, effect_scope="task", research_tools=tools, nested=True)[0]

    try:
        first = execute("browser_snapshot", {})
        if expected_failure:
            assert not first.ok and expected_failure in first.model_text, first.model_text
            assert provider._session is None
            # Even navigate/new-tab must not silently attach another browser/page.
            for name in ("browser_navigate", "browser_tab_new"):
                failed = execute(name, {"url": URL})
                assert not failed.ok and expected_failure in failed.model_text, failed.model_text
            return
        if read_failure:
            assert first.ok, first.model_text
            rejected = execute("browser_read", {})
            assert not rejected.ok and read_failure in rejected.model_text, rejected.model_text
            assert len(tools.ledger.opened_sources) == 1
            assert session.source_ids == {"s1": URL}
            return
        current = observe(first)
        assert "Clicked 1" in str(current)
        assert "Unsaved draft survives Codey restart" in str(current)
        fresh_ref = element_ref(current, "button")
        assert fresh_ref != old_ref
        stale = execute("browser_click", {"ref": old_ref})
        assert not stale.ok
        assert decode_json_object(stale.canonical["value"])["reason"] == "stale_ref"
        assert execute("browser_click", {"ref": fresh_ref}).ok
        assert "Clicked 2" in str(observe(execute("browser_snapshot", {})))
        tabs = observe(execute("browser_tab_list", {}))["tabs"]
        assert isinstance(tabs, list)
        source_tab = next(decode_json_object(row) for row in tabs if decode_json_object(row)["url"] == URL)
        assert execute("browser_tab_switch", {"index": source_tab["index"]}).ok
        read = execute("browser_read", {})
        assert read.ok and "17 grams" in read.model_text, read.model_text
        assert len(tools.ledger.opened_sources) == 1
    finally:
        close_research_tools(tools)
        sink.durable.close()


def main() -> None:
    faulthandler.dump_traceback_later(30)
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("initialize", "recover", "cleanup"))
    parser.add_argument("state", type=Path)
    parser.add_argument("port", type=int)
    parser.add_argument("--expected-failure", default="")
    parser.add_argument("--read-failure", default="")
    args = ProcessArguments()
    parser.parse_args(namespace=args)
    if args.stage == "initialize":
        initialize(args.state, args.port)
    else:
        recover(args.state, args.port, args.expected_failure, args.read_failure, cleanup_only=args.stage == "cleanup")


if __name__ == "__main__":
    main()
