from __future__ import annotations

from types import SimpleNamespace

from tools import live_smoke


def test_live_smoke_builds_agent_request_for_current_project_adapter(monkeypatch) -> None:
    provider = SimpleNamespace(close=lambda: None)
    seen: list[object] = []

    monkeypatch.setattr(live_smoke, "connect_provider", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(live_smoke.provider_controls, "begin_task_context", lambda *_args: None)
    monkeypatch.setattr(live_smoke.provider_controls, "end_task_context", lambda: None)

    def fake_run(request):
        seen.append(request)
        return SimpleNamespace(summary="ok", stop_reason="done", turns=1, changed=False)

    monkeypatch.setattr(live_smoke, "run", fake_run)
    result = live_smoke.run_smoke("zen", "discussion", 9222, 2)

    assert result["ok"] is True
    assert len(seen) == 1
    request = seen[0]
    assert request.provider is provider
    assert request.max_turns == 2
    assert request.provider_id == "zen"
    assert request.workspace_revision_store is not None
