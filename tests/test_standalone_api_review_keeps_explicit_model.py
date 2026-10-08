"""Standalone API review uses its selected model even when a web model is open."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.provider_registry import ProviderRegistry
from codey.runtime.core.api_selection import ApiRunSelection
from tests.support.model_preferences import enable_models


@pytest.mark.parametrize("connection_id", ["local", "zen"])
def test_formal_review_keeps_selected_api_and_never_borrows_open_browser(tmp_path, monkeypatch, connection_id):
    from codey.app import provider_services
    from codey.providers import api_connections

    selection = ApiRunSelection(connection_id, "fixture-revision", "selected-reviewer", "openai-completions", True)
    reviewer = SimpleNamespace(
        name="Selected API", model_identity="fixture-model-identity",
        new_chat=Mock(), close=Mock(),
        send=Mock(return_value='{"verdict":"approved","summary":"Looks good","findings":[]}'),
    )
    connector = SimpleNamespace(capture_selection=lambda *_: selection, validate_selection=lambda *_: None)
    monkeypatch.setattr(api_connections, "connection_for", lambda _: connector)
    opened = Mock(return_value=reviewer)
    monkeypatch.setattr(api_connections, "open_selection", opened)
    choose_other = Mock(side_effect=AssertionError("standalone review has no writer model to avoid"))
    monkeypatch.setattr(api_connections, "capture_reviewer_selection", choose_other)
    monkeypatch.setattr(provider_services, "reviewer_candidates", lambda *_: ["deepseek"])
    web = Mock(side_effect=AssertionError("explicit API review must not borrow a browser"))
    monkeypatch.setattr(provider_services, "connect_existing_provider", web)
    project = tmp_path / "project"
    project.mkdir()
    source = project / "app.py"
    source.write_text("x = 2\n", encoding="utf-8")
    changes = {"ok": True, "files": [{"path": "app.py"}], "changed_count": 1,
               "diff": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"}
    rows = []
    enable_models(tmp_path / "state", **{connection_id:[selection.model_id]})
    result = run_headless(
        HeadlessRequest(project=project, task="Review changes", intent="review", provider_id=connection_id,
                        model_selection={"model": selection.model_id}, state_home=tmp_path / "state"),
        connect_provider=lambda *_args, **_kwargs: reviewer,
        collect_changes=lambda *_: changes, emit_jsonl=rows.append,
    )
    assert result.exit_code == 0
    terminal = next(row for row in rows if row["type"] == "task_done")
    assert terminal["review"]["status"] == "complete"
    opened.assert_called_once_with(selection)
    reviewer.send.assert_called_once()
    web.assert_not_called()
    choose_other.assert_not_called()
    assert source.read_text(encoding="utf-8") == "x = 2\n"


def test_standalone_missing_selection_cannot_fall_back_to_open_web_model(monkeypatch):
    from codey.app import provider_services, review_service

    context = SimpleNamespace(run_registry=SimpleNamespace(api_selection_for=lambda _: None), emit=Mock())
    web = Mock(return_value="unexpected browser")
    monkeypatch.setattr(provider_services, "reviewer_candidates", web)
    with pytest.raises(RuntimeError, match="Selected API reviewer is unavailable"):
        review_service.run_review(context, session_id="s", project="fixture", task="review",
                                 writer_summary="", changes={}, recent_log="", writer_id="zen", run_id="r",
                                 review_impact_map="", standalone=True)
    web.assert_not_called()


def test_automatic_project_review_preserves_open_web_preference(monkeypatch, tmp_path):
    from codey.app import provider_services, review_service
    from codey.providers import api_connections

    context = SimpleNamespace(providers=ProviderRegistry(tmp_path), lock=threading.Lock(), set_provider_session=Mock())
    monkeypatch.setattr(provider_services, "reviewer_candidates", lambda *_: ["deepseek"])
    monkeypatch.setattr(provider_services, "connect_existing_provider", lambda _: "web reviewer")
    opened = Mock(side_effect=AssertionError("project review should prefer an available independent web model"))
    monkeypatch.setattr(api_connections, "open_selection", opened)
    attempt = Mock(return_value=("deepseek", "review result"))
    monkeypatch.setattr(review_service, "run_review_attempt", attempt)
    result = review_service.run_review(context, session_id="s", project="fixture", task="task",
                                      writer_summary="done", changes={}, recent_log="", writer_id="zen",
                                      run_id="r", review_impact_map="")
    assert result == ("deepseek", "review result")
    assert attempt.call_args.kwargs["reviewer"] == "web reviewer"
    assert attempt.call_args.kwargs["self_review"] is False
    opened.assert_not_called()
