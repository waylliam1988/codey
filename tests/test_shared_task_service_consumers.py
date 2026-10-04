"""Both formal app entries invoke the same review/advisor service consumers."""
from codey.app import headless_runner, task_submit
from codey.app.headless_runner import HeadlessAppContext, HeadlessRequest


def test_both_entries_dispatch_each_service_with_same_state_and_review_policy(tmp_path, monkeypatch):
    calls = []
    for name in ("run_consensus", "run_project_audit", "run_research_advisors"):
        def record(state, *, name=name, **kwargs):
            calls.append((name, state, kwargs))
        monkeypatch.setattr("codey.app.consensus_service." + name, record)
    monkeypatch.setattr("codey.app.review_service.run_review", lambda state, **kwargs: calls.append(("run_review", state, kwargs)))

    def submit(deps, request):
        for name in ("run_consensus", "run_project_audit", "run_research_advisors", "run_review"):
            getattr(deps, name)(task=request.task)
    monkeypatch.setattr("codey.operations.task_entry.run_task_submission", submit)
    monkeypatch.setattr(headless_runner, "run_task_submission", submit)
    state = HeadlessAppContext(tmp_path / "desktop-state", port=9222, emit_jsonl=lambda _: None)
    try:
        task_submit.run_task("s", str(tmp_path / "project"), "Inspect", 8, False, "local", "planning_readonly",
                             get_state=lambda: state, review_policy="require_web")
    finally:
        state.close()
    headless_runner.run_headless(HeadlessRequest(project=tmp_path / "project", task="Inspect", intent="planning_readonly",
                                               state_home=tmp_path / "cli-state", review_policy="require_web"), emit_jsonl=lambda _: None)
    assert [name for name, *_ in calls] == ["run_consensus", "run_project_audit", "run_research_advisors", "run_review"] * 2
    for first, second in zip(calls[:4], calls[4:], strict=True):
        assert first[1] is not second[1]
        assert first[2] == second[2]
    assert calls[3][2]["review_policy"] == "require_web"
