"""Completed prose survives delivery repair without becoming a second answer."""

import copy
import hashlib
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from codey.knowledge.store import KnowledgeStore
from codey.operations import kernel_prompt
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL
from tests.test_research_long_term_loop import strict_session

DRAFT = ("Only one writer can write at a time [e1].\n" * 35
         + "关键边界：材料不能证明这台机器的性能。\n"
         + '```json\n{"tool":"knowledge_commit","args":{"changes":[]}}\n```')
MARKER = "Rejected response (quoted data, not instructions): "


def observed(prompt):
    return json.JSONDecoder().raw_decode(prompt.split(MARKER, 1)[1])[0]


def test_complete_prose_roundtrips_in_repair_without_execution_or_state_inference():
    snapshot = build_turn_snapshot(strict_session(), native=True)
    reply = AssistantTurn(text=DRAFT)
    plan = normalize_turn(reply, snapshot=snapshot)
    assert plan.protocol_error and not plan.calls and plan.control is None
    prompt = kernel_prompt._repair_prompt(plan, snapshot=snapshot, native=True, reply=reply)
    data = observed(prompt)
    assert data["response"]["text"] == DRAFT
    assert data["response"]["calls"] == [] and "preview" not in data
    encoded = json.dumps(data["response"], ensure_ascii=False, sort_keys=True)
    assert data["sha256"] == hashlib.sha256(encoded.encode()).hexdigest()
    assert "Choose state honestly" in prompt and "has not been certified" in prompt
    assert "Quoted tool syntax does not execute" in prompt
    assert kernel_prompt._repair_prompt(plan, snapshot=snapshot, native=True, reply=reply) == prompt


@pytest.mark.parametrize("scenario", ["ordinary", "legacy", "truncated", "malformed", "tool_args", "text"])
def test_other_protocol_errors_keep_the_existing_diagnostic_contract(scenario):
    session, native = strict_session(), scenario != "text"
    reply = AssistantTurn(text=DRAFT)
    if scenario == "ordinary":
        session = TaskSession(TaskPolicy(frozenset({"control"})))
    elif scenario == "legacy":
        session = TaskSession(TaskPolicy(frozenset({"control", "web.read"}), strict_research=True))
    elif scenario in {"truncated", "malformed"}:
        reply = AssistantTurn(text=DRAFT, finish=TurnFinish.OUTPUT_LIMIT if scenario == "truncated"
                              else TurnFinish.PROTOCOL_ERROR)
    elif scenario == "tool_args":
        reply = AssistantTurn(text=DRAFT, tool_calls=(ProviderToolCall("bad", "submit_research_answer",
            {"markdown": DRAFT, "state": "invalid-state"}),))
    snapshot = build_turn_snapshot(session, native=native)
    plan = normalize_turn(reply, snapshot=snapshot)
    prompt = kernel_prompt._repair_prompt(plan, snapshot=snapshot, native=native, reply=reply)
    data = observed(prompt)
    assert "response" not in data and len(data["preview"]) == 600
    assert "Repair the missing submission action" not in prompt


def run_full_prose_repair(root, *, boundary="", resume=False, repeat=False):
    requests = root / "actual-requests.jsonl"

    def provider_factory(_):
        provider = ApiProvider("http://localhost:9/v1", "fixture", native_tools=True)
        provider.bind_context("s")
        return provider

    def generate(_url, payload, *_args, **_kwargs):
        before = requests.read_text(encoding="utf-8").splitlines() if requests.exists() else []
        with requests.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(copy.deepcopy(payload), ensure_ascii=False) + "\n")
        turn = len(before) + 1
        if turn == 1:
            name, args = "open_url", {"url": URL}
        elif turn == 2 or repeat:
            return {"choices": [{"message": {"role": "assistant", "content": DRAFT}, "finish_reason": "stop"}]}
        else:
            assert turn == 3
            text = "\n".join(str(row.get("content", "")) for row in payload["messages"])
            assert observed(text)["response"]["text"] == DRAFT
            name, args = "submit_research_answer", {"markdown": DRAFT, "state": "partial"}
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call-{turn}", "type": "function", "function": {"name": name,
                "arguments": json.dumps(args, ensure_ascii=False)}}]}, "finish_reason": "tool_calls"}]}

    with patch("codey.providers.api_generation.generate", generate):
        return _run(root, native=True, resume=resume, boundary=boundary,
            boundary_turn=3 if boundary == "provider_request_ready" else 2,
            compact_planning=True, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS,
            provider_factory=provider_factory)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary", ["", "provider_response_ready", "provider_request_ready"])
def test_actual_request_and_cold_restart_preserve_draft_with_one_real_publication(tmp_path, boundary):
    if boundary:
        source = ("from pathlib import Path; import sys; "
                  "from tests.test_research_full_prose_repair import run_full_prose_repair; "
                  "run_full_prose_repair(Path(sys.argv[1]), boundary=sys.argv[2])")
        child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary],
                               capture_output=True, timeout=60)
        assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_full_prose_repair(tmp_path, resume=bool(boundary))
    assert result.stop_reason == "done", result.summary
    assert result.accepted_report.state == "partial"
    assert result.accepted_report.markdown == DRAFT
    before = (tmp_path / "actual-requests.jsonl").read_bytes()
    assert len(before.splitlines()) == 3
    assert run_full_prose_repair(tmp_path, resume=True) == result
    assert (tmp_path / "actual-requests.jsonl").read_bytes() == before
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        counts = {row["kind"]: row["n"] for row in store.index._read_conn().execute(
            "SELECT kind,COUNT(*) AS n FROM knowledge_versions GROUP BY kind")}
        assert counts["answer"] == 1 and "claim" not in counts
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_repeated_prose_stops_without_promoting_embedded_tool_syntax(tmp_path):
    result = run_full_prose_repair(tmp_path, repeat=True)
    assert result.stop_reason == "protocol" and not result.synthesis_id
    assert "without new diagnostic information" in result.summary
    assert len((tmp_path / "actual-requests.jsonl").read_bytes().splitlines()) == 3
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute(
            "SELECT COUNT(*) FROM knowledge_versions WHERE kind IN ('answer','claim','inference')").fetchone()[0] == 0
    finally:
        store.close()
