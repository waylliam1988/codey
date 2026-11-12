"""Tool groups keep exact receipt predicates through publication and restart."""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from codey.completion.contract import CHECK_PASS, MAX_COMPLETION_CHECKS, CompletionCheck
from codey.operations.completion_gate import register_completion_check_provider, unregister_completion_check_provider
from codey.operations.completion_inputs import CompletionCheckInput
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
from codey.policies.tool_requirement import MAX_REQUIRED_TOOLS, require_tool_success
from codey.research.report_projection import validate_accepted_projection
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL


def run_grouped(root: Path, *, resume: bool = False, native: bool = False, boundary: str = ""):
    def factory(provider):
        def send(prompt):
            path = root / "calls.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            turn = 1 + sum("grouped_request" in row for row in rows)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"grouped_request": turn}) + "\n")
            if turn <= MAX_REQUIRED_TOOLS:
                return json.dumps({"tool": "open_url", "args": {"url": f"{URL}?read={turn}"}})
            return json.dumps({"tool": "submit_research_answer", "args": {
                "state": "answered", "markdown": "Only one writer can write at a time [e1]."}})
        provider.send = send
        return provider

    return _run(root, task=f"Read {URL}?read=1 and the other writer references; explain writer limits.",
                native=native, resume=resume, budget=MAX_REQUIRED_TOOLS + 2, boundary=boundary,
                boundary_turn=MAX_REQUIRED_TOOLS + 1, provider_factory=factory,
                required_checks=(*RESEARCH_ANSWER_REQUIRED_CHECKS, *(require_tool_success(
                    "open_url", arguments={"url": f"{URL}?read={i}"}) for i in range(1, MAX_REQUIRED_TOOLS + 1))))


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_sixteen_tool_predicates_publish_once_and_replay_without_new_io(tmp_path, native):
    result = run_grouped(tmp_path, native=native)
    assert result.stop_reason == "done", result.summary
    validate_accepted_projection(result, run_id="r", session_id="s", project="")
    checks = result.completion_proof.checks
    assert len(checks) == 9 <= MAX_COMPLETION_CHECKS
    assert {row.check_id for row in checks} >= {*RESEARCH_ANSWER_REQUIRED_CHECKS,
                                               "required_tools_satisfied", "research_publication"}
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert run_grouped(tmp_path, native=native, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before
    rows = [json.loads(line) for line in before.splitlines()]
    # The explicit first source is prefetched once before the model's reads.
    assert Counter(row["fetch"] for row in rows if "fetch" in row) == Counter(
        [f"{URL}?read=1", *(f"{URL}?read={i}" for i in range(1, MAX_REQUIRED_TOOLS + 1))])
    assert sum("publication" in row for row in rows) == 1


@pytest.mark.parametrize("boundary", ["kernel_settled", "publication_receipt"])
def test_tool_group_cold_restart_preserves_original_receipts(tmp_path, boundary):
    source = ("from pathlib import Path; import sys; "
              "from tests.test_completion_tool_groups import run_grouped; "
              "run_grouped(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary],
                           capture_output=True, timeout=60)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_grouped(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    validate_accepted_projection(result, run_id="r", session_id="s", project="")
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("grouped_request" in row for row in rows) == MAX_REQUIRED_TOOLS + 1
    assert Counter(row["fetch"] for row in rows if "fetch" in row) == Counter(
        [f"{URL}?read=1", *(f"{URL}?read={i}" for i in range(1, MAX_REQUIRED_TOOLS + 1))])
    assert sum("publication" in row for row in rows) == 1


def _publication_padding(_inputs: CompletionCheckInput) -> list[CompletionCheck]:
    return [CompletionCheck(f"padding_{i}", CHECK_PASS) for i in range(4)]


def test_seventeen_distinct_tool_predicates_exceed_the_declared_bound():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    policy = TaskPolicy(frozenset({"control"}), required_checks=tuple(
        require_tool_success("open_url", arguments={"url": f"{URL}?read={i}"})
        for i in range(MAX_REQUIRED_TOOLS + 1)))
    verdict = evaluate(TaskSession(policy=policy), "Already done")
    assert not verdict.complete and verdict.blocked
    assert verdict.error_source == "completion_contract"
    assert "17 > 16" in verdict.followup


@pytest.mark.usefixtures("public_source_dns")
def test_publication_capacity_is_reserved_before_any_publication_or_retry(tmp_path):
    register_completion_check_provider("publication_padding", _publication_padding, profile="research")
    try:
        result = run_grouped(tmp_path)
    finally:
        unregister_completion_check_provider("publication_padding")
    assert result.stop_reason == "error", result.summary
    assert "13 > 12" in result.summary and "research_publication" in result.summary
    assert result.completion_proof is None and not result.synthesis_id
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("grouped_request" in row for row in rows) == MAX_REQUIRED_TOOLS + 1
    assert not any("publication" in row for row in rows)
