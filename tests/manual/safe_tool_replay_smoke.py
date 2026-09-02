"""Deterministic smoke test for safe tool replay and resume recovery (0.5.4).

Verifies the 0.5.4 crash-recovery lifecycle:
1. Pending safe tool calls (read/ls/search/references) with valid replay_args are re-executed during resume.
2. Recovered outcomes are recorded with replay_count=1 and replayed_from_effect_id=effect_id.
3. Pending unsafe tool calls (edit/run/shell) are never replayed and fail-closed to interrupted.
4. Recovered tool outcomes are fed to the next turn's model prompt and run continues seamlessly.
5. Run details recovery summary renders 'Read action was recovered', etc.

Usage:
    python -B tests/manual/safe_tool_replay_smoke.py --self-test
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any
from unittest.mock import Mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.loop import run as run_agent_loop
from codey.agents.request import AgentRequest
from codey.agents.tools import DEFAULT_TOOL_FNS, AgentToolFns
from codey.agents.tool_execution import (
    evaluate_tool_call_policy,
    record_tool_call_intent,
)
from codey.operations.recovery import recover_effects_for_resume
from codey.runtime.effect_records import (
    RuntimeEffectStore,
    SETTLEMENT_STATUS_OK,
)
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.models import ToolCall
from codey.runtime.session_log import RuntimeSessionLog
from codey.runs.details import load_run_details
from codey.runs.ledger import RunLedgerStore


RESULTS_DIR = Path(__file__).resolve().parent / "results"
LIVE_TASK = (
    "Resume this interrupted Codey project task. First, use the recovered local "
    "tool result already provided for config.py. Then change only FEATURE_FLAG "
    "from False to True in config.py, run python -m py_compile config.py, and "
    "finish with a concise summary. Use only Codey's local JSON tools."
)
SAME_RUN_TASK = (
    "Resume this interrupted Codey project task. Use the recovered local tool "
    "result already provided for config.py, change only FEATURE_FLAG from False "
    "to True in config.py, and finish with a concise summary. Use only Codey's "
    "local JSON tools."
)


class _InjectedCrash(BaseException):
    """Synthetic process crash that the agent loop should not catch."""




class _MockResumeProvider:
    name = "MockResumeProvider"

    def __init__(self, final_summary: str = "smoke completed after replay") -> None:
        self.final_summary = final_summary
        self.prompts: list[str] = []

    def new_chat(self, timeout=None) -> None:
        del timeout

    def send(self, text: str, timeout=None) -> str:
        del timeout
        self.prompts.append(text)
        return f'{{"tool":"done","args":{{"summary":"{self.final_summary}"}}}}'


class _ScriptedResumeProvider:
    name = "ScriptedResumeProvider"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.replies = [
            '{"tool":"edit","args":{"path":"config.py","old_string":"FEATURE_FLAG = False","new_string":"FEATURE_FLAG = True"}}',
            '{"tool":"run","args":{"command":"python -m py_compile config.py","path":"."}}',
            '{"tool":"done","args":{"summary":"same-run resume smoke completed"}}',
        ]

    def new_chat(self, timeout=None) -> None:
        del timeout

    def send(self, text: str, timeout=None) -> str:
        del timeout
        self.prompts.append(str(text or ""))
        if self.replies:
            return self.replies.pop(0)
        return '{"tool":"done","args":{"summary":"same-run resume smoke completed"}}'


class _CountingProvider:
    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self.name = getattr(provider, "name", "provider")
        self.prompts: list[str] = []
        self.replies: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.provider, name)

    def new_chat(self, timeout: float | None = None) -> Any:
        if timeout is None:
            return self.provider.new_chat()
        return self.provider.new_chat(timeout=timeout)

    def send(self, text: str, timeout: float | None = None) -> str:
        prompt = str(text or "")
        self.prompts.append(prompt)
        reply = self.provider.send(prompt) if timeout is None else self.provider.send(prompt, timeout=timeout)
        reply_text = str(reply or "")
        self.replies.append(reply_text)
        return reply_text

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"safe_tool_replay_live_resume-{provider_id}-{stamp}.json"


def _write_resume_fixture(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "config.py").write_text(
        'FEATURE_FLAG = False\nAPP_NAME = "codey replay smoke"\n',
        encoding="utf-8",
    )


def _record_pending_read_intent(
    effects_store: RuntimeEffectStore,
    *,
    session_id: str,
    run_id: str,
    project_dir: Path,
) -> str:
    mock_session = Mock()
    mock_session.runtime_effects = effects_store
    mock_session.session_id = session_id
    mock_session.run_id = run_id
    mock_session.project = project_dir
    mock_session.profile = Mock()
    mock_session.profile.name = "coding_writer"
    mock_session.on_shell_request = None
    mock_session.trace = Mock()

    call_read = ToolCall(name="read", args={"path": "config.py"})
    _, replay_read = evaluate_tool_call_policy(mock_session, call_read, turn=1, tool_index=0)
    return record_tool_call_intent(
        mock_session,
        call_read,
        turn=1,
        tool_index=0,
        replay_decision=replay_read,
    )


def _final_content_ok(project_dir: Path) -> bool:
    path = project_dir / "config.py"
    return path.is_file() and "FEATURE_FLAG = True" in path.read_text(encoding="utf-8")


def _tool_names(events: list[Any]) -> tuple[str, ...]:
    names = []
    for event in events:
        call = getattr(event, "call", None)
        if getattr(event, "kind", "") == "tool" and call is not None:
            names.append(str(getattr(call, "name", "")))
    return tuple(names)


def run_self_test() -> bool:
    print("[safe_tool_replay_smoke] starting deterministic self-test...")

    with tempfile.TemporaryDirectory(prefix="codey_replay_smoke_") as temp_dir:
        root = Path(temp_dir)
        state_home = root / ".codey"
        state_home.mkdir(parents=True, exist_ok=True)
        project_dir = root / "project"
        project_dir.mkdir(parents=True, exist_ok=True)

        # Create dummy project files
        (project_dir / "target.py").write_text("print('hello codey replay')", encoding="utf-8")
        (project_dir / "helper.py").write_text("def helper_fn(): pass", encoding="utf-8")

        session_id = "smoke_replay_session"
        run_id = "smoke_replay_run"

        session_log = RuntimeSessionLog(state_home)
        operations_store = RuntimeOperationStore(session_log)
        effects_store = RuntimeEffectStore(session_log)
        operations_store.start(
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            provider_id="MockResumeProvider",
            turn_budget=10,
            max_repair_rounds=1,
            task_kind="project",
        )


        # 1. Simulate a crashed run that recorded intents before exiting:
        # - Intent 1: Safe read tool call (should be replayed)
        # - Intent 2: Safe search tool call (should be replayed)
        # - Intent 3: Unsafe edit tool call (should NOT be replayed, synthesized as interrupted)
        mock_session = Mock()
        mock_session.runtime_effects = effects_store
        mock_session.session_id = session_id
        mock_session.run_id = run_id
        mock_session.project = project_dir
        mock_session.profile = Mock()
        mock_session.profile.name = "coding_writer"
        mock_session.on_shell_request = None
        mock_session.trace = Mock()

        call_read = ToolCall(name="read", args={"path": "target.py", "offset": 1})
        _, replay_read = evaluate_tool_call_policy(mock_session, call_read, turn=1, tool_index=0)
        eff_read = record_tool_call_intent(mock_session, call_read, turn=1, tool_index=0, replay_decision=replay_read)

        call_search = ToolCall(name="search", args={"path": ".", "query": "helper_fn"})
        _, replay_search = evaluate_tool_call_policy(mock_session, call_search, turn=1, tool_index=1)
        eff_search = record_tool_call_intent(mock_session, call_search, turn=1, tool_index=1, replay_decision=replay_search)

        call_edit = ToolCall(name="edit", args={"path": "new_file.py", "content": "# new"})
        _, replay_edit = evaluate_tool_call_policy(mock_session, call_edit, turn=1, tool_index=2)
        eff_edit = record_tool_call_intent(mock_session, call_edit, turn=1, tool_index=2, replay_decision=replay_edit)

        # Verify initial pending state
        pending_before = effects_store.pending_effects(session_id, run_id)
        assert len(pending_before) == 3, f"expected 3 pending effects, got {len(pending_before)}"
        print("[safe_tool_replay_smoke] successfully recorded 3 pending intents (2 safe, 1 unsafe).")

        # 2. Trigger resume recovery.
        deps = Mock()
        deps.runtime_effects = effects_store
        deps.state = Mock()
        deps.state.runtime_effects = effects_store

        recovery = recover_effects_for_resume(
            deps,
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            task_kind="project",
        )
        assert recovery.ok, "expected recover_effects_for_resume to succeed"
        recovered_outcomes = recovery.recovered_tool_outcomes
        assert len(recovered_outcomes) == 2, f"expected 2 recovered outcomes, got {len(recovered_outcomes)}"
        print(f"[safe_tool_replay_smoke] recover_effects_for_resume produced {len(recovered_outcomes)} recovered outcomes.")

        # Check outcomes contents
        read_outcome = next(r for r in recovered_outcomes if r.call.name == "read")
        assert read_outcome.outcome.ok, "read outcome should be ok"
        assert "hello codey replay" in read_outcome.outcome.model_text

        search_outcome = next(r for r in recovered_outcomes if r.call.name == "search")
        assert search_outcome.outcome.ok, "search outcome should be ok"
        assert "helper.py" in search_outcome.outcome.model_text

        # 3. Check settlement records on disk
        loaded_after = effects_store.load_effects(session_id, run_id)
        assert len(effects_store.pending_effects(session_id, run_id)) == 0, "all effects should be settled"

        proj_read = next(p for p in loaded_after if p.intent.effect_id == eff_read)
        assert proj_read.settlement is not None and proj_read.settlement.status == SETTLEMENT_STATUS_OK
        assert proj_read.settlement.replay_count == 1
        assert proj_read.settlement.replayed_from_effect_id == eff_read

        proj_search = next(p for p in loaded_after if p.intent.effect_id == eff_search)
        assert proj_search.settlement is not None and proj_search.settlement.status == SETTLEMENT_STATUS_OK
        assert proj_search.settlement.replay_count == 1
        assert proj_search.settlement.replayed_from_effect_id == eff_search

        proj_edit = next(p for p in loaded_after if p.intent.effect_id == eff_edit)
        assert proj_edit.settlement is not None and proj_edit.settlement.status == "interrupted"
        assert proj_edit.settlement.replay_count == 0

        # 4. Check RecoverySummary
        summary = effects_store.recovery_summary(session_id, run_id)
        assert summary.replayed_reads == 1
        assert summary.replayed_lookups == 1
        assert summary.interrupted_writes == 1
        assert "Read action was recovered" in summary.explanation_lines
        assert "Lookup action was recovered" in summary.explanation_lines
        assert "Local write was interrupted and was not repeated" in summary.explanation_lines

        # 5. Agent loop execution with recovered outcomes
        provider = _MockResumeProvider()
        agent_req = AgentRequest(
            provider=provider,
            project=project_dir,
            task="finish project task after crash",
            session_id=session_id,
            run_id=run_id,
            runtime_effects=effects_store,
            recovered_tool_outcomes=recovered_outcomes,
        )

        loop_result = run_agent_loop(agent_req)
        assert loop_result.stop_reason == "done"
        assert loop_result.turns == 2
        assert len(provider.prompts) == 1
        assert "hello codey replay" in provider.prompts[0]
        assert "helper.py" in provider.prompts[0]
        print("[safe_tool_replay_smoke] agent loop resumed smoothly starting at turn 2.")

        # 6. Run details projection
        ledgers = RunLedgerStore(state_home)
        ledger_writer = ledgers.open(
            run_id=run_id,
            session_id=session_id,
            project=str(project_dir),
            task="finish project task after crash",
            provider="MockResumeProvider",
            mode="project",
        )
        ledger_writer.finish(summary="smoke completed after replay", stop_reason="done")



        details = load_run_details(
            run_ledgers=ledgers,
            run_traces=None,
            session_id=session_id,
            run_id=run_id,
            runtime_operations=None,
            runtime_effects=effects_store,
        )
        recovery_rows = [r for r in details.rows if r.label == "Recovery"]
        assert len(recovery_rows) >= 2, f"expected recovery rows in details, got {recovery_rows}"
        print(f"[safe_tool_replay_smoke] details projection contains {len(recovery_rows)} recovery row(s).")

    print("[safe_tool_replay_smoke] all 0.5.4 checks passed successfully.")
    return True


def run_same_run_self_test() -> bool:
    print("[safe_tool_replay_smoke] starting same-run resume self-test...")

    with tempfile.TemporaryDirectory(prefix="codey_replay_same_run_") as temp_dir:
        root = Path(temp_dir)
        state_home = root / ".codey"
        project_dir = root / "project"
        _write_resume_fixture(project_dir)

        session_id = "smoke_same_run_session"
        run_id = "smoke_same_run_run"
        provider = _ScriptedResumeProvider()
        events = []
        session_log = RuntimeSessionLog(state_home)
        operations_store = RuntimeOperationStore(session_log)
        effects_store = RuntimeEffectStore(session_log)

        started = operations_store.start(
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            provider_id=provider.name,
            turn_budget=8,
            max_repair_rounds=1,
            task_kind="project",
        )
        assert started is not None, "expected runtime operation to start"
        effect_id = _record_pending_read_intent(
            effects_store,
            session_id=session_id,
            run_id=run_id,
            project_dir=project_dir,
        )

        deps = Mock()
        deps.runtime_effects = effects_store
        deps.state = Mock()
        deps.state.runtime_effects = effects_store
        recovery = recover_effects_for_resume(
            deps,
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            task_kind="project",
        )
        assert recovery.ok, "expected resume recovery to succeed"
        recovered_outcomes = recovery.recovered_tool_outcomes
        assert len(recovered_outcomes) == 1, "expected one recovered read outcome"

        result = run_agent_loop(
            AgentRequest(
                provider=provider,
                project=project_dir,
                task=SAME_RUN_TASK,
                max_turns=8,
                fresh_chat=False,
                provider_id=provider.name,
                on_event=lambda event: events.append(event),
                session_id=session_id,
                run_id=run_id,
                runtime_effects=effects_store,
                recovered_tool_outcomes=recovered_outcomes,
            )
        )

        loaded = effects_store.load_effects(session_id, run_id)
        projection = next(proj for proj in loaded if proj.intent.effect_id == effect_id)
        assert projection.settlement is not None, "expected recovered read to settle"
        assert projection.settlement.replay_count == 1, "expected exactly one replay"
        assert projection.settlement.replayed_from_effect_id == effect_id
        assert len(effects_store.pending_effects(session_id, run_id)) == 0, "expected all effects to settle"
        assert _final_content_ok(project_dir), "expected config.py to be edited after resume"
        assert "FEATURE_FLAG = False" in provider.prompts[0], "expected recovered read text in first continuation prompt"
        assert result.stop_reason == "done", "expected clean agent completion"
        assert result.checks_passed, "expected py_compile to pass after resume"
        tools = _tool_names(events)
        assert "edit" in tools and "run" in tools, f"expected edit and run after resume, got {tools}"

    print("[safe_tool_replay_smoke] same-run resume self-test passed.")
    return True


def _crashing_tool_fns() -> AgentToolFns:
    def crash_info_tool(*_args: Any, **_kwargs: Any) -> Any:
        raise _InjectedCrash("synthetic crash after safe tool intent")

    return AgentToolFns(
        read_file=crash_info_tool,
        list_directory=DEFAULT_TOOL_FNS.list_directory,
        search_files=DEFAULT_TOOL_FNS.search_files,
        find_references=DEFAULT_TOOL_FNS.find_references,
        write_file=DEFAULT_TOOL_FNS.write_file,
        edit_file=DEFAULT_TOOL_FNS.edit_file,
        run_command=DEFAULT_TOOL_FNS.run_command,
        run_command_with_context=DEFAULT_TOOL_FNS.run_command_with_context,
    )


def _run_live_resume_case(
    provider: _CountingProvider,
    *,
    provider_id: str,
    max_turns: int,
) -> dict[str, Any]:
    started_at = time.time()
    with tempfile.TemporaryDirectory(prefix=f"codey_replay_live_{provider_id}_") as temp_dir:
        root = Path(temp_dir)
        state_home = root / ".codey"
        project_dir = root / "project"
        _write_resume_fixture(project_dir)

        session_id = f"safe_replay_live_{provider_id}"
        run_id = f"safe_replay_live_{provider_id}_{int(started_at)}"
        session_log = RuntimeSessionLog(state_home)
        operations_store = RuntimeOperationStore(session_log)
        effects_store = RuntimeEffectStore(session_log)
        operations_store.start(
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            provider_id=provider_id,
            turn_budget=max_turns,
            max_repair_rounds=1,
            task_kind="project",
        )

        stage1_events = []
        crashed = False
        try:
            run_agent_loop(AgentRequest(
                provider=provider,
                project=project_dir,
                task=(
                    "For this Codey smoke, first respond with exactly one local JSON "
                    'tool call: {"tool":"read","args":{"path":"config.py"}}. '
                    "After the tool result is returned, continue the task: change "
                    "FEATURE_FLAG from False to True in config.py, run python -m "
                    "py_compile config.py, and finish."
                ),
                max_turns=max_turns,
                fresh_chat=True,
                strict_fresh_chat=True,
                provider_id=provider_id,
                on_event=lambda event: stage1_events.append(event),
                tool_fns=_crashing_tool_fns(),
                session_id=session_id,
                run_id=run_id,
                runtime_effects=effects_store,
            ))
        except _InjectedCrash:
            crashed = True

        pending = effects_store.pending_effects(session_id, run_id)
        if not crashed or not pending:
            return {
                "ok": False,
                "provider": provider_id,
                "seconds": round(time.time() - started_at, 3),
                "stage": "fault_injection",
                "crashed": crashed,
                "pending_effects": len(pending),
                "stage1_event_count": len(stage1_events),
            }

        recover_deps = Mock()
        recover_deps.runtime_effects = effects_store
        recover_deps.state = Mock()
        recover_deps.state.runtime_effects = effects_store
        recovery = recover_effects_for_resume(
            recover_deps,
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            task_kind="project",
        )
        recovered_outcomes = recovery.recovered_tool_outcomes
        prompt_index = len(provider.prompts)
        if not recovery.ok or not recovered_outcomes:
            return {
                "ok": False,
                "provider": provider_id,
                "seconds": round(time.time() - started_at, 3),
                "stage": "recovery",
                "recover_ok": recovery.ok,
                "recovered_outcomes": len(recovered_outcomes),
            }

        stage2_events = []
        result = run_agent_loop(AgentRequest(
            provider=provider,
            project=project_dir,
            task=LIVE_TASK,
            max_turns=max_turns,
            fresh_chat=False,
            provider_id=provider_id,
            on_event=lambda event: stage2_events.append(event),
            session_id=session_id,
            run_id=run_id,
            runtime_effects=effects_store,
            recovered_tool_outcomes=recovered_outcomes,
        ))
        resume_prompt = provider.prompts[prompt_index] if len(provider.prompts) > prompt_index else ""
        summary = effects_store.recovery_summary(session_id, run_id)
        tools = _tool_names(stage2_events)
        checks_passed = bool(result.checks_passed)
        return {
            "ok": result.stop_reason == "done" and _final_content_ok(project_dir) and checks_passed,
            "provider": provider_id,
            "seconds": round(time.time() - started_at, 3),
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "provider_sends": len(provider.prompts),
            "recovered_outcomes": len(recovered_outcomes),
            "replayed_reads": summary.replayed_reads,
            "replayed_lookups": summary.replayed_lookups,
            "interrupted_writes": summary.interrupted_writes,
            "resume_prompt_contains_recovered_read": "FEATURE_FLAG = False" in resume_prompt,
            "final_content_ok": _final_content_ok(project_dir),
            "checks_passed": checks_passed,
            "tools_after_resume": list(tools),
        }


def run_live_resume_smoke(
    *,
    provider_id: str,
    port: int,
    max_turns: int,
    keep_open: bool,
    output: Path,
) -> int:
    from codey.providers import controls as provider_controls
    from codey.providers.registry import connect_provider

    payload: dict[str, Any] = {
        "probe": "safe_tool_replay_live_resume",
        "provider": provider_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "row": {},
    }
    _atomic_write_json(output, payload)
    provider_controls.begin_task_context(f"safe-tool-replay-live-resume:{provider_id}")
    provider = None
    try:
        provider = _CountingProvider(connect_provider(provider_id, port=port))
        row = _run_live_resume_case(
            provider,
            provider_id=provider_id,
            max_turns=max_turns,
        )
        payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        payload["row"] = row
        _atomic_write_json(output, payload)
        print(json.dumps(row, ensure_ascii=False, indent=2), flush=True)
        print(f"report: {output}", flush=True)
        return 0 if row.get("ok") else 1
    finally:
        provider_controls.end_task_context()
        if provider is not None and not keep_open:
            try:
                provider.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe tool replay smoke test.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic offline self-test")
    parser.add_argument("--same-run-self-test", action="store_true", help="Run same-run task entry resume self-test")
    parser.add_argument("--provider", default="", help="Live provider id for resume smoke")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.self_test:
        success = run_self_test()
        sys.exit(0 if success else 1)
    if args.same_run_self_test:
        success = run_same_run_self_test()
        sys.exit(0 if success else 1)
    if args.provider:
        output = args.output or _default_output(args.provider)
        sys.exit(run_live_resume_smoke(
            provider_id=args.provider,
            port=args.port,
            max_turns=args.max_turns,
            keep_open=args.keep_open,
            output=output,
        ))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
