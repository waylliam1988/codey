from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ScholarlySources
from codey.research.search_contracts import SearchOutcome, SearchState, complete_search, failed_search
from codey.research.search_profiles import SearchProfile
from codey.research.search_provenance import SearchAttempt
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, parse_json_object
from tests.manual import research_live_web_local_ab as experiment
from tests.test_chat_web_provider import Browser


def provider(cls: type[experiment.ObservedProduction], rows: list[JsonObject], error: Exception | None = None) -> tuple[experiment.ObservedProduction, Browser]:
    class Backend(Browser):
        def search(self, query: str, *, limit: int, profile: SearchProfile, deadline: float) -> SearchOutcome:
            if error is not None and not isinstance(error, cancellation.TaskCancelled):
                self.searches.append((query, limit))
                return failed_search(query, profile.name, "search_unavailable", str(error))
            return super().search(query, limit=limit, profile=profile, deadline=deadline)
    class Metadata(ScholarlySources):
        def search(self, query: str, *, limit: int, deadline: float) -> SearchOutcome | None:
            return complete_search(query, rows, SearchAttempt(query, "fixture", coverage="scholarly"), limit) if rows else None
    backend = Backend()
    backend.error = error
    backend.hits = [{"title": "Official guidance", "url": "https://official.example/guidance", "snippet": ""}]
    base = BrowserSearchProvider(headless=True)
    base.bind_browser_source(backend)
    result = cls(base, connector_ids=())
    result.scholarly = Metadata()
    return result, backend


HITS: list[JsonObject] = [{"title": f"Paper {n}", "url": f"https://pubmed.ncbi.nlm.nih.gov/{n}/", "snippet": ""} for n in (1, 2)]


def test_production_and_candidate_keep_browser_material_after_connector_hits() -> None:
    baseline, baseline_browser = provider(experiment.ObservedProduction, HITS)
    candidate, candidate_browser = provider(experiment.BrowserAfterConnector, HITS)
    baseline_rows = baseline.search("PubMed evidence").rows
    assert baseline_rows[:2] == HITS
    assert baseline_rows[2]["url"] == "https://official.example/guidance"
    assert len(baseline_browser.searches) == 1
    rows = candidate.search("PubMed evidence").rows
    assert rows[:2] == HITS
    assert rows[2]["url"] == "https://official.example/guidance"
    assert len(candidate_browser.searches) == 1


def test_browser_failure_preserves_available_connector_evidence() -> None:
    candidate, _ = provider(experiment.BrowserAfterConnector, HITS, RuntimeError("browser unavailable"))
    assert candidate.search("PubMed evidence").rows == HITS


def test_failure_without_connector_hits_is_not_hidden() -> None:
    candidate, _ = provider(experiment.BrowserAfterConnector, [], RuntimeError("browser unavailable"))
    assert candidate.search("PubMed evidence").state == SearchState.FAILED


def test_candidate_preserves_cancellation() -> None:
    candidate, _ = provider(experiment.BrowserAfterConnector, HITS, cancellation.TaskCancelled())
    with pytest.raises(cancellation.TaskCancelled):
        candidate.search("PubMed evidence")


def test_run_applies_backend_budget_after_local_selection_and_records_it(tmp_path: Path) -> None:

    def fake_stage(*args: object, **kwargs: object) -> None:
        from codey.providers.api_provider import ApiProvider

        assert kwargs.get("reasoning_effort") is None
        provider = ApiProvider("http://127.0.0.1:5001/v1", "test")
        payload: dict[str, object] = {}
        provider.configure_request(payload)
        assert payload["reasoning_effort"] == "none"

    response = io.BytesIO(b'{"idle":1,"queue":0}')
    with patch.object(experiment, "urlopen", return_value=response), patch.object(
        experiment.stage_module, "stage", side_effect=fake_stage
    ):
        experiment.run(tmp_path, "pubmed_myocarditis", "baseline", 1202, "http://127.0.0.1:5001", "test")
    row = parse_json_object((tmp_path / "pubmed_myocarditis-1202-baseline/search-observations.json").read_text())
    assert row["reasoning_effort"] == "none"


def test_experiment_budget_overrides_default_context_reserve() -> None:
    from codey.providers.local_config import LocalContextBudget, LocalProviderConfig

    config = LocalProviderConfig(context=LocalContextBudget(32768, 4096, 12000))
    configured = experiment.apply_experiment_output_budget(config)
    assert configured.context is not None
    assert configured.context.context_reserve_tokens == experiment.OUTPUT_TOKENS
    assert configured.context.source == "2a-live-contract"


@pytest.mark.parametrize("compiled", [True, False])
def test_actual_schema_must_compile_before_inference_and_is_checked_once(tmp_path: Path, compiled: bool) -> None:
    import json

    inference_ready = []

    def fake_stage(*args: object, **kwargs: object) -> None:
        from codey.providers.api_provider import ApiProvider

        provider = ApiProvider("http://127.0.0.1:5001/v1", "test")
        payload: dict[str, object] = {"response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}}
        provider.configure_request(payload)
        provider.configure_request(payload)
        inference_ready.append(True)

    responses = [io.BytesIO(b'{"idle":1,"queue":0}'),
                 io.BytesIO(json.dumps({"success": compiled, "result": 'root ::= "{}"' if compiled else ""}).encode())]
    with patch.object(experiment, "urlopen", side_effect=responses) as opened, patch.object(
        experiment.stage_module, "stage", side_effect=fake_stage
    ):
        if compiled:
            experiment.run(tmp_path, "pubmed_myocarditis", "baseline", 1202, "http://127.0.0.1:5001", "test")
        else:
            with pytest.raises(RuntimeError, match="discarded experimental tool schema"):
                experiment.run(tmp_path, "pubmed_myocarditis", "baseline", 1202, "http://127.0.0.1:5001", "test")
    assert opened.call_count == 2
    assert bool(inference_ready) == compiled
