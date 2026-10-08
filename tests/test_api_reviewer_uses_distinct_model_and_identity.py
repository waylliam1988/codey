"""Writer and reviewer API models stay distinct with reusable scope identities."""
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from codey.app.provider_registry import ProviderRegistry
from codey.providers.api_provider import ApiProvider
from codey.providers.zen.connection import ZenProvider
from codey.reviews.identity import review_model_identity
from codey.runtime.core.api_selection import ApiRunSelection
from tests.support.model_preferences import enable_models


def test_zen_wrapper_preserves_protocol_and_effort_in_review_identity():
    first = ZenProvider(ApiProvider("http://fixture.test/v1", "model", api_protocol="openai-responses", reasoning_effort="low"))
    second = ZenProvider(ApiProvider("http://fixture.test/v1", "model", api_protocol="openai-responses", reasoning_effort="high"))
    assert review_model_identity(first)
    assert review_model_identity(first) != review_model_identity(second)
    assert review_model_identity(first) != review_model_identity(first.runtime)


def test_api_reviewer_selects_another_model_without_changing_writer(monkeypatch):
    from codey.providers import api_connections

    writer = ApiRunSelection("zen", "fixture", "writer", "openai-responses", True)
    class Connection:
        @staticmethod
        def model_payload():
            return {"models": [{"id": "writer"}, {"id": "reviewer"}]}

        @staticmethod
        def capture_selection(payload):
            return replace(writer, model_id=payload["model"], protocol="openai-completions")

    monkeypatch.setattr(api_connections, "connection_for", lambda _: Connection)
    reviewer = api_connections.capture_reviewer_selection(writer)
    assert reviewer.model_id == "reviewer"
    assert reviewer.protocol == "openai-completions"
    assert writer.model_id == "writer"


def test_production_review_uses_distinct_api_reviewer_with_frozen_writer(tmp_path):
    from codey.app import review_service

    writer = ApiRunSelection("zen", "fixture", "writer", "openai-responses", True)
    reviewer = replace(writer, model_id="reviewer", protocol="openai-completions")
    enable_models(tmp_path, zen=["writer", "reviewer", "selected-reviewer"])
    context = SimpleNamespace(providers=ProviderRegistry(tmp_path), lock=threading.Lock(), run_registry=SimpleNamespace(api_selection_for=lambda _: writer), emit=lambda _: None)
    with patch("codey.app.provider_services.reviewer_candidates", return_value=()), \
            patch("codey.providers.api_connections.capture_reviewer_selection", return_value=reviewer), \
            patch("codey.providers.api_connections.open_selection", return_value="review-provider"), \
            patch.object(review_service, "run_review_attempt", return_value=("zen", "approved")) as attempt, \
            patch("codey.app.provider_services.connect_fresh_provider_tab", side_effect=RuntimeError("unexpected self review")):
        result = review_service.run_review(context, session_id="s", project="fixture", task="task", writer_summary="done",
                                           changes={}, recent_log="", writer_id="zen", run_id="r", review_impact_map="")
    assert result == ("zen", "approved")
    assert attempt.call_args.kwargs["self_review"] is False
    assert attempt.call_args.kwargs["reviewer"] == "review-provider"


def test_reviewer_selection_survives_restart_and_cannot_be_rebound(tmp_path):
    import pytest

    from codey.runtime.core.operation_state import RuntimeOperationStore, RuntimeOperationTransitionError
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    selection = ApiRunSelection("zen", "revision", "reviewer", "openai-completions", True)
    log = RuntimeSessionLog(tmp_path)
    mutations = RuntimeMutationLine(log)
    mutations.accept_operation(session_id="s", run_id="r", provider_id="zen", turn_budget=4, max_repair_rounds=1)
    mutations.admit_reviewer_selection("s", "r", selection.to_payload())
    restarted = RuntimeOperationStore(RuntimeSessionLog(tmp_path)).load("s", "r")
    assert restarted.reviewer_selection == selection.to_payload()
    with pytest.raises(RuntimeOperationTransitionError, match="immutable"):
        mutations.admit_reviewer_selection("s", "r", replace(selection, model_id="another").to_payload())


def test_standalone_api_review_uses_selected_model_instead_of_selecting_another(tmp_path):
    from codey.app import review_service
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    selected = ApiRunSelection("zen", "agreement", "selected-reviewer", "openai-completions", True)
    log = RuntimeSessionLog(tmp_path)
    mutations = RuntimeMutationLine(log)
    mutations.accept_operation(session_id="s", run_id="r", provider_id="zen", turn_budget=4, max_repair_rounds=1,
                               task_kind="review", model_selection=selected.to_payload())
    enable_models(tmp_path, zen=["writer", "reviewer", "selected-reviewer"])
    context = SimpleNamespace(providers=ProviderRegistry(tmp_path), lock=threading.Lock(), run_registry=SimpleNamespace(api_selection_for=lambda _: selected), emit=lambda _: None,
                              runtime_log=log, runtime_mutations=mutations)
    with patch("codey.app.provider_services.reviewer_candidates", return_value=()), \
            patch("codey.providers.api_connections.capture_reviewer_selection", side_effect=AssertionError("no writer exists in a review-only run")), \
            patch("codey.providers.api_connections.open_selection", return_value="selected-provider") as opened, \
            patch.object(review_service, "run_review_attempt", return_value=("zen", "approved")):
        assert review_service.run_review(context, session_id="s", project="fixture", task="review", writer_summary="",
            changes={}, recent_log="", writer_id="zen", run_id="r", review_impact_map="", standalone=True) == ("zen", "approved")
    assert opened.call_args.args == (selected,)
