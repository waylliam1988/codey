"""The controlled evidence probe freezes content and ordering independently."""

import hashlib
import json
from pathlib import Path

import pytest

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


def test_candidate_refresh_probe_rereads_the_oldest_of_six_scripted_candidates():
    from tests.manual.research_evidence_local import candidate_refresh_calls

    calls = candidate_refresh_calls("p_frozen")
    assert [call.name for call in calls] == ["save_finding"] * 6 + ["knowledge_read"]
    assert all(call.args["question_ref"] == "q1" for call in calls)
    assert calls[-1].args["id"] == "f1"



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


def test_fixed_clock_and_semantic_axes_are_explicit_and_do_not_change_the_rubric():
    from tests.manual.research_evidence_local import DEFAULT_CLOCK_FILE, fixture_clock
    from tests.manual.research_local_matrix import pending_review

    clock = fixture_clock(DEFAULT_CLOCK_FILE)
    assert "2026-11-01T04:00:00+00:00" in clock
    assert set(pending_review()) == {"condition_or_relation_omission", "unsupported_addition", "state_misreport", "gap_termination"}
    assert all(row["status"] == "pending_manual_review" and row["evidence"] == [] for row in pending_review().values())


@pytest.mark.parametrize("change", ["missing", "naive", "wrong_date", "wrong_offset"])
def test_invalid_fixed_clock_fails_instead_of_falling_back_to_host_time(tmp_path, change):
    from tests.manual.research_evidence_local import DEFAULT_CLOCK_FILE, fixture_clock

    clock = fixture_clock(DEFAULT_CLOCK_FILE)
    if change == "missing":
        clock = "unbounded host clock"
    elif change == "naive":
        clock = clock.replace("12:00:00+08:00", "12:00:00")
    elif change == "wrong_date":
        clock = clock.replace('"local_date":"2026-11-01"', '"local_date":"2026-11-02"')
    else:
        clock = clock.replace('"utc_offset":"+0800"', '"utc_offset":"+0900"')
    path = tmp_path / "clock.txt"
    path.write_text(clock, encoding="utf-8")
    with pytest.raises(ValueError):
        fixture_clock(path)


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_fixed_clock_is_identical_in_counted_and_admitted_initial_and_continuation_requests(tmp_path, monkeypatch, protocol):
    import copy

    from codey.providers import api_transport, request_context
    from codey.providers.api_provider import ApiProvider
    from codey.providers.base import ProviderToolDefinition, ProviderToolResult
    from codey.providers.token_accounting import RequestContextCount
    from tests.manual.research_evidence_local import DEFAULT_CLOCK_FILE, fixture_clock
    from tests.manual.research_local_matrix import wire_stats

    clock = fixture_clock(DEFAULT_CLOCK_FILE)
    monkeypatch.setattr(request_context, "clock_context", lambda: clock)
    sent, counted = [], []

    def count(payload, **kwargs):
        counted.append(copy.deepcopy(payload))
        return RequestContextCount(100, "estimated")

    def generate(url, payload, headers, **kwargs):
        sent.append(copy.deepcopy(payload))
        if protocol == "openai-completions":
            message = ({"tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"a.py"}'}}]} if len(sent) == 1 else {"content": "ok"})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}
        return {"status": "completed", "output": ([{"type": "function_call", "call_id": "c1", "name": "read_file", "arguments": '{"path":"a.py"}'}]
            if len(sent) == 1 else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}])}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol, request_counter=count)
    try:
        tools = [ProviderToolDefinition("read_file", "read", {})]
        provider.send_turn("Historical material dated 2024-05-22", tools)
        provider.send_tool_results([ProviderToolResult("c1", "original result")], tools)
        assert sent == counted
        path = tmp_path / "research-wire.jsonl"
        path.write_text("\n".join(json.dumps({"event": "admitted_payload", "payload": payload}) for payload in sent), encoding="utf-8")
        stats = wire_stats(tmp_path)
        assert stats["clock_observation_counts"] == [1, 1]
        assert stats["clock_sha256_values"] == [hashlib.sha256(clock.encode()).hexdigest()]
        assert "2024-05-22" in json.dumps(sent)
        assert "<request_context>" not in json.dumps(provider._messages)
    finally:
        provider.close()
