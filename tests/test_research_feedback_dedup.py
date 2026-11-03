"""Exercise the production presentation against original receipts and actual requests."""

import copy
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from codey.operations import task_guidance
from codey.providers import api_generation
from codey.providers.api_provider import ApiProvider
from codey.providers.token_accounting import RequestContextCount
from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope
from codey.research.ledger import ResearchLedger
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_research_long_term_loop import strict_session
from tests.test_research_whole_kernel import run_whole

ERROR_RECEIPT = "Tool failed. The complete error appears in latest_error in the current evidence view.\n"


def failed(text, tool="knowledge_read"):
    return ToolResult(ToolCall(tool, {"id": "exact-failed-target", "revision": 1}), text, ok=False)


@pytest.mark.parametrize("length", [1599, 1600, 1601, 4000])
def test_only_fully_displayed_latest_error_is_compacted_with_exact_recovery(tmp_path, length):
    session = strict_session()
    old = failed("ERROR: older failure retains its own exact diagnosis.")
    latest = failed("ERROR: " + "X" * (length - 7))
    session._memory_results = {"r-old": old, "r-latest": latest}
    scope = answer_scope(session)
    view = answer_context(session, ResearchLedger(), scope)
    delivered = task_guidance.task_result_context(session, latest, scope)
    assert old.model_text in view and latest.model_text[:1600] in view
    assert "r-latest" in delivered
    assert (ERROR_RECEIPT in delivered) == (length <= 1600)
    if length <= 1600:
        assert (view + delivered).count(latest.model_text) == 1
    else:
        assert latest.model_text in delivered
    assert old.model_text in task_guidance.task_result_context(session, old, scope)
    assert latest.model_text in task_guidance.task_result_context(session, latest, None)
    assert session._memory_results["r-latest"].model_text == latest.model_text


def test_previous_error_remains_after_a_later_success_and_noninvestigation_errors_keep_full_delivery(tmp_path):
    session = strict_session()
    result = failed("ERROR: retain version and target after a later success.")
    session._memory_results = {"r-error": result, "r-success": ToolResult(ToolCall("knowledge_search", {}), "none", ok=True)}
    assert result.model_text in answer_context(session, ResearchLedger(), answer_scope(session))
    assert result.model_text in task_guidance.task_result_context(session, result, answer_scope(session))
    submission = failed("ERROR: unknown evidence e99; recover the original passage.", "submit_research_answer")
    session._memory_results["r-submit"] = submission
    view = answer_context(session, ResearchLedger(), answer_scope(session))
    assert submission.model_text in view
    assert ERROR_RECEIPT in task_guidance.task_result_context(session, submission, answer_scope(session))


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_actual_request_contains_one_complete_error_and_real_repair_still_publishes(tmp_path, monkeypatch, native):
    sent, providers = [], []

    def wrap(scripted):
        def generate(_url, payload, *_args, **_kwargs):
            sent.append(copy.deepcopy(payload))
            prompt = "\n".join(str(row.get("content", "")) for row in payload["messages"])
            row = json.loads(scripted.send(prompt))
            message = ({"tool_calls": [{"id": f"call-{len(sent)}", "type": "function", "function": {
                "name": row["tool"], "arguments": json.dumps(row["args"])}}]} if native else {"content": json.dumps(row)})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}

        monkeypatch.setattr(api_generation, "generate", generate)
        provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, stream=False,
                               request_counter=lambda *a, **kw: RequestContextCount(100, "tokenizer"))
        providers.append(provider)
        return provider

    try:
        result = run_whole(tmp_path, native=native, scenario="repair", provider_wrap=wrap)
        assert result.stop_reason == "done" and result.synthesis_id and len(sent) == 3
        last = "\n".join(str(row.get("content", "")) for row in sent[-1]["messages"])
        assert last.count("Unknown or unshown evidence: e99.") == 1
        assert ERROR_RECEIPT.strip() in last
        assert "[e1]" in last and "e99" not in result.accepted_report.markdown
    finally:
        for provider in providers:
            provider.close()


def run_error_restart(root, *, boundary="", resume=False):
    from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
    from tests.test_research_deterministic_completion import _run
    from tests.test_research_findings import URL

    path = root / "feedback-requests.jsonl"

    def factory(_):
        provider = ApiProvider("http://fixture/v1", "fixture", native_tools=True, stream=False,
                               request_counter=lambda *a, **kw: RequestContextCount(100, "tokenizer"))
        provider.bind_context("s")
        if resume:
            from codey.durable import DurableSession, SQLiteDurableStore

            durable = DurableSession(SQLiteDurableStore(root / "state.sqlite"))
            try:
                original = durable.context_snapshot("s", token_budget=30000)
                messages = provider._codec.encode_context(list(original.messages))
                # This low-level pipeline fixture does not run the app's
                # conversation activation. Restore its actual committed path,
                # including the assistant's pending native call, before delivery.
                provider._codec.validate_view(messages)
                provider._messages = messages
                provider._durable_source_entry_ids = original.entry_ids
            finally:
                durable.close()
        return provider

    def generate(_url, payload, *_args, **_kwargs):
        before = path.read_text(encoding="utf8").splitlines() if path.exists() else []
        with path.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(payload) + "\n")
        turn = len(before) + 1
        if turn == 1:
            name, args = "knowledge_read", {"id": "missing-exact-restart-target", "revision": 1}
        elif turn == 2:
            name, args = "open_url", {"url": URL}
        else:
            assert turn == 3
            name, args = "submit_research_answer", {"state": "answered", "markdown": "Only one writer writes at a time [e1]."}
        return {"choices": [{"message": {"content": "", "tool_calls": [{"id": f"call-{turn}",
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "finish_reason": "tool_calls"}]}

    with patch("codey.providers.api_generation.generate", generate):
        return _run(root, native=True, compact_planning=True, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS,
                    provider_factory=factory, resume=resume, boundary=boundary,
                    boundary_turn=2 if boundary == "provider_request_ready" else 1)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary", ["", "provider_response_ready", "provider_request_ready"])
def test_error_delivery_survives_process_restart_and_publishes_once(tmp_path, boundary):
    if boundary:
        source = ("from pathlib import Path; import sys; "
                  "from tests.test_research_feedback_dedup import run_error_restart; "
                  "run_error_restart(Path(sys.argv[1]), boundary=sys.argv[2])")
        child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=60)
        assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_error_restart(tmp_path, resume=bool(boundary))
    assert result.stop_reason == "done" and result.synthesis_id
    path = tmp_path / "feedback-requests.jsonl"
    before = path.read_bytes()
    rows = [json.loads(line) for line in before.splitlines()]
    assert len(rows) == 3
    error_request = "\n".join(row.get("content", "") for row in rows[1]["messages"])
    assert error_request.count("requested knowledge identity was not found") == 1
    assert ERROR_RECEIPT.strip() in error_request
    assert run_error_restart(tmp_path, resume=True) == result and path.read_bytes() == before
