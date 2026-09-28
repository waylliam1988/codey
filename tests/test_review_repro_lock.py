"""Repro locks for review findings 1-7 (must fail before fix, pass after)."""
from __future__ import annotations

from types import SimpleNamespace


def _submission(project="E:/codey", task="研究，不要修改", requested=(), strict=True):
    return SimpleNamespace(
        project=project,
        task=task,
        requested_capabilities=requested,
        strict_research=strict,
    )


def test_issue1_strict_hybrid_no_write_without_explicit():
    from codey.policies.task_policy import build_task_policy
    s = _submission(requested=(), strict=True)
    p = build_task_policy(s, task_kind="hybrid", strict_research=True)
    assert "project.write" not in set(p.grants), f"hybrid strict leaked write: {sorted(p.grants)}"
    assert "shell.approval" not in set(p.grants), f"hybrid strict leaked shell: {sorted(p.grants)}"


def test_issue1_strict_hybrid_edit_rejected_at_parse_and_execute(tmp_path):
    from codey.operations.kernel_protocol import normalize_turn
    from codey.operations.task_kernel import TaskSession, execute_turn
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolCall
    s = _submission(project=str(tmp_path), requested=(), strict=True)
    policy = build_task_policy(s, task_kind="hybrid", strict_research=True)
    plan = normalize_turn('{"tool":"edit","args":{"path":"a.py","content":"x"}}', policy=policy, controller_allowed=None)
    assert plan.protocol_error and "disallowed" in plan.protocol_error.lower(), f"parse should reject edit: {plan}"
    session = TaskSession(policy=policy, task_kind="hybrid", project=str(tmp_path))
    results = execute_turn(session, [ToolCall("edit", {"path": "a.py", "content": "x"})],
                           executors={"edit": lambda c: "should-not-run"},
                           run_id="r", turn=1)
    assert results[0].model_text.startswith("ERROR:"), results[0].model_text
    assert not (tmp_path / "a.py").exists()


def test_issue1_strict_hybrid_explicit_write_kept():
    from codey.policies.task_policy import build_task_policy
    s = _submission(requested=("project.write",), strict=True)
    p = build_task_policy(s, task_kind="hybrid", strict_research=True)
    assert "project.write" in set(p.grants)


def test_issue2_default_hybrid_uses_unified_session():
    from types import SimpleNamespace as SN
    from unittest.mock import patch

    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    outcome = ModeOutcome({"type": "task_done", "mode": "hybrid"})
    # After cutover, hybrid must hit unified before any deps access, so empty
    # namespaces suffice. Before cutover it falls through to legacy flows.
    with patch("codey.operations.task_phases.dispatch.run_unified_mode", return_value=outcome) as unified:
        actual = dispatch_run_mode(SN(), SN(), SN(), SN(), SN(), SN(), "hybrid", SN())
    assert actual is outcome
    assert unified.call_count == 1


def test_issue2_unified_interleaves_web_and_project(tmp_path):
    from codey.operations.task_kernel import TaskSession, run_task_kernel
    from codey.policies.task_policy import build_task_policy
    from codey.runtime.core.models import ToolResult
    from codey.task.model import TaskSubmission
    sub = TaskSubmission("s", str(tmp_path), "task", 6, False, "deepseek",
                         requested_capabilities=("web.read",))
    policy = build_task_policy(sub, task_kind="hybrid", strict_research=False)
    session = TaskSession(policy=policy, task_kind="hybrid", project=str(tmp_path), max_turns=6)
    replies = iter([
        '{"tool":"web_search","args":{"query":"docs"}}',
        '{"tool":"read_file","args":{"path":"file.txt"}}',
        '{"tool":"edit","args":{"path":"new.txt","content":"hi"}}',
        '{"tool":"run","args":{"path":".","command":"echo ok"}}',
        '{"tool":"done","args":{"summary":"finished with interleaved tools"}}',
    ])
    class Provider:
        def send(self, prompt, timeout=None):
            return next(replies)
    def _ok(text):
        def _fn(call):
            return ToolResult(call=call, model_text=text)
        return _fn
    executors = {
        "web_search": _ok("1. Docs\n   https://example.com/docs"),
        "read_file": _ok("local content"),
        "edit": _ok("edited new.txt"),
        "run": _ok("1 passed"),
    }
    result = run_task_kernel(session, provider=Provider(), executors=executors,
                             run_id="r-interleave", effect_scope="hybrid")
    assert result.stop_reason == "done", result
    assert session.searches, "web_search should have run in same session"
    assert session.read_files or session.edited_files, "project tools should have run in same session"
    assert session.edited_files, "edit should have run in same session"
    assert session.verifications, "run should have run in same session"


def test_issue3_unified_completion_uses_session_facts_with_evidence():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_kernel import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project", project="p")
    session.record_edit("app.py")
    latest = max(session.edited_files.values())
    session.record_verification("pytest", latest, True, exit_code=0)
    v_no_ctx = evaluate(session, "done work")
    assert v_no_ctx.complete, f"baseline should pass: {v_no_ctx.followup}"
    ev = ExecutionEvidence()
    # unified production passes only evidence + run metadata, no changes/scope
    ctx = {"run_id": "r", "task": "t", "question": "t", "project": "p",
           "execution_evidence": ev, "analysis_run_payloads": []}
    v_ctx = evaluate(session, "done work", context=ctx)
    assert v_ctx.complete, (
        f"same session facts with evidence-only ctx should still pass, "
        f"got complete={v_ctx.complete} followup={v_ctx.followup!r} "
        f"proof={[ (c.check_id, c.status) for c in (v_ctx.proof.checks if v_ctx.proof else [])]}"
    )
    # Stale verification (old revision) must not complete, even with evidence.
    session.record_edit("app.py")
    ev2 = ExecutionEvidence()
    ctx2 = {"run_id": "r", "task": "t", "question": "t", "project": "p",
            "execution_evidence": ev2, "analysis_run_payloads": []}
    v_stale = evaluate(session, "done work", context=ctx2)
    assert not v_stale.complete, "old-version verification must not complete"


def test_issue4_same_slot_different_args_must_not_reuse():
    from codey.operations.task_kernel import TaskSession, execute_turn
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write"})),
        task_kind="project", project="p")
    def exec_a(call):
        return ToolResult(call=call, model_text="edited a.py")
    def exec_b(call):
        return ToolResult(call=call, model_text="edited b.py")
    r1 = execute_turn(session, [ToolCall("edit", {"path": "a.py", "content": "x"})],
                      executors={"edit": exec_a}, run_id="r", turn=1)
    assert "a.py" in r1[0].model_text
    r2 = execute_turn(session, [ToolCall("edit", {"path": "b.py", "content": "y"})],
                      executors={"edit": exec_b}, run_id="r", turn=1)
    # Must NOT return stale a.py result; must error or execute b.py
    assert "a.py" not in r2[0].model_text, f"stale reuse: {r2[0].model_text}"
    assert ("b.py" in r2[0].model_text) or r2[0].model_text.startswith("ERROR:")


def test_issue4_session_payload_roundtrip_keeps_policy_and_facts():
    from codey.operations.task_kernel import TaskSession
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission
    sub = TaskSubmission("s", "E:/codey", "task", 8, False, "local",
                         requested_capabilities=("web.read",))
    policy = build_task_policy(sub, task_kind="hybrid")
    s = TaskSession(policy=policy, task_kind="hybrid", project="E:/codey", max_turns=8)
    s.record_search("q")
    s.record_open("https://example.com/x")
    s.record_edit("a.py")
    payload = s.to_payload()
    assert "policy" in payload and "grants" in payload["policy"]
    revived = TaskSession.from_payload(payload)
    assert revived.task_kind == "hybrid"
    assert revived.edited_files == s.edited_files
    assert set(revived.policy.grants) == set(policy.grants)


def test_issue5_strict_initial_snapshot_hides_unavailable_tools():
    from codey.operations.task_kernel import TaskSession, controller_allowed_for_session
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission
    from codey.toolchain.tool_spec import json_contract_text
    sub = TaskSubmission("s", None, "research q", 3, False, "deepseek")
    policy = build_task_policy(sub, task_kind="research", strict_research=True)
    session = TaskSession(policy=policy, task_kind="research", max_turns=3)
    controller = controller_allowed_for_session(session)
    assert controller is not None
    assert "knowledge_write" not in controller, controller
    assert "open_hit" not in controller, controller
    # Per-turn snapshot shown to model must respect controller, not just policy
    contract = json_contract_text(policy, controller_allowed=controller)
    assert "knowledge_write" not in contract, contract
    # native snapshot helper should also support controller narrowing (or caller must filter)
    # At minimum, policy-wide list contains knowledge_write but per-turn must hide it.
    # This locks the requirement: caller needs controller-aware snapshot.
    try:
        from codey.toolchain.tool_spec import native_tools_for_snapshot
    except ImportError as err:
        # Before fix there is no controller-aware native snapshot -> fail to lock issue
        raise AssertionError("missing controller-aware native snapshot (native_tools_for_snapshot)") from err
    names2 = [t["function"]["name"] for t in native_tools_for_snapshot(policy, controller)]
    assert "knowledge_write" not in names2


def test_issue6_native_prompt_and_done_closure(monkeypatch):
    from codey.env_names import NATIVE_TOOLS_ENV
    from codey.operations.task_kernel import TaskSession, run_task_kernel
    from codey.policies.task_policy import build_task_policy
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.task.model import TaskSubmission
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    sub = TaskSubmission("s", None, "q", 2, False, "local")
    policy = build_task_policy(sub, task_kind="research")
    session = TaskSession(policy=policy, task_kind="research", max_turns=2)
    # Native prompt must not demand JSON-only
    try:
        import inspect

        from codey.operations.task_kernel import kernel_prompt_for_session as kprompt
        sig = inspect.signature(kprompt)
        if "native" in sig.parameters or "protocol" in sig.parameters:
            prompt = kprompt(session, native=True)
        else:
            # Before fix: no native-aware prompt param -> fail
            raise AssertionError("kernel prompt has no native mode (still forces JSON)")
    except AssertionError:
        raise
    except Exception as exc:
        raise AssertionError(f"native prompt missing: {exc}") from exc
    assert "JSON" not in prompt or "native" in prompt.lower(), prompt[:500]
    # Accepted native done must still answer its call id
    sent_batches = []
    class Provider:
        def send_turn(self, prompt, tools, timeout=None):
            return AssistantTurn(text="", tool_calls=(
                ProviderToolCall(id="done-1", name="done", arguments={"answer": "结论 x 来源 y"}),
            ))
        def send_tool_results(self, messages, tools, timeout=None):
            sent_batches.append(messages)
            return AssistantTurn(text="acked")
    # Need opened evidence so strict gate could pass? use non-strict project-less session
    s2 = TaskSession(policy=policy, task_kind="research", max_turns=2)
    # Monkeypatch gate to complete immediately to isolate closure behavior
    from unittest.mock import patch
    with patch("codey.operations.completion_gate.evaluate",
               return_value=SimpleNamespace(complete=True, followup="", proof=None)):
        run_task_kernel(s2, provider=Provider(), provider_id="local", run_id="r-native-done")
    assert sent_batches, "accepted native done must still send tool result for its call id"
    assert any(m.get("tool_call_id") == "done-1" for batch in sent_batches for m in batch)


def test_issue7_required_checks_are_enforced():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_kernel import TaskSession
    from codey.policies.task_policy import TaskPolicy
    policy = TaskPolicy(grants=frozenset({"control", "project.read"}),
                        required_checks=("custom_must_run",))
    session = TaskSession(policy=policy, task_kind="project", project="p")
    verdict = evaluate(session, "all done")
    assert not verdict.complete, "missing required check provider must block completion"
    assert "custom_must_run" in verdict.followup or "custom" in verdict.followup.lower() or verdict.proof is not None
