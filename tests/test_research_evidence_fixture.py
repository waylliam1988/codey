"""The controlled evidence probe freezes content and ordering independently."""

import hashlib
import json
from pathlib import Path

from tests.manual.research_evidence_local import INPUTS, PASSAGES, fixture_windows


def test_windows_are_lossless_and_sampling_seed_is_not_an_input():
    text = "\n".join(start + "X" * 601 + end for start, end in PASSAGES.values())
    first = fixture_windows(text, "writer_limit", "candidates", 0)
    assert first == fixture_windows(text, "writer_limit", "candidates", 0)
    for row in first:
        excerpt = text[row["offset"]:row["offset"] + row["limit"]]
        assert hashlib.sha256(excerpt.encode()).hexdigest() == row["sha256"]
    assert {row["selector"] for row in first} == set(INPUTS["candidates"]["writer_limit"])


def test_rubric_covers_all_five_minimal_and_two_composite_cases():
    rubric = json.loads((Path(__file__).parent / "fixtures/research_local_rubric.json").read_text())
    assert len(rubric["cases"]) == 7
    assert all(case in INPUTS["given"] and row["required"] and row["forbidden"]
               for case, row in rubric["cases"].items())



def test_factorial_matrix_holds_completion_sampling_and_candidate_order_constant():
    from tests.manual.research_local_matrix import matrix

    runs = matrix("factorial")
    assert len(runs) == 24
    assert {row["order_seed"] for row in runs} == {0}
    assert {row["layer"] for row in runs} == {"given"}
    for label, split, full_history in (("A", False, True), ("B", True, True), ("C", False, False), ("D", True, False)):
        cells = [row for row in runs if row["configuration"] == label]
        assert len(cells) == 6
        assert {(row["case"], row["seed"]) for row in cells} == {
            (case, seed) for case in ("versions", "checkpoint_constraints") for seed in (74, 75, 76)}
        assert all(row["split"] == split and row["full_history"] == full_history for row in cells)



def test_wire_statistics_use_admitted_payload_and_report_missing_usage(tmp_path):
    from tests.manual.research_local_matrix import wire_stats

    payload = {"messages": [{"role": "user", "content": "完整条件"}]}
    rows = [{"event": "generation_start", "messages": [{"content": "unadmitted history"}]},
        {"event": "admitted_payload", "payload": payload},
        {"event": "generation_response", "body": {"usage": {"prompt_tokens": 42}, "choices": [{
            "message": {"content": '{"tool":"submit_research_answer","args":{"state":"unresolved","findings":[]}}'}}]}}]
    path = tmp_path / "research-wire.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    stats = wire_stats(tmp_path)
    assert stats["request_count"] == 1 and stats["prompt_tokens"] == [42]
    assert stats["admitted_payload_characters"] == [len(json.dumps(payload, ensure_ascii=False))]
    assert stats["model_tool_counts"] == {"submit_research_answer": 1}
    del rows[-1]["body"]["usage"]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    assert wire_stats(tmp_path)["total_prompt_tokens"] is None
