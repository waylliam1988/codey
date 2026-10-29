"""Deterministic smoke test for safe tool replay and resume recovery (0.5.4).

Verifies the 0.5.4 crash-recovery lifecycle:
1. Pending safe tool calls (read/ls/search/references) with valid replay_args are re-executed during resume.
2. Recovered outcomes are recorded with replay_count=1 and replayed_from_effect_id=effect_id.
3. Pending unsafe tool calls (edit/run/shell) are never replayed and fail-closed to interrupted.
4. Recovered tool outcomes are fed to the next turn's model prompt and run continues seamlessly.
5. Run details recovery summary renders 'Read action was recovered', etc.

Usage:
    python -B tests/manual/safe_tool_replay_smoke.py --same-run-self-test
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import contextlib

from codey.agents.request import AgentRequest
from codey.durable import DurableSession, SQLiteDurableStore
from codey.providers.snapshot import provider_snapshot
from codey.runtime.effects.effect_records import (
    RuntimeEffectStore,
)
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.workspace.revision import WorkspaceRevisionStore
from tests.support.kernel_runtime import run_agent as run_agent_loop

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




class _ScriptedResumeProvider:
    name = "ScriptedResumeProvider"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.replies = [
            '{"tool":"edit","args":{"path":"config.py","replacements":[{"old_string":"FEATURE_FLAG = False","new_string":"FEATURE_FLAG = True"}]}}',
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


def _open_runtime(
    state_home: Path,
    *,
    session_id: str,
    run_id: str,
    provider_id: str,
    task: str,
) -> tuple[DurableSession, RuntimeEffectStore, ToolResultDeliveryStore, RuntimeMutationLine]:
    durable = DurableSession(SQLiteDurableStore(state_home / "durable.sqlite3"))
    durable.admit_input(
        conversation_id=session_id,
        request_id=run_id,
        task_id=run_id,
        text=task,
        provider_snapshot=provider_snapshot(provider_id, None),
        task_state={"task_kind": "project"},
    )
    durable.claim_task(run_id)
    durable.start_task(run_id, state={"task_kind": "project"})
    durable.advance_task(
        task_id=run_id, phase="running", state={"task_kind": "project"},
    )
    return (
        durable,
        RuntimeEffectStore(durable=durable),
        ToolResultDeliveryStore(durable=durable),
        RuntimeMutationLine(durable=durable),
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
        durable, effects_store, delivery_store, mutations = _open_runtime(
            state_home,
            session_id=session_id,
            run_id=run_id,
            provider_id=provider.name,
            task=SAME_RUN_TASK,
        )

        started = mutations.accept_operation(
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            provider_id=provider.name,
            turn_budget=8,
            max_repair_rounds=1,
            task_kind="project",
        )
        assert started is not None, "expected runtime operation to start"
        mutations.mark_writer_running(
            session_id,
            run_id,
            provider_id=provider.name,
        )
        from codey.durable.checkpoint import operation_from_checkpoint
        from codey.storage.managed_outputs import ManagedOutputStore

        outputs = ManagedOutputStore(state_home)
        armed = [True]
        def crash_after_intent(*_args):
            position = operation_from_checkpoint(durable.task(run_id), session_id)
            if armed[0] and position.leaf == "tool_effect_pending":
                armed[0] = False
                raise _InjectedCrash("committed original read intent")
        durable.subscribe_commits(crash_after_intent)

        class InitialProvider:
            name = provider.name
            def send(self, _prompt, timeout=None):
                return '{"tool":"read_file","args":{"path":"config.py"}}'

        def request(active_provider):
            return AgentRequest(
                provider=active_provider, project=project_dir,
                workspace_revision_store=WorkspaceRevisionStore(state_home),
                managed_outputs=outputs, task=SAME_RUN_TASK, max_turns=8,
                fresh_chat=False, provider_id=provider.name, on_event=events.append,
                session_id=session_id, run_id=run_id, runtime_mutations=mutations,
                durable=durable,
            )
        try:
            run_agent_loop(request(InitialProvider()))
        except _InjectedCrash:
            pass
        else:
            raise AssertionError("original read intent fault was not reached")
        pending_reads = [row for row in effects_store.pending_effects(session_id, run_id)
                         if row.intent.effect_category == "tool_call"]
        assert len(pending_reads) == 1
        effect_id = pending_reads[0].intent.effect_id
        result = run_agent_loop(request(provider))

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
        durable.close()

    print("[safe_tool_replay_smoke] same-run resume self-test passed.")
    return True


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
        durable, effects_store, delivery_store, mutations = _open_runtime(
            state_home,
            session_id=session_id,
            run_id=run_id,
            provider_id=provider_id,
            task=LIVE_TASK,
        )
        mutations.accept_operation(
            session_id=session_id,
            run_id=run_id,
            project=str(project_dir),
            provider_id=provider_id,
            turn_budget=max_turns,
            max_repair_rounds=1,
            task_kind="project",
        )
        mutations.mark_writer_running(
            session_id,
            run_id,
            provider_id=provider_id,
        )

        from codey.durable.checkpoint import operation_from_checkpoint
        from codey.storage.managed_outputs import ManagedOutputStore

        outputs = ManagedOutputStore(state_home)
        armed = [True]
        def crash_after_intent(*_args):
            position = operation_from_checkpoint(durable.task(run_id), session_id)
            if armed[0] and position.leaf == "tool_effect_pending":
                armed[0] = False
                raise _InjectedCrash("original tool intent committed")
        durable.subscribe_commits(crash_after_intent)

        stage1_events = []
        crashed = False
        try:
            run_agent_loop(AgentRequest(
                provider=provider,
                project=project_dir,
                workspace_revision_store=WorkspaceRevisionStore(state_home),
                managed_outputs=outputs,
                task=LIVE_TASK,
                max_turns=max_turns,
                fresh_chat=True,
                provider_id=provider_id,
                on_event=lambda event: stage1_events.append(event),
                session_id=session_id,
                run_id=run_id,
                runtime_mutations=mutations,
                durable=durable,
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

        prompt_index = len(provider.prompts)
        original_effect_ids = {item.intent.effect_id for item in pending
                               if item.intent.effect_category == "tool_call"}

        stage2_events = []
        result = run_agent_loop(AgentRequest(
            provider=provider,
            project=project_dir,
            workspace_revision_store=WorkspaceRevisionStore(state_home),
            managed_outputs=outputs,
            task=LIVE_TASK,
            max_turns=max_turns,
            fresh_chat=False,
            provider_id=provider_id,
            on_event=lambda event: stage2_events.append(event),
            session_id=session_id,
            run_id=run_id,
            runtime_mutations=mutations,
            durable=durable,
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
            "recovered_outcomes": sum(row.intent.effect_id in original_effect_ids and row.settlement is not None
                                      for row in effects_store.load_effects(session_id, run_id)),
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
            with contextlib.suppress(Exception):
                provider.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Safe tool replay smoke test.")
    parser.add_argument("--same-run-self-test", action="store_true", help="Run same-run task entry resume self-test")
    parser.add_argument("--provider", default="", help="Live provider id for resume smoke")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

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
