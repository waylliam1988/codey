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
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents.loop import run as run_agent_loop
from codey.agents.request import AgentRequest
from codey.agents.tool_execution import (
    evaluate_tool_call_policy,
    record_tool_call_intent,
)
from codey.operations.task_run import _recover_effects_for_resume
from codey.runtime.effect_records import (
    RuntimeEffectStore,
    SETTLEMENT_STATUS_OK,
)
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.models import ToolCall
from codey.runtime.session_log import RuntimeSessionLog
from codey.runs.details import load_run_details
from codey.runs.ledger import RunLedgerStore




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

        # 2. Trigger resume recovery via _recover_effects_for_resume
        deps = Mock()
        deps.runtime_effects = effects_store
        deps.state = Mock()
        deps.state.runtime_effects = effects_store

        ok, recovered_outcomes = _recover_effects_for_resume(
            deps,
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            task_kind="project",
        )
        assert ok, "expected _recover_effects_for_resume to succeed"
        assert len(recovered_outcomes) == 2, f"expected 2 recovered outcomes, got {len(recovered_outcomes)}"
        print(f"[safe_tool_replay_smoke] _recover_effects_for_resume produced {len(recovered_outcomes)} recovered outcomes.")

        # Check outcomes contents
        read_outcome = next(r for r in recovered_outcomes if r.effect_id == eff_read)
        assert read_outcome.outcome.ok, "read outcome should be ok"
        assert "hello codey replay" in read_outcome.outcome.model_text

        search_outcome = next(r for r in recovered_outcomes if r.effect_id == eff_search)
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
        assert summary.replayed_searches == 1
        assert summary.interrupted_writes == 1
        assert "Read action was recovered" in summary.explanation_lines
        assert "Search action was recovered" in summary.explanation_lines
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe tool replay smoke test.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic offline self-test")
    args = parser.parse_args()

    if args.self_test:
        success = run_self_test()
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
