from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from codey import server
from codey.consensus import ConsensusResult
from codey.agent import RunResult
from codey.research.runner import ResearchRunResult
from codey import task_runner as task_runner_module
from codey.task_runner import TaskRequest, TaskRunner


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
    state: server.State,
    *,
    agent_run=None,
    collect_changes=None,
    run_review=None,
    run_consensus=None,
    run_project_audit=None,
    router_provider_factory=None,
) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=agent_run or mock.Mock(return_value=RunResult("done", "done", 1)),
        collect_changes=collect_changes
        or mock.Mock(return_value={
            "ok": True,
            "changed_count": 0,
            "files": [],
            "diff": "",
        }),
        run_review=run_review or mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        run_consensus=run_consensus,
        run_project_audit=run_project_audit,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
        ghost_router_provider_factory=router_provider_factory,
    )


def _trace_payload(state: server.State, session_id: str, run_id: str) -> dict:
    assert state.run_traces is not None
    path = state.run_traces.path_for(session_id, run_id)
    return json.loads(path.read_text(encoding="utf-8"))


def test_project_run_writes_bounded_trace_without_raw_prompt_or_provider_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state = server.State(root / "state")
        secret_prompt = "SECRET_PROMPT_SHOULD_NOT_BE_SAVED"

        def fake_agent(*_args, **kwargs):
            trace = kwargs["trace_recorder"]
            trace.record_permission_profile("coding_writer")
            trace.record_tool_contract_hash("sha256:" + "a" * 64)
            trace.record_prompt_section("fake_prompt", secret_prompt)
            trace.record_provider_failure(
                "deepseek",
                type("Failure", (), {
                    "action": "send",
                    "kind": "response_missing",
                    "stage": "completion",
                    "message": "RAW_PROVIDER_ERROR_SHOULD_NOT_BE_SAVED",
                })(),
            )
            return RunResult("done", "done", 1)

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, agent_run=fake_agent)
            runner.run(TaskRequest(
                "session-trace",
                str(project),
                "Build the feature",
                4,
                False,
                "deepseek",
                intent="project",
            ))
            state.wait_for_ghost_sleep(timeout=2)

        run_id = state.last_terminal_event["run_id"]
        payload = _trace_payload(state, "session-trace", run_id)
        serialized = json.dumps(payload, ensure_ascii=False)

        assert payload["kind"] == "run_trace_manifest"
        assert payload["mode_initial"] == "project"
        assert payload["mode_final"] == "project"
        assert payload["provider_initial"] == "deepseek"
        assert payload["provider_final"] == "deepseek"
        assert payload["permission_profile"] == "coding_writer"
        assert payload["router"]["source"] == "explicit_user_choice"
        assert payload["model_tool_contract_hash"].startswith("sha256:")
        assert "fake_prompt" in [item["name"] for item in payload["prompt_sections"]]
        assert secret_prompt not in serialized
        assert "RAW_PROVIDER_ERROR_SHOULD_NOT_BE_SAVED" not in serialized


def test_auto_router_and_research_result_write_structured_trace_refs() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state = server.State(root / "state")
        route_provider = _Provider('{"mode":"research","confidence":0.92,"reason":"fresh info"}')
        main_provider = _Provider()

        result = ResearchRunResult(
            question="Research storage",
            summary="done",
            stop_reason="done",
            turns=1,
            notes_created=["note-created"],
            notes_updated=["note-updated"],
            synthesis_id="synth-1",
            opened_sources=[{
                "requested_url": "https://example.com/request",
                "final_url": "https://example.com/final",
                "title": "Example Source",
            }],
        )

        def router_factory(_provider_id: str):
            return route_provider

        with mock.patch.object(state, "get_provider", return_value=main_provider):
            runner = _runner(state, router_provider_factory=router_factory)
            runner._run_research_task = mock.Mock(return_value=result)
            runner.run(TaskRequest(
                "session-research-trace",
                None,
                "查一下最新 storage 方案",
                4,
                False,
                "deepseek",
                intent="auto",
            ))
            state.wait_for_ghost_sleep(timeout=2)

        run_id = state.last_terminal_event["run_id"]
        payload = _trace_payload(state, "session-research-trace", run_id)
        serialized = json.dumps(payload, ensure_ascii=False)

        assert payload["mode_initial"] == "chat"
        assert payload["mode_final"] == "research"
        assert payload["permission_profile"] == "research"
        assert payload["router"]["source"] == "auto_router"
        assert payload["router"]["reason_code"] == "accepted"
        assert payload["router"]["selected_mode"] == "research"
        assert set(payload["research_note_ids"]) == {"note-created", "note-updated", "synth-1"}
        assert payload["research_source_refs"][0]["host"] == "example.com"
        assert "https://example.com/request" not in serialized
        assert "https://example.com/final" not in serialized
        assert "Example Source" not in serialized


def test_hybrid_trace_records_research_and_writer_phases() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text("print('hi')\n", encoding="utf-8")
        state = server.State(root / "state")

        def fake_agent(*_args, **kwargs):
            trace = kwargs["trace_recorder"]
            trace.record_permission_profile("coding_writer", phase="writer")
            trace.record_tool_contract_hash("sha256:" + "w" * 64, phase="writer")
            return RunResult("writer done", "done", 1)

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, agent_run=fake_agent)

            def fake_research_task(**kwargs):
                trace = kwargs["trace_recorder"]
                trace.record_permission_profile("research", phase="research")
                trace.record_tool_contract_hash("sha256:" + "r" * 64, phase="research")
                trace.record_prompt_section(
                    "research_outbound_prompt",
                    "SECRET_RESEARCH_PROMPT_SHOULD_NOT_BE_SAVED",
                )
                return ResearchRunResult(
                    question="Research first",
                    summary="research done",
                    stop_reason="done",
                    turns=1,
                    synthesis_id="synth-hybrid",
                )

            runner._run_research_task = mock.Mock(side_effect=fake_research_task)
            runner.run(TaskRequest(
                "session-hybrid-trace",
                str(project),
                "Research then edit",
                4,
                False,
                "deepseek",
                intent="hybrid",
            ))
            state.wait_for_ghost_sleep(timeout=2)

        run_id = state.last_terminal_event["run_id"]
        payload = _trace_payload(state, "session-hybrid-trace", run_id)
        serialized = json.dumps(payload, ensure_ascii=False)

        assert payload["mode_final"] == "hybrid"
        assert payload["permission_profile"] == "coding_writer"
        assert {item["phase"] for item in payload["permission_profiles"]} >= {"research", "writer"}
        assert {item["profile"] for item in payload["permission_profiles"]} >= {"research", "coding_writer"}
        assert {item["phase"] for item in payload["tool_contracts"]} >= {"research", "writer"}
        assert "SECRET_RESEARCH_PROMPT_SHOULD_NOT_BE_SAVED" not in serialized


def test_local_context_trace_skips_scanning_when_trace_is_missing() -> None:
    class _ExplodingContext:
        @property
        def selected_nodes(self):  # pragma: no cover - exercised through early return
            raise AssertionError("should not inspect local contexts without trace")

        @property
        def selected_items(self):  # pragma: no cover - exercised through early return
            raise AssertionError("should not inspect local contexts without trace")

    task_runner_module._record_local_context_trace(None, _ExplodingContext())


def test_secondary_inputs_are_traced_as_prepared_digest_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text("print('old')\n", encoding="utf-8")
        state = server.State(root / "state")
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
                "codey.task_runner.safe_review_impact_map",
                return_value="SECRET_REVIEW_IMPACT_SHOULD_NOT_BE_SAVED",
            ) as impact_map,
        ):
            runner = _runner(
                state,
                agent_run=fake_agent,
                collect_changes=mock.Mock(return_value=changes),
                run_review=run_review,
            )
            runner.run(TaskRequest(
                "session-secondary-trace",
                str(project),
                "Build the feature",
                4,
                False,
                "deepseek",
                intent="project",
            ))
            state.wait_for_ghost_sleep(timeout=2)

        run_id = state.last_terminal_event["run_id"]
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


def test_chat_consensus_inputs_are_traced_by_digest_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(Path(td) / "state")
        secret_task = "SECRET_CONSENSUS_TASK_SHOULD_NOT_BE_SAVED"
        consensus = mock.Mock(return_value=ConsensusResult("combined", 1))

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, run_consensus=consensus)
            runner.run(TaskRequest(
                "session-consensus-trace",
                None,
                secret_task,
                4,
                False,
                "deepseek",
                intent="chat",
            ))
            state.wait_for_ghost_sleep(timeout=2)

        run_id = state.last_terminal_event["run_id"]
        payload = _trace_payload(state, "session-consensus-trace", run_id)
        names = {item["name"] for item in payload["prompt_sections"]}
        serialized = json.dumps(payload, ensure_ascii=False)

        consensus.assert_called_once()
        assert "consensus_task" in names
        assert "chat_outbound_prompt" not in names
        consensus_task = next(item for item in payload["prompt_sections"] if item["name"] == "consensus_task")
        assert consensus_task["freshness"] == "secondary_input_prepared"
        assert consensus_task["source_refs"] == ["secondary_input:consensus:task"]
        assert secret_task not in serialized


def test_project_audit_inputs_are_prepared_metadata_not_model_boundary() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        (project / "app.py").write_text("print('hello')\n", encoding="utf-8")
        state = server.State(root / "state")
        secret_task = "SECRET_PROJECT_AUDIT_TASK_SHOULD_NOT_BE_SAVED"
        project_audit = mock.Mock(return_value=())

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state, run_project_audit=project_audit)
            runner.run(TaskRequest(
                "session-project-audit-prepared",
                str(project),
                secret_task,
                4,
                False,
                "deepseek",
                intent="project",
            ))
            state.wait_for_ghost_sleep(timeout=2)

        run_id = state.last_terminal_event["run_id"]
        payload = _trace_payload(state, "session-project-audit-prepared", run_id)
        audit_task = next(
            item
            for item in payload["prompt_sections"]
            if item["name"] == "project_audit_task"
        )
        serialized = json.dumps(payload, ensure_ascii=False)

        project_audit.assert_called_once()
        assert audit_task["freshness"] == "secondary_input_prepared"
        assert audit_task["source_refs"] == ["secondary_input:project_audit:task"]
        assert secret_task not in serialized


def test_preflight_provider_switch_is_recorded_as_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state = server.State(root / "state")
        state.provider_supervisor = _UnavailableSupervisor()

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner = _runner(state)
            runner.run(TaskRequest(
                "session-fallback-trace",
                str(project),
                "Build the feature",
                4,
                False,
                "deepseek",
                intent="project",
            ))
            state.wait_for_ghost_sleep(timeout=2)

        run_id = state.last_terminal_event["run_id"]
        payload = _trace_payload(state, "session-fallback-trace", run_id)

        assert payload["provider_initial"] == "deepseek"
        assert payload["provider_final"] == "qwen"
        assert payload["fallbacks"] == [{
            "from_provider": "deepseek",
            "to_provider": "qwen",
            "phase": "preflight",
            "reason_code": "unavailable",
        }]


def test_forget_conversation_deletes_session_run_traces() -> None:
    with tempfile.TemporaryDirectory() as td:
        state = server.State(Path(td) / "state")
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
