"""完成证明只有一个权威：共同 gate 生成，外层只持久化与展示。

- subject 用真实 run_id，不再回退到任务文本摘要；
- evidence refs 用实际收据身份（edit revision、verification workspace 身份）；
- RunResult/Research 投影不得丢弃 kernel proof；
- 项目最终事件收据携带同一份 kernel proof。
"""

from __future__ import annotations


def _session():
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    return TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
        task_kind="project", max_turns=4, task_text="t",
    )


def test_gate_subject_uses_real_run_id_without_task_fallback():
    from codey.operations.completion_gate import evaluate

    verdict = evaluate(_session(), "all done", context={"run_id": "r-123"})
    assert verdict.complete is True
    assert verdict.proof is not None
    assert "r-123" in verdict.proof.subject_ref

    fallback = evaluate(_session(), "all done task text here", context={})
    assert fallback.complete is True
    assert fallback.proof is not None
    # 任务文本回退已删除：无 run 时 subject 不再携带文本摘要身份
    assert fallback.proof.subject_ref == "task:project", fallback.proof.subject_ref


def test_proof_refs_carry_receipt_identities():
    from codey.operations.completion_gate import _proof_evidence_refs

    session = _session()
    session.record_edit("app.py", revision=7)
    session.record_verification(
        "pytest", 7, True, exit_code=0,
        workspace_revision=7, workspace_fingerprint="fp-abc",
    )
    refs = _proof_evidence_refs(session, {"run_id": "r-9"})
    text = " ".join(refs)
    assert "run:r-9" in text
    edit_refs = [r for r in refs if r.startswith("edit:")]
    assert edit_refs and any("@rev7" in r or "rev7" in r for r in edit_refs), refs
    verify_refs = [r for r in refs if r.startswith("verify:")]
    assert verify_refs and any("fp-abc" in r or "@ws7" in r for r in verify_refs), refs
    assert not any(r.startswith("edit:app.py") and r == "edit:app.py" for r in refs), (
        f"描述标签不能唯一定位执行：{refs}"
    )


def test_run_result_preserves_kernel_proof():
    from dataclasses import replace

    from codey.operations.completion_gate import evaluate
    from codey.runtime.core.run_result import RunResult

    verdict = evaluate(_session(), "all done", context={"run_id": "r-keep"})
    assert verdict.proof is not None
    result = RunResult(summary="all done", stop_reason="done", turns=1, proof=verdict.proof)
    assert result.proof is verdict.proof
    assert replace(result, checks_passed=True).proof is verdict.proof


def test_research_projection_keeps_kernel_proof():
    from codey.operations.completion_gate import evaluate
    from codey.operations.research_flow import research_payload
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.run_result import ResearchRunResult

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read"})),
        task_kind="research", max_turns=2, task_text="q",
    )
    verdict = evaluate(session, "结论：x\n来源：y", context={"run_id": "r-res"})
    assert verdict.proof is not None
    result = ResearchRunResult(
        question="q", summary="s", stop_reason="done", turns=1,
        completion_proof=verdict.proof,
    )
    payload = research_payload(result)
    assert payload.get("completion_proof") is not None


def test_project_done_event_carries_kernel_proof():
    from types import SimpleNamespace

    from codey.operations.completion_gate import evaluate
    from codey.operations.project_completion_flow import _build_project_done_event
    from codey.runtime.core.run_result import RunResult

    verdict = evaluate(_session(), "all done", context={"run_id": "r-proj"})
    assert verdict.proof is not None
    receipt = SimpleNamespace(to_dict=lambda: {"display": {"summary": "all done"},
                                              "completion_proof": verdict.proof.to_payload()})
    ctx = SimpleNamespace(
        frame=SimpleNamespace(run_id="r-proj", provider_id="local"),
        request=SimpleNamespace(session_id="s-proj", max_turns=4),
        result=RunResult(summary="all done", stop_reason="done", turns=2, proof=verdict.proof),
        receipt=receipt,
        research_result=None,
        research_pipeline_result=None,
        task_changed=False,
        task_changes={"changed_count": 0, "files": []},
        work=SimpleNamespace(
            evidence=SimpleNamespace(changed_files=()),
            analysis_run_payloads=(),
        ),
    )
    event = _build_project_done_event(ctx)
    payload = event.get("receipt", {}).get("completion_proof") if isinstance(event, dict) else None
    if payload is None and hasattr(event, "event"):
        payload = event.event.get("receipt", {}).get("completion_proof")
    assert payload is not None, "最终事件必须携带同一份 kernel proof"


def test_final_project_gate_cannot_drop_required_web_evidence(monkeypatch):
    from types import SimpleNamespace

    from codey.completion.contract import build_completion_contract, completion_check, project_completion_proof
    from codey.operations import project_completion_enforcement as enforcement
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.run_result import RunResult

    coding_proof = project_completion_proof(build_completion_contract(
        domain="coding", subject_ref="run:r-final",
        checks=[completion_check("relevant_verification", "pass")],
    ))
    from codey.completion.decision import CompletionDecision

    evaluation = SimpleNamespace(
        decision=CompletionDecision(coding_proof, None, (), "", ""), integrity=None,
    )
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"}),
                                          sources_open_required=True), task_kind="project", max_turns=4)
    ctx = SimpleNamespace(
        task_session=session, result=RunResult("done", proof=coding_proof),
        frame=SimpleNamespace(run_id="r-final", trace=None),
        request=SimpleNamespace(task="查官方文档", session_id="s"),
        task_changed=False, files=(), task_changes={}, selected_check=None,
        work=SimpleNamespace(evidence=object(), analysis_run_payloads=()), project=".",
        checkpoint_green=False, verification_forbidden=False,
        behavioral_plan=None, behavioral_observation=None,
        completion_engine=SimpleNamespace(evaluate=lambda **kw: evaluation),
    )
    monkeypatch.setattr(enforcement, "_commit_operation_proof", lambda *_: None)
    enforcement._record_completion_evidence(ctx)
    assert ctx.proof is not None and not ctx.proof.satisfied
    assert ctx.result.proof is ctx.proof


def test_requested_project_change_is_required_for_research_too():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.write"})),
                          task_kind="research", project_changes_required=True)
    assert not evaluate(session, "done", context={"run_id": "research-edit"}).complete


def test_project_receipt_projects_final_verification_without_rechecking_workspace():
    from types import SimpleNamespace
    from unittest import mock

    from codey.completion.contract import completion_check
    from codey.operations.task_session import session_checks_passed

    session = SimpleNamespace(edited_files={"app.py": 1})
    proof = SimpleNamespace(checks=(completion_check("relevant_verification", "pass"),))
    with mock.patch("codey.operations.project_completion_checks.project_completion_checks", side_effect=AssertionError("duplicate gate")):
        assert session_checks_passed(session, proof)
