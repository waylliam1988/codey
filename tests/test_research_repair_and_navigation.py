"""Receipt-derived navigation and bounded protocol repair with actionable feedback."""

import json
import subprocess
import sys

import pytest

from codey.operations import kernel_prompt
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope
from codey.research.context_view import read_evidence_projection, read_intervals
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL, execute, setup
from tests.test_research_long_term_loop import strict_session


def run_repeated_protocol(root, *, native=False, boundary="", resume=False):
    def factory(provider):
        def respond(prompt):
            path = root / "requests.jsonl"
            rows = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"prompt": prompt}) + "\n")
            args = {"markdown": 7, "state": "answered"}
            if native:
                return AssistantTurn(tool_calls=(ProviderToolCall(f"changing-id-{len(rows)}", "submit_research_answer", args),))
            return json.dumps({"tool": "submit_research_answer", "args": args})
        provider.send = respond
        if native:
            provider.send_turn = lambda prompt, tools: respond(prompt)
        return provider

    session = strict_session()
    return _run(root, native=native, resume=resume, boundary=boundary, boundary_turn=2,
                compact_planning=True, required_checks=session.policy.required_checks, provider_factory=factory)


@pytest.mark.parametrize("native", [False, True])
def test_same_invalid_payload_stops_after_one_informed_retry_even_after_restart(tmp_path, native):
    source = ("from pathlib import Path; from tests.test_research_repair_and_navigation import run_repeated_protocol; "
              "import sys; run_repeated_protocol(Path(sys.argv[1]), native=sys.argv[2]=='True', "
              "boundary='provider_response_ready')")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), str(native)], capture_output=True, timeout=60)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_repeated_protocol(tmp_path, native=native, resume=True)
    assert result.stop_reason == "protocol", result.summary
    assert "without new diagnostic information" in result.summary
    requests = (tmp_path / "requests.jsonl").read_bytes()
    rows = [json.loads(row) for row in requests.splitlines()]
    assert len(rows) == 2 and rows[0]["prompt"] != rows[1]["prompt"]
    assert "markdown" in rows[1]["prompt"] and "Rejected response" in rows[1]["prompt"]
    assert run_repeated_protocol(tmp_path, native=native, resume=True) == result
    assert (tmp_path / "requests.jsonl").read_bytes() == requests
    assert not result.synthesis_id


def test_new_field_errors_produce_new_diagnosis_and_ignore_provider_call_ids():
    snapshot = build_turn_snapshot(strict_session(), native=True)

    def repair(args, identity):
        reply = AssistantTurn(tool_calls=(ProviderToolCall(identity, "submit_research_answer", args),))
        plan = normalize_turn(reply, snapshot=snapshot)
        assert plan.protocol_error
        return kernel_prompt._repair_prompt(plan, snapshot=snapshot, native=True, reply=reply)

    args = {"state": "answered", "markdown": 7}
    first = repair(args, "call1")
    assert first == repair(args, "call2")
    second = repair({"state": "wrong-state", "markdown": "An actual answer would go here."}, "call3")
    assert second != first
    assert "markdown" in first and "state" in second


def test_plain_prose_is_diagnosed_without_becoming_an_answer():
    snapshot = build_turn_snapshot(strict_session(), native=True)
    reply = AssistantTurn(text="Plain prose that was never submitted.")
    plan = normalize_turn(reply, snapshot=snapshot)
    assert plan.protocol_error
    prompt = kernel_prompt._repair_prompt(plan, snapshot=snapshot, native=True, reply=reply)
    assert "Plain prose" in prompt and "submit_research_answer" in prompt
    assert not plan.calls and plan.control is None


@pytest.mark.parametrize("ranges,expected", [([], [[0, 12000]]),
    ([(0, 1000), (9000, 10000)], [[1000, 9000], [10000, 12000]]),
    ([(2000, 6000), (0, 3000), (6000, 12000)], [])])
def test_navigation_merges_actual_ranges_and_preserves_holes(ranges, expected):
    view = read_intervals(12000, ranges)
    assert view["unread_intervals"] == expected
    assert view["first_unread_interval"] == (expected[0] if expected else None)
    assert view["next_offset"] == (expected[0][0] if expected else 12000)


@pytest.mark.usefixtures("public_source_dns")
def test_source_locator_exposes_middle_gap_from_real_read_receipts(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "abcdefghij" * 1200
    try:
        execute(delegate, tools, session, "open_url", {"url": URL, "limit": 1000})
        execute(delegate, tools, session, "open_url", {"url": URL, "offset": 9000, "limit": 1000})
        before = dict(tools.ledger.read_passages)
        projection = read_evidence_projection(session, tools.ledger)
        locator = next(row for row in projection["locators"] if "source_id" in row)
        assert locator["read_intervals"] == [[0, 1000], [9000, 10000]]
        assert locator["first_unread_interval"] == [1000, 9000]
        assert locator["next_offset"] == 1000
        assert tools.ledger.read_passages == before
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_every_omitted_window_has_exact_recovery_beyond_old_receipt_and_locator_caps(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "Complete original material. " * 25
    try:
        for index in range(16):
            read = execute(delegate, tools, session, "open_url", {"url": URL + f"/{index}"})
            session._memory_results[f"receipt-{index}"] = read
        scope = answer_scope(session, budget=1400)
        before = dict(tools.ledger.read_passages)
        text = answer_context(session, tools.ledger, scope)
        data = next(json.loads(block) for block in text.split("\n\n") if block.startswith('{"investigation_attempts"'))
        assert len(data["read_receipts"]) == 16
        assert len(data["source_locators"]) == 16
        assert len(data["omitted_windows"]) == 14
        hidden = data["omitted_windows"][0]
        assert hidden["label"] == "e1" and hidden["display_omitted"]
        assert hidden["source_hash"] and (hidden["start"], hidden["end"]) == (0, len(tools.search.text))
        assert hidden["recover"] == {"tool": "read_tool_result", "args": {"result_ref": "receipt-0"}}
        assert "text" not in hidden and "excerpt" not in hidden
        # Same receipt recovery semantics used by the real kernel: re-display
        # the original window last, making it eligible with its original label.
        session._memory_results["recovered"] = ToolResult(
            ToolCall("read_tool_result", {"result_ref": "receipt-0"}), "Recovered original receipt", ok=True)
        restored = answer_scope(session, budget=1400)
        assert ("e1", hidden["passage_ref"]) in restored.aliases
        assert tools.ledger.read_passages == before
        assert not store.search("material")
    finally:
        store.close()
