from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("module_name", "experiment_id"),
    (
        ("tests.manual.bounded_research_planner_ab", "bounded_research_planner_ab"),
        ("tests.manual.source_connector_ab", "source_connector_ab"),
    ),
)
def test_ab_harness_main_closes_trace_writer_on_output_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_name: str,
    experiment_id: str,
) -> None:
    harness = importlib.import_module(module_name)
    closed: list[Path] = []

    class FakeTrace:
        def __init__(
            self,
            *,
            directory: Path,
            experiment_id: str,
            run_id: str,
            provider: str,
        ) -> None:
            self.directory = Path(directory)
            self.closed = False
            assert experiment_id == expected_experiment_id
            assert run_id == "result"
            assert provider == "deepseek"

        def close(self) -> None:
            assert not self.closed
            self.closed = True
            closed.append(self.directory)

    expected_experiment_id = experiment_id
    output = tmp_path / "result.json"

    def fake_run_provider(provider_id: str, **kwargs: object) -> dict[str, object]:
        assert provider_id == "deepseek"
        assert isinstance(kwargs["trace"], FakeTrace)
        raise harness.OutputProviderMismatch(
            path=Path(kwargs["output"]),
            expected="deepseek",
            found="qwen",
        )

    monkeypatch.setattr(harness, "WEB_PROVIDERS", ("deepseek",))
    monkeypatch.setattr(harness, "ABJournalWriter", FakeTrace)
    monkeypatch.setattr(harness, "run_provider", fake_run_provider)
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--provider", "deepseek", "--output", str(output)],
    )

    assert harness.main() == 2
    assert closed == [tmp_path / "result.trace"]
