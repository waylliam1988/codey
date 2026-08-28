from __future__ import annotations

import json

from tests.manual import bounded_research_planner_ab
from tests.manual.ab_harness_common import ArmRunLayout


class _DoneProvider:
    name = "fake"
    location = "fixture://fake"

    def new_chat(self, timeout=None) -> None:
        pass

    def send(self, text: str, timeout=None) -> str:
        return json.dumps({"tool": "done", "args": {"answer": "No supported evidence was opened."}})

    def close(self) -> None:
        pass


def test_run_case_accepts_pipeline_topic_continuity_kwargs(tmp_path) -> None:
    row = bounded_research_planner_ab.run_case(
        _DoneProvider(),
        provider_id="deepseek",
        case=bounded_research_planner_ab.CASES["warehouse_gap"],
        arm="baseline",
        max_turns=1,
        run_id="bounded-topic-continuity-test",
        trace=None,
        layout=ArmRunLayout.for_output(tmp_path / "result.json", journal_dir=None),
    )

    assert row["ok"] is True
    assert "error" not in row
    assert row["provider"] == "deepseek"
