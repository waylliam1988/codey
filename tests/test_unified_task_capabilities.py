"""Authorization and delivery obligations compose across activity labels."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from codey.knowledge.store import KnowledgeStore
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel_facts import bind_source_identity, project_execution_record, project_result_observation
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.research.research_completion_checks import report_completes_task
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy, build_task_policy
from codey.protocols.text_turn import decode_text_turn
from codey.research.answer_contract import answer_scope, submit_answer
from codey.research.task_mode import whole_research
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject


@dataclass(frozen=True)
class Request:
    project: str = "workspace"
    strict_research: bool = True
    project_changes_required: bool = True
    requested_capabilities: tuple[str, ...] = ()
    denied_capabilities: tuple[str, ...] = ()


@pytest.mark.parametrize("strict", [False, True])
def test_same_authorization_has_same_capabilities_in_every_activity(strict: bool) -> None:
    policy = build_task_policy(Request(strict_research=strict), task_kind="project")
    inventories: list[tuple[str, ...]] = []
    for kind in ("project", "research", "hybrid", "chat"):
        session = TaskSession(policy, task_kind=kind, project_changes_required=True)
        snapshot = offer_browser_tools(build_turn_snapshot(session), session)
        assert {"read_file", "edit", "run", "browser_navigate", "browser_snapshot"} <= set(snapshot.inventory_names)
        assert {"browser_snapshot", "browser_fill", "browser_click", "browser_read"} <= set(snapshot.tool_names)
        assert "run" in snapshot.nested_tool_names
        assert whole_research(session) is strict
        assert restore_snapshot(snapshot_description(snapshot)) == snapshot
        inventories.append(snapshot.inventory_names)
    assert all(names == inventories[0] for names in inventories)


@pytest.mark.parametrize("kind", ["project", "research", "hybrid", "chat"])
def test_report_requirement_preserves_explicit_project_work_and_denials(kind: str) -> None:
    policy = build_task_policy(Request(), task_kind=kind)
    assert policy.allows("project.write") and policy.allows("shell.approval")
    assert set(RESEARCH_ANSWER_REQUIRED_CHECKS) <= set(policy.required_checks)
    pure = build_task_policy(Request(project_changes_required=False), task_kind=kind)
    assert not pure.allows("project.write")
    denied = build_task_policy(Request(denied_capabilities=("project.write", "browser.interact")), task_kind=kind)
    session = TaskSession(denied, task_kind=kind)
    snapshot = build_turn_snapshot(session)
    assert not {"edit", "browser_click", "browser_navigate"} & set(snapshot.inventory_names)
    readonly = build_task_policy(Request(), task_kind="planning")
    assert not readonly.allows("project.write") and not readonly.allows("browser.interact")


def test_missing_legacy_research_plan_blocks_only_its_question_bound_tools() -> None:
    policy = TaskPolicy(frozenset({"control", "project.read", "project.write", "project.verify",
                                   "web.read", "knowledge.read", "browser.observe", "browser.interact"}),
                        strict_research=True)
    session = TaskSession(policy, task_kind="project")
    snapshot = build_turn_snapshot(session)
    assert {"research_plan", "edit", "run", "browser_snapshot"} <= set(snapshot.inventory_names)
    assert "save_finding" not in snapshot.inventory_names
    assert "open_url" not in snapshot.inventory_names


def test_codemode_verification_uses_verify_grant_independently_of_edit_grant() -> None:
    from codey.operations.browser.catalog import snapshot_catalog

    policy = TaskPolicy(frozenset({"control", "project.read", "project.verify"}))
    session = TaskSession(policy, task_kind="research")
    snapshot = build_turn_snapshot(session)
    assert "run" in snapshot.nested_tool_names
    catalog = snapshot_catalog(snapshot)
    assert "run" in {entry.name for entry in catalog.entries}
    assert "edit" not in {entry.name for entry in catalog.entries}
    assert "run" not in {entry.name for entry in snapshot_catalog(snapshot, allow_writes=False).entries}


class Source:
    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return []

    def fetch(self, url: str) -> dict[str, object]:
        return {"status": "ok", "url": url, "title": "Reference", "truncated": False,
                "text": "Readers and a writer may run concurrently. Only one writer at a time."}


@pytest.mark.usefixtures("public_source_dns")
def test_report_cannot_finish_required_implementation_or_failed_verification(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path / "knowledge")) as store:
        tools = ResearchTools(Source(), store, None)
        policy = build_task_policy(Request(), task_kind="project")
        session = TaskSession(policy, task_kind="project", project_changes_required=True)
        delegate = ExecutionDelegate(session=session, research_tools=tools)
        call = ToolCall("open_url", {"url": "https://example.com/reference"})
        result, ok, _ = delegate.execute(call)
        assert ok, result.model_text
        result = bind_source_identity(session, call, result, ok=True)
        project_result_observation(session, call, result, ok=True, research_ledger=tools.ledger)
        project_execution_record(session, "read", call, result, ok=True)
        session._memory_results["read"] = result
        scope = answer_scope(session)
        args: JsonObject = {"state": "answered", "markdown": "Only one writer at a time [e1]."}
        session._memory_results["report"] = ToolResult(ToolCall("submit_research_answer", args), "Recorded", ok=True,
            canonical={"research_submission": submit_answer(scope, args)})
        context = CompletionContext(research_ledger=tools.ledger, knowledge_store=store)
        assert not report_completes_task(session)
        assert not evaluate(session, "Implemented the requested behavior.", context=context).complete
        snapshot = build_turn_snapshot(session)
        assert {"edit", "run", "done", "submit_research_answer"} <= set(snapshot.inventory_names)
        proposal = normalize_turn(decode_text_turn('{"tool":"done","args":{"summary":"Implementation ready."}}'),
                                  snapshot=snapshot)
        assert not proposal.protocol_error
        session.record_edit("feature.py")
        session.set_workspace_state(1, "sha256:" + "a" * 64)
        session.record_verification("python -m pytest tests/test_feature.py", 1, False,
                                    exit_code=1, workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
        rejected = evaluate(session, "Implementation ready.", context=context)
        assert not rejected.complete
        session.record_verification("python -m pytest tests/test_feature.py", 1, True,
                                    exit_code=0, workspace_revision=1, workspace_fingerprint="sha256:" + "a" * 64)
        accepted = evaluate(session, "Implementation verified.", context=context)
        assert accepted.complete, accepted.followup
        assert "Implementation verified." in accepted.final_text
        assert "Only one writer at a time" in accepted.final_text
        assert accepted.research_report is not None
        pure = replace(session, policy=replace(policy, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS),
                       project_changes_required=False, edited_files={}, verifications=[])
        assert report_completes_task(pure)


def test_outer_and_nested_contracts_share_descriptions_and_keep_full_signatures() -> None:
    from codey.codemode.catalog import render_declarations
    from codey.operations.browser.catalog import snapshot_catalog
    session = TaskSession(build_task_policy(Request(), task_kind="project"), project_changes_required=True)
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    outer = next(spec for spec in snapshot.model_specs if spec.name == "read_file")
    code = next(spec for spec in snapshot.model_specs if spec.name == "codemode")
    assert snapshot.contract_text.count(outer.description) == 1
    assert "Also callable as tools.NAME" in code.declarations
    signatures = render_declarations(snapshot_catalog(snapshot))
    assert "read_file(args:" in signatures and "browser_snapshot(args:" in signatures
    assert restore_snapshot(snapshot_description(snapshot)) == snapshot


@pytest.mark.usefixtures("public_source_dns")
def test_real_kernel_continues_from_report_to_file_edit_and_actual_verification(tmp_path: Path) -> None:
    import json

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.operations.provider_session import ProviderAdapter
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_loop import run_task_kernel
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.workspace.revision import WorkspaceRevisionStore

    project = tmp_path / "project"
    project.mkdir()
    (project / "result.json").write_text('{"amount":0,"code":"unknown"}', encoding="utf-8")
    (project / "test_result.py").write_text(
        "import json\nfrom pathlib import Path\ndef test_result():\n"
        "    assert json.loads(Path('result.json').read_text()) == {'amount':173,'code':'QUARTZ_851'}\n", encoding="utf-8")
    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.write", "project.read", "project.write", "project.verify"}),
        strict_research=True, required_checks=(*RESEARCH_ANSWER_REQUIRED_CHECKS, "project_changes_required"))
    task = "Research the fixture, submit its report, update result.json and verify the change."
    session = TaskSession(policy, task_kind="project", project=str(project), task_text=task,
                          project_changes_required=True, max_turns=8)

    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        def send(self, prompt: str, **options: object) -> str:
            self.calls += 1
            args: JsonObject
            if self.calls == 1:
                name = "open_url"
                args = {"url": "https://example.com/reference"}
            elif self.calls == 2:
                name = "submit_research_answer"
                args = {"state": "answered", "markdown": "Only one writer at a time [e1]."}
            elif self.calls == 3:
                assert not session.edited_files
                assert any(result.call.name == "submit_research_answer" for result in session._memory_results.values())
                name = "read_file"
                args = {"path": "result.json"}
            elif self.calls == 4:
                ref = next(ref for ref, result in reversed(tuple(session._memory_results.items())) if result.call.name == "read_file")
                name = "edit"
                args = {"path": "result.json", "base_result_ref": ref,
                                      "content": '{"amount":173,"code":"QUARTZ_851"}'}
            elif self.calls == 5:
                name = "run"
                args = {"path": ".", "command": "python -m pytest test_result.py -q", "run_in_background": False}
            elif self.calls == 6:
                assert session.verifications and session.verifications[-1]["passed"]
                name = "done"
                args = {"summary": "Updated result.json; the fixture regression passed."}
            else:
                raise AssertionError(f"unexpected extra model request {self.calls}: {prompt[-500:]}")
            return json.dumps({"tool": name, "args": args})

    with closing(DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))) as durable, closing(KnowledgeStore(tmp_path / "knowledge")) as store:
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text=task, provider_snapshot={})
        durable.claim_task("r")
        durable.start_task("r", state={})
        durable.record_task_policy("r", policy.to_payload())
        line = RuntimeMutationLine(durable=durable)
        line.accept_operation(session_id="s", run_id="r", provider_id="fixture", turn_budget=8,
                              max_repair_rounds=0, task_kind="project")
        line.mark_writer_running("s", "r", provider_id="fixture")
        outputs = ManagedOutputStore(tmp_path / "outputs")
        sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="fixture", durable=durable, managed_outputs=outputs)
        revisions = WorkspaceRevisionStore(tmp_path / "state")
        current = revisions.current_state(project)
        session.set_workspace_state(current.revision, current.fingerprint)
        provider = Provider()
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(ProviderAdapter(provider), run_id="r", provider_id="fixture", user_task=task),
            execution=KernelExecutionDeps(intent_sink=sink, project_path=project,
                research_tools=ResearchTools(Source(), store, None), workspace_revision_store=revisions),
            observation=KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
                durable=durable, session_id="s", task_id="r", sink=sink))))
        assert result.completed, result.summary
        assert provider.calls == 6
        assert json.loads((project / "result.json").read_text(encoding="utf-8")) == {"amount": 173, "code": "QUARTZ_851"}
        assert "fixture regression passed" in result.summary and "Only one writer" in result.summary


@pytest.mark.usefixtures("public_source_dns")
def test_research_entry_publishes_only_report_and_preserves_project_delivery(tmp_path: Path) -> None:
    import json
    import re

    from codey.app.context import AppContext
    from codey.completion.contract import CompletionProof
    from codey.operations.research_flow import ResearchFlowDeps
    from codey.operations.research_iteration import run_research_iteration
    from codey.providers.base import result_metadata_from_text
    from codey.research.answer_report import AnswerReport
    from codey.research.report_projection import validate_accepted_projection

    project = tmp_path / "project"
    project.mkdir()
    (project / "result.json").write_text('{"amount":0}', encoding="utf-8")
    (project / "test_result.py").write_text(
        "import json\nfrom pathlib import Path\ndef test_result():\n"
        "    assert json.loads(Path('result.json').read_text()) == {'amount':173}\n", encoding="utf-8")
    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.write", "project.read", "project.write", "project.verify"}),
        strict_research=True, required_checks=(*RESEARCH_ANSWER_REQUIRED_CHECKS, "project_changes_required"))
    task = "Research the reference, submit its report, update result.json and verify it."

    class Provider:
        calls = 0

        def new_chat(self) -> None:
            pass

        def send(self, prompt: str, **options: object) -> str:
            self.calls += 1
            name: str
            args: JsonObject
            if self.calls == 1:
                name = "open_url"
                args = {"url": "https://example.com/reference"}
            elif self.calls == 2:
                name = "submit_research_answer"
                args = {"state": "answered", "markdown": "Only one writer at a time [e1]."}
            elif self.calls == 3:
                name = "read_file"
                args = {"path": "result.json"}
            elif self.calls == 4:
                metadata = [result_metadata_from_text(match[0]) for match in
                            re.finditer(r"Result metadata:\n[^\n]+", prompt)]
                ref = next(row.result_ref for row in reversed(metadata) if row is not None and row.tool == "read_file")
                name = "edit"
                args = {"path": "result.json", "base_result_ref": ref, "content": '{"amount":173}'}
            elif self.calls == 5:
                name = "run"
                args = {"path": ".", "command": "python -m pytest test_result.py -q", "run_in_background": False}
            elif self.calls == 6:
                name = "done"
                args = {"summary": "Updated result.json; the fixture regression passed."}
            else:
                raise AssertionError(f"unexpected extra request {self.calls}: {prompt[-800:]}")
            return json.dumps({"tool": name, "args": args})

    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state, closing(
            KnowledgeStore(tmp_path / "knowledge")) as store:
        durable = state.durable
        durable.admit_input(conversation_id="s", request_id="input", task_id="r", text=task, provider_snapshot={})
        durable.claim_task("r")
        durable.start_task("r", state={})
        durable.record_task_policy("r", policy.to_payload())
        state.runtime_mutations.accept_operation(session_id="s", run_id="r", provider_id="fixture", turn_budget=8,
                                                 max_repair_rounds=0, task_kind="research")
        state.runtime_mutations.mark_writer_running("s", "r", provider_id="fixture")
        provider = Provider()
        deps = ResearchFlowDeps(state, store, Source, managed_outputs=state.managed_outputs,
                                runtime_mutations=state.runtime_mutations)
        result = run_research_iteration(deps, provider=provider, session_id="s", project=str(project), task=task,
            max_turns=8, on_event=lambda _event: None, stop_flag=None, provider_id="fixture", run_id="r",
            chat_handoff="", trace_recorder=None, search=Source(), task_policy=policy).result
        assert result.stop_reason == "done", result.summary
        assert provider.calls == 6
        compiled: object = result.accepted_report
        assert isinstance(compiled, AnswerReport)
        assert "fixture regression passed" in result.summary and result.summary.endswith(compiled.text)
        note = store.read_note(result.synthesis_id)
        assert note is not None and note.body == compiled.text
        validate_accepted_projection(result, run_id="r", session_id="s", project=str(project))
        proof: object = result.completion_proof
        assert isinstance(proof, CompletionProof) and proof.satisfied
        assert json.loads((project / "result.json").read_text(encoding="utf-8")) == {"amount": 173}
