from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from codey.agents.consensus import ConsensusResult
from codey.agents.handoff import ConversationSnapshot, render_summary_prompt
from codey.agents.request import AgentRequest
from codey.app import server
from codey.app import task_submit as task_submit
from codey.app.context import AppContext
from codey.operations.task_run import TaskRunDeps
from codey.operations.task_submission import run_task_submission
from codey.research.ledger import ResearchLedger
from codey.research.object_model import ResearchRecord, build_research_record
from codey.research.pipeline import ResearchIterationRun
from codey.research.report_quality import review_report_quality
from codey.research.run_result import ResearchRunResult
from codey.runtime.core.run_result import RunResult
from codey.task.model import TaskSubmission
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    parse_json_object,
    require_json_string,
    require_json_strings,
)
from codey.utils.refs import digest_text

RESEARCH_ITERATION = "codey.operations.research_iteration.run_research_iteration"


class _Provider:
    name = "DeepSeek Web"
    location = "https://chat.deepseek.com/"

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


class _UnavailableSupervisor:
    def prepare_user_selected(self, provider_id: str):
        return provider_id

    def is_available(self, provider_id: str) -> bool:
        return provider_id != "deepseek"

    def select(self, *_args, **_kwargs) -> str:
        return "qwen"

    def needs_canary(self, _provider_id: str) -> bool:
        return False

    def record_failure(self, _provider_id: str, failure):
        return failure


def _runner(
    state: server.AppContext,
    *,
    agent_run=None,
    collect_changes=None,
    run_review=None,
    run_consensus=None,
    run_project_audit=None,
) -> TaskRunDeps:
    return TaskRunDeps(state=state,
        agent_run=agent_run or mock.Mock(return_value=RunResult("done", "done", 1)),
        collect_changes=collect_changes
        or mock.Mock(
            return_value={
                "ok": True,
                "changed_count": 0,
                "files": [],
                "diff": "",
            }
        ),
        run_review=run_review or mock.Mock(return_value=None),
        capture_provider_failure=task_submit.capture_provider_failure,
        run_consensus=run_consensus,
        run_project_audit=run_project_audit,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        workspace_revisions=state.workspace_revisions,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
        runtime_mutations=state.runtime_mutations,
        runtime_effects=state.runtime_effects,
    )


def _trace_payload(state: AppContext, session_id: str, run_id: str) -> JsonObject:
    assert state.run_traces is not None
    path = state.run_traces.path_for(session_id, run_id)
    return parse_json_object(path.read_text(encoding="utf-8"))


def _research_record(project: Path | None = None) -> ResearchRecord:
    url = "https://example.com/helium?token=SECRET_TOKEN"
    summary = (
        "## 结论\n"
        "- Helium supply depends on gas processing. [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source says helium is separated from natural gas streams.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新供应数据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Helium article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search(
        "helium",
        [
            {
                "title": "Helium article",
                "url": url,
                "snippet": "Helium supply.",
            }
        ],
    )
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Helium article",
        text="Helium is separated from natural gas streams.",
    )
    prepared = ledger.prepare_evidence_items(
        [
            {
                "claim": "Helium supply depends on gas processing.",
                "source_url": url,
                "excerpt": "Helium is separated from natural gas streams.",
                "stance": "supports",
            }
        ],
        fallback_sources=[url],
        fallback_claim="Helium supply depends on gas processing.",
        fallback_body="Helium is separated from natural gas streams.",
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-1")
    review = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert review.ok
    return build_research_record(
        question="Research helium",
        summary=summary,
        ledger=ledger,
        review=review,
        run_id="run-record",
        session_id="session-evidence-ledger",
        project=project,
        synthesis_id="synth-1",
        stop_reason="done",
    )


def test_project_run_writes_bounded_trace_without_raw_prompt_or_provider_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state = server.AppContext(root / "state")
        secret_prompt = "SECRET_PROMPT_SHOULD_NOT_BE_SAVED"

        def fake_agent(request: AgentRequest):
            trace = request.trace_recorder
            trace.record_permission_profile("coding_writer")
            trace.record_tool_contract_hash("sha256:" + "a" * 64)
            trace.record_prompt_section("fake_prompt", secret_prompt)
            trace.record_provider_failure(
                "deepseek",
                type(
                    "Failure",
                    (),
                    {
                        "action": "send",
                        "kind": "response_missing",
                        "stage": "completion",
                        "message": "RAW_PROVIDER_ERROR_SHOULD_NOT_BE_SAVED",
                    },
                )(),
            )
            return RunResult("done", "done", 1)

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, agent_run=fake_agent)
            run_task_submission(runner,
                TaskSubmission(
                    "session-trace",
                    str(project),
                    "Build the feature",
                    4,
                    False,
                    "deepseek",
                    intent="project",
                )
            )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, "session-trace", run_id)
        serialized = json.dumps(payload, ensure_ascii=False)

        assert payload["kind"] == "run_trace_manifest"
        assert payload["mode_initial"] == "project"
        assert payload["mode_final"] == "project"
        assert payload["provider_initial"] == "deepseek"
        assert payload["provider_final"] == "deepseek"
        assert payload["permission_profile"] == "coding_writer"
        assert payload["mode_selection"]["source"] == "explicit_user_choice"
        assert payload["model_tool_contract_hash"].startswith("sha256:")
        assert "fake_prompt" in [item["name"] for item in payload["prompt_sections"]]
        assert secret_prompt not in serialized
        assert "RAW_PROVIDER_ERROR_SHOULD_NOT_BE_SAVED" not in serialized


def test_explicit_research_result_writes_structured_trace_refs() -> None:
    """Explicit Research archives bounded report refs without source-body leaks."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state = AppContext(root / "state")
        main_provider = _Provider("ACTION: research\nPLAN: 查最新 storage 方案")
        record = _research_record()

        result = ResearchRunResult(
            question="Research storage",
            summary="done",
            stop_reason="done",
            turns=1,
            notes_created=["note-created"],
            notes_updated=["note-updated"],
            synthesis_id="synth-1",
            opened_sources=[
                {
                    "requested_url": "https://example.com/request",
                    "final_url": "https://example.com/final",
                    "title": "Example Source",
                }
            ],
            research_record=record,
        )

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            runner = _runner(state)
            research_iteration = mock.Mock(return_value=ResearchIterationRun(result=result))
            with mock.patch(RESEARCH_ITERATION, research_iteration):
                run_task_submission(runner,
                    TaskSubmission(
                        "session-research-trace",
                        None,
                        "查一下最新 storage 方案",
                        4,
                        False,
                        "deepseek",
                        intent="research",
                    )
                )

        terminal = decode_json_object(state.run_registry.last_terminal_event())
        run_id = require_json_string(terminal["run_id"], "run_id")
        payload = _trace_payload(state, "session-research-trace", run_id)
        serialized = json.dumps(payload, ensure_ascii=False)
        research_payload = decode_json_object(terminal["research"])

        assert payload["mode_initial"] == "research"
        assert payload["mode_final"] == "research"
        assert payload["permission_profile"] == "research"
        mode_selection = decode_json_object(payload["mode_selection"])
        assert mode_selection["source"] == "explicit_user_choice"
        assert mode_selection["reason_code"] == "intent_selected"
        research_iteration.assert_called_once()
        assert set(require_json_strings(payload["research_note_ids"], "note_ids")) == {
            "note-created", "note-updated", "synth-1"}
        source_refs = payload["research_source_refs"]
        assert isinstance(source_refs, list)
        assert decode_json_object(source_refs[0])["host"] == "example.com"
        pipeline_runs = payload["research_pipeline_runs"]
        assert isinstance(pipeline_runs, list) and len(pipeline_runs) == 1
        assert decode_json_object(pipeline_runs[0])["followup_applied"] is False
        assert payload["research_records"] == [record.to_summary_payload()]
        assert "https://example.com/request" not in serialized
        assert "https://example.com/final" not in serialized
        assert "Example Source" not in serialized
        assert "SECRET_TOKEN" not in serialized
        assert research_payload["pipeline_stop_reason"] == "done"
        assert research_payload["final_evidence_count"] == 0
        assert "followup_applied" not in research_payload
        assert "followup_rounds" not in research_payload
        assert "planner_stop_reason" not in research_payload
        assert "research_record" not in research_payload


def test_research_result_appends_evidence_ledger_without_terminal_payload_change() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state = AppContext(root / "state")
        record = _research_record(project)
        result = ResearchRunResult(
            question="Research helium",
            summary="done",
            stop_reason="done",
            turns=1,
            synthesis_id="synth-1",
            opened_sources=[
                {
                    "requested_url": "https://example.com/helium?token=SECRET_TOKEN",
                    "final_url": "https://example.com/helium?token=SECRET_TOKEN",
                    "title": "Helium article",
                }
            ],
            research_record=record,
        )

        research_iteration = mock.Mock(return_value=ResearchIterationRun(result=result))
        with (
            mock.patch.object(state, "get_provider", return_value=_Provider()),
            mock.patch(RESEARCH_ITERATION, research_iteration),
        ):
            runner = _runner(state)
            run_task_submission(runner,
                TaskSubmission(
                    "session-evidence-ledger",
                    str(project),
                    "Research helium",
                    4,
                    False,
                    "deepseek",
                    intent="research",
                )
            )

        terminal = decode_json_object(state.run_registry.last_terminal_event())
        run_id = require_json_string(terminal["run_id"], "run_id")
        trace_payload = _trace_payload(state, "session-evidence-ledger", run_id)
        terminal_research_payload = decode_json_object(terminal["research"])
        serialized_trace = json.dumps(trace_payload, ensure_ascii=False)

        assert trace_payload["research_evidence_ledgers"] == []
        assert trace_payload["research_records"] == [record.to_summary_payload()]
        assert trace_payload["research_proof_reviews"] == []
        assert trace_payload["research_plans"] == []
        pipeline_runs = trace_payload["research_pipeline_runs"]
        assert pipeline_runs == [
            {
                "followup_applied": False,
                "followup_rounds": 0,
                "stop_reason": "done",
                "planner_stop_reason": "",
                "fresh_source_count": 0,
                "new_evidence_count": 0,
                "final_evidence_count": 0,
                "attempted_fresh_source_count": 0,
                "attempted_new_evidence_count": 0,
            }
        ]

        assert "SECRET_TOKEN" not in serialized_trace
        assert "followup_applied" not in terminal_research_payload
        assert "followup_rounds" not in terminal_research_payload
        assert terminal_research_payload["pipeline_stop_reason"] == "done"
        assert terminal_research_payload["final_evidence_count"] == 0
        assert "planner_stop_reason" not in terminal_research_payload
        assert "research_record" not in terminal_research_payload
        assert "research_proof" not in terminal_research_payload
        assert "research_plan" not in terminal_research_payload
        assert "evidence_ledger" not in terminal_research_payload


def test_hybrid_trace_records_research_and_writer_phases() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
        state = server.AppContext(root / "state")

        # Single-session hybrid: one unified run touches web + project tools.
        # The trace still records both research and writer phases (no secret
        # prompts), but via one session instead of two separate phases.
        def fake_unified(frame, work, hooks, deps, task_kind="", config_result=None):
            from codey.operations.result import ModeOutcome as _Outcome

            trace = getattr(frame, "trace", None)
            if trace is not None:
                try:
                    trace.record_permission_profile("research", phase="research")
                    trace.record_tool_contract_hash("sha256:" + "r" * 64, phase="research")
                    trace.record_prompt_section(
                        "research_outbound_prompt",
                        "SECRET_RESEARCH_PROMPT_SHOULD_NOT_BE_SAVED",
                    )
                    trace.record_permission_profile("coding_writer", phase="writer")
                    trace.record_tool_contract_hash("sha256:" + "w" * 64, phase="writer")
                except Exception:
                    pass
            return _Outcome({
                "type": "task_done", "run_id": frame.run_id, "session_id": frame.request.session_id,
                "summary": "hybrid done", "stop_reason": "done", "turns": 2,
                "max_turns": 4, "provider": frame.provider_id, "mode": "hybrid",
                "receipt": {"display": {"summary": "hybrid done"}},
            })

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, agent_run=lambda req: RunResult("writer done", "done", 1))
            with mock.patch("codey.operations.task_phases.dispatch.run_task_mode",
                            side_effect=fake_unified):
                run_task_submission(runner,
                    TaskSubmission(
                        "session-hybrid-trace",
                        str(project),
                        "Research then edit",
                        4,
                        False,
                        "deepseek",
                        intent="hybrid",
                    )
                )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, "session-hybrid-trace", run_id)
        serialized = json.dumps(payload, ensure_ascii=False)

        assert payload["mode_final"] == "hybrid"
        assert payload["permission_profile"] == "coding_writer"
        assert {item["phase"] for item in payload["permission_profiles"]} >= {"research", "writer"}
        assert {item["profile"] for item in payload["permission_profiles"]} >= {"research", "coding_writer"}
        assert {item["phase"] for item in payload["tool_contracts"]} >= {"research", "writer"}
        assert "SECRET_RESEARCH_PROMPT_SHOULD_NOT_BE_SAVED" not in serialized



def test_secondary_inputs_are_traced_as_prepared_digest_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text("print('old')\n", encoding="utf-8")
        state = server.AppContext(root / "state")
        secret_diff = "SECRET_REVIEW_DIFF_SHOULD_NOT_BE_SAVED"
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": secret_diff,
        }

        def fake_agent(*_args, **_kwargs):
            return RunResult("SECRET_WRITER_SUMMARY_SHOULD_NOT_BE_SAVED", "done", 1, True, True)

        run_review = mock.Mock(return_value=None)
        with (
            mock.patch.object(state, "get_provider", return_value=_Provider()),
            mock.patch(
                "codey.operations.project_review_phase.safe_review_impact_map",
                return_value="SECRET_REVIEW_IMPACT_SHOULD_NOT_BE_SAVED",
            ) as impact_map,
        ):
            runner = _runner(
                state,
                agent_run=fake_agent,
                collect_changes=mock.Mock(return_value=changes),
                run_review=run_review,
            )
            run_task_submission(runner,
                TaskSubmission(
                    "session-secondary-trace",
                    str(project),
                    "Build the feature",
                    4,
                    False,
                    "deepseek",
                    intent="project",
                )
            )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, "session-secondary-trace", run_id)
        names = {item["name"] for item in payload["prompt_sections"]}
        serialized = json.dumps(payload, ensure_ascii=False)

        run_review.assert_called_once()
        impact_map.assert_called_once()
        assert run_review.call_args.kwargs["review_impact_map"] == "SECRET_REVIEW_IMPACT_SHOULD_NOT_BE_SAVED"
        assert {"review_task", "review_writer_summary", "review_diff"}.issubset(names)
        review_sections = [
            item
            for item in payload["prompt_sections"]
            if item["name"] in {"review_task", "review_writer_summary", "review_diff"}
        ]
        assert review_sections
        assert {item["freshness"] for item in review_sections} == {"secondary_input_prepared"}
        assert all(ref.startswith("secondary_input:review:") for item in review_sections for ref in item["source_refs"])
        assert {"phase": "review", "profile": "reviewer"} not in payload["permission_profiles"]
        assert secret_diff not in serialized
        assert "SECRET_WRITER_SUMMARY_SHOULD_NOT_BE_SAVED" not in serialized
        assert "SECRET_REVIEW_IMPACT_SHOULD_NOT_BE_SAVED" not in serialized


def test_chat_workspace_prompt_is_traced_by_digest_without_hidden_consensus() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.AppContext(Path(td) / "state")
        secret_task = "SECRET_CONSENSUS_TASK_SHOULD_NOT_BE_SAVED"
        consensus = mock.Mock(return_value=ConsensusResult("combined", 1))

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, run_consensus=consensus)
            run_task_submission(runner,
                TaskSubmission(
                    "session-consensus-trace",
                    None,
                    secret_task,
                    4,
                    False,
                    "deepseek",
                    intent="chat",
                )
            )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, "session-consensus-trace", run_id)
        names = {item["name"] for item in payload["prompt_sections"]}
        serialized = json.dumps(payload, ensure_ascii=False)

        consensus.assert_not_called()
        assert "consensus_task" not in names
        assert "workspace_outbound_prompt" in names
        consensus_task = next(item for item in payload["prompt_sections"] if item["name"] == "workspace_outbound_prompt")
        assert consensus_task["freshness"] == "provider_send"
        assert consensus_task["source_refs"] == ["provider_send:workspace"]
        assert secret_task not in serialized


def test_chat_outbound_prompt_carries_shared_workspace_kernel_provenance() -> None:
    # Non-consensus chat sends go straight through the runner's own prompt
    # boundary; their outbound row must name the capability that owns it.
    with tempfile.TemporaryDirectory() as td:
        state = server.AppContext(Path(td) / "state")
        consensus = mock.Mock(return_value=None)

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, run_consensus=consensus)
            run_task_submission(runner,
                TaskSubmission(
                    "session-chat-provenance",
                    None,
                    "plain question",
                    4,
                    False,
                    "deepseek",
                    intent="chat",
                )
            )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, "session-chat-provenance", run_id)
        outbound = next(item for item in payload["prompt_sections"] if item["name"] == "workspace_outbound_prompt")

        assert outbound["freshness"] == "provider_send"
        assert outbound["capability_id"] == "agent_runner"
        assert outbound["admission_reason"] == "provider_turn_boundary"
        assert outbound["epoch_id"].startswith("ctx_epoch:")
        assert outbound["source_refs"] == ["provider_send:workspace"]


def test_conversation_handoff_summary_prompt_is_traced_on_rollover() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.AppContext(Path(td) / "state")
        session_id = "session-handoff-summary-trace"
        provider = _Provider(reply='{"current_state":"Keep the plan"}')
        conversation = state.conversation_for(session_id)
        snapshot = ConversationSnapshot(
            mode="chat",
            goal="Keep the existing chat plan",
            provider_id="deepseek",
        )
        from dataclasses import replace

        from codey.workspace.session import session_workspace
        workspace = str(session_workspace(Path(td) / "state", session_id))
        snapshot = replace(snapshot, project=workspace)
        conversation.begin_window("deepseek", "chat", workspace)
        conversation.update_snapshot(snapshot)
        conversation.estimated_context_tokens = conversation.soft_limit
        state.set_provider_session("deepseek", session_id)

        with mock.patch.object(state, "get_provider", return_value=provider):
            runner = _runner(state, run_consensus=mock.Mock(return_value=None))
            run_task_submission(runner,
                TaskSubmission(
                    session_id,
                    None,
                    "continue with the plan",
                    4,
                    False,
                    "deepseek",
                    intent="chat",
                )
            )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, session_id, run_id)
        handoff = next(
            item for item in payload["prompt_sections"] if item["name"] == "conversation_handoff_summary_prompt"
        )

        assert provider.prompts[0] == render_summary_prompt(snapshot)
        assert handoff["freshness"] == "provider_send"
        assert handoff["capability_id"] == "conversation_handoff"
        assert handoff["admission_reason"] == "provider_turn_boundary"
        assert handoff["epoch_id"].startswith("ctx_epoch:")
        assert handoff["source_refs"] == ["provider_send:conversation_handoff_summary"]
        assert handoff["digest"] == digest_text(provider.prompts[0])
        assert "Return only one compact JSON object" not in json.dumps(
            payload,
            ensure_ascii=False,
        )


def test_project_audit_inputs_are_prepared_metadata_not_model_boundary() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
        state = server.AppContext(root / "state")
        secret_task = "SECRET_PROJECT_AUDIT_TASK_SHOULD_NOT_BE_SAVED"
        project_audit = mock.Mock(return_value=())

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, run_project_audit=project_audit)
            run_task_submission(runner,
                TaskSubmission(
                    "session-project-audit-prepared",
                    str(project),
                    secret_task,
                    4,
                    False,
                    "deepseek",
                    intent="project",
                )
            )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, "session-project-audit-prepared", run_id)
        audit_task = next(item for item in payload["prompt_sections"] if item["name"] == "project_audit_task")
        serialized = json.dumps(payload, ensure_ascii=False)

        project_audit.assert_called_once()
        assert audit_task["freshness"] == "secondary_input_prepared"
        assert audit_task["source_refs"] == ["secondary_input:project_audit:task"]
        assert secret_task not in serialized


def test_preflight_provider_switch_is_recorded_as_fallback(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state = server.AppContext(root / "state")
        state.providers.supervisor = _UnavailableSupervisor()
        monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", lambda **_kwargs: {})

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state)
            run_task_submission(runner,
                TaskSubmission(
                    "session-fallback-trace",
                    str(project),
                    "Build the feature",
                    4,
                    False,
                    "deepseek",
                    intent="project",
                )
            )

        run_id = state.run_registry.last_terminal_event()["run_id"]
        payload = _trace_payload(state, "session-fallback-trace", run_id)

        assert payload["provider_initial"] == "deepseek"
        assert payload["provider_final"] == "qwen"
        assert payload["fallbacks"] == [
            {
                "from_provider": "deepseek",
                "to_provider": "qwen",
                "phase": "preflight",
                "reason_code": "unavailable",
            }
        ]
        assert len(payload["policy_decisions"]) == 1
        assert payload["policy_decisions"][0]["subject_ref"].startswith("action:")
        assert payload["policy_decisions"][0] == {
            "kind": "provider_fallback",
            "decision": "allow",
            "guard_id": "default_allow",
            "reason_code": "allowed",
            "phase": "preflight",
            "subject_ref": payload["policy_decisions"][0]["subject_ref"],
        }


def test_forget_conversation_deletes_session_run_traces() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.AppContext(Path(td) / "state")
        assert state.run_traces is not None
        recorder = state.run_traces.open(
            run_id="run-forget",
            session_id="session-forget",
            project=None,
            mode_initial="chat",
            provider_initial="deepseek",
        )
        recorder.finish(status="done")
        path = state.run_traces.path_for("session-forget", "run-forget")
        assert path.exists()

        state.forget_conversation("session-forget")

        assert not path.exists()


def _run_project_task(state: server.AppContext, project: Path, session_id: str, task: str,
                      changes: dict, *, agent_result: RunResult | None = None) -> dict:
    with mock.patch.object(state, "get_provider", return_value=_Provider()):
        runner = _runner(
            state,
            collect_changes=mock.Mock(return_value=changes),
            agent_run=mock.Mock(return_value=agent_result) if agent_result is not None else None,
        )
        run_task_submission(runner,
            TaskSubmission(
                session_id,
                str(project),
                task,
                4,
                False,
                "deepseek",
                intent="project",
            )
        )
    run_id = state.run_registry.last_terminal_event()["run_id"]
    return _trace_payload(state, session_id, run_id)


def test_done_project_run_records_shadow_completion_proof_for_code_change() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        (project / "src").mkdir(parents=True)
        state = server.AppContext(Path(td) / "state")

        payload = _run_project_task(
            state,
            project,
            "session-completion-code",
            "Change the module",
            {
                "ok": True,
                "changed_count": 1,
                "files": [{"path": "src/mod.py", "status": "modified"}],
                "diff": "",
                "mode": "git",
            },
        )

        proofs = payload["completion_proofs"]
        assert len(proofs) == 1
        proof = proofs[0]
        assert proof["domain"] == "coding"
        # No verification candidate covers the change and no tool events were
        # observed locally: the honest shadow status is blocked.
        assert proof["status"] == "blocked"
        assert proof["satisfied"] is False
        assert proof["checks"] == [
            {
                "check_id": "relevant_verification",
                "status": "not_run",
                "reason_code": "no_matching_verification_command",
            }
        ]
        assert any(ref.startswith("ledger:") for ref in proof["external_refs"])
        assert any(ref.startswith("receipt:") for ref in proof["external_refs"])
        assert proof["analysis_run_refs"] == []
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "SECRET" not in serialized


def test_docs_only_done_run_completes_with_limitations() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        state = server.AppContext(Path(td) / "state")

        payload = _run_project_task(
            state,
            project,
            "session-completion-docs",
            "Update the readme",
            {
                "ok": True,
                "changed_count": 1,
                "files": [{"path": "README.md", "status": "modified"}],
                "diff": "",
                "mode": "git",
            },
        )

        proofs = payload["completion_proofs"]
        assert len(proofs) == 1
        proof = proofs[0]
        assert proof["domain"] == "coding"
        assert proof["status"] == "complete_with_limitations"
        assert proof["satisfied"] is False
        assert proof["limitation_refs"] == ["docs_only_change"]
        assert proof["checks"] == [
            {
                "check_id": "relevant_verification",
                "status": "not_applicable",
                "reason_code": "docs_only_change",
            }
        ]


def test_unchanged_done_run_records_one_not_applicable_completion_proof() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        state = server.AppContext(Path(td) / "state")

        payload = _run_project_task(
            state,
            project,
            "session-completion-empty",
            "Answer a question",
            {"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"},
        )

        proofs = payload["completion_proofs"]
        assert len(proofs) == 1
        assert proofs[0]["satisfied"] is True
        assert proofs[0]["checks"] == [{"check_id": "relevant_verification", "status": "not_applicable"}]


def test_interrupted_run_never_records_completion_proof() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        state = server.AppContext(Path(td) / "state")
        payload = _run_project_task(
            state, project, "interrupted-proof", "Answer a question",
            {"ok": True, "changed_count": 0, "files": [], "diff": "", "mode": "git"},
            agent_result=RunResult("Stopped", "stopped", 1),
        )
        assert state.run_registry.last_terminal_event()["stop_reason"] == "stopped"
        assert payload["completion_proofs"] == []
