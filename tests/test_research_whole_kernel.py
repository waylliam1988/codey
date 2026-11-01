"""Whole answers use the real kernel, frozen requests and original publication."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, build_task_policy
from codey.research.answer_report import AnswerReport
from codey.research.completion_gate import ResearchCompletionGate
from codey.research.report_projection import validate_accepted_projection
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL

TASK = "Explain concurrent readers and the writer limit. State the evidence boundary."


def run_whole(root, *, native=False, resume=False, boundary="", scenario="answer"):
    def factory(provider):
        def send(prompt):
            path = root / "calls.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            turn = 1 + sum("whole_request" in row for row in rows)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"whole_request": turn, "prompt": prompt}) + "\n")
            if turn == 1:
                tool, args = "open_url", {"url": URL}
            else:
                tool = "submit_research_answer"
                args = {"state": "answered", "markdown": "Readers and a writer can run concurrently. "
                        "Only one writer can write at a time [e1]."}
                if scenario == "gap":
                    args = {"state": "unresolved", "markdown": "The material does not establish the historical restrictions. "
                            "Further historical documentation is needed."}
                elif scenario == "repair" and turn == 2:
                    args["markdown"] += " Unsupported citation [e99]."
            return json.dumps({"tool": tool, "args": args})
        provider.send = send
        return provider
    return _run(root, native=native, resume=resume, boundary=boundary, task=TASK, compact_planning=True,
                required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS, provider_factory=factory, boundary_turn=2)


def test_new_entry_declares_whole_answer_requirements_and_keeps_explicit_source_requirement():
    policy = build_task_policy(SimpleNamespace(project="", sources_open_required=True), task_kind="research", strict_research=True)
    assert policy.required_checks == RESEARCH_ANSWER_REQUIRED_CHECKS
    assert policy.sources_open_required


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("scenario", ["answer", "gap", "repair"])
@pytest.mark.usefixtures("public_source_dns")
def test_read_submit_publish_and_resume_without_extra_model_or_claims(tmp_path, native, scenario):
    result = run_whole(tmp_path, native=native, scenario=scenario)
    assert result.stop_reason == "done", result.summary
    assert isinstance(result.accepted_report, AnswerReport)
    assert result.turns == (3 if scenario == "repair" else 2)
    assert result.completion_proof.satisfied and not result.research_record.claims
    validate_accepted_projection(result, run_id="r", session_id="s", project="")
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert run_whole(tmp_path, native=native, resume=True, scenario=scenario) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        kinds = store.index._read_conn().execute("SELECT kind,COUNT(*) AS n FROM knowledge_versions GROUP BY kind").fetchall()
        counts = {row["kind"]: row["n"] for row in kinds}
        assert counts["answer"] == 1 and "claim" not in counts
        assert counts.get("evidence", 0) == (0 if scenario == "gap" else 1)
    finally:
        store.close()
    if scenario == "gap":
        gate = ResearchCompletionGate()
        item = SimpleNamespace(kind="open_question", title=TASK, started_run_id="r")
        decision = gate.evaluate(item=item, event={"run_id": "r", "stop_reason": "done"},
                                 research_result=result, session_id="s", task=TASK)
        assert decision.block and decision.blocked_reason == "research_delivered_question_unresolved"


@pytest.mark.parametrize("boundary", ["provider_request_ready", "provider_response_ready", "tool_effect_pending",
                                      "kernel_settled", "publication_admitted", "publication_committed", "publication_receipt"])
def test_whole_answer_cold_restart_keeps_one_publication(tmp_path, boundary):
    source = ("from pathlib import Path; import sys; from tests.test_research_whole_kernel import run_whole; "
              "run_whole(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_whole(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    validate_accepted_projection(result, run_id="r", session_id="s", project="")
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("whole_request" in row for row in rows) == 2
    assert sum("publication" in row for row in rows) == 1
