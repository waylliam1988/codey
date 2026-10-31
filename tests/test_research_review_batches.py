"""Bounded review scopes preserve exact coverage, dependencies, and one publisher."""

import copy
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from codey.research.review_batches import batch_receipts, batch_scopes, batch_view, merge_assessments, next_batch
from codey.research.semantic_review import parse_review, review_issues, submitted_review
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_research_review_scope import case
from tests.test_research_semantic_review import _run, assessment


def large_basis():
    basis, _ = case()
    basis.update(basis_digest="frozen-root", investigations={"a1": {"question_ref": "q1"}})
    basis["claims"][0]["units"] = basis["claims"][0]["units"][:1]
    basis["claims"][0]["evidence"] = [{"excerpt": "Only one writer at a time."}]
    basis["requirements"] = [{"task_ref": f"r{n}", "text": "Explain the writer limit."} for n in range(1, 21)]
    basis["paragraphs"][0]["units"] = basis["paragraphs"][0]["units"][:1]
    return basis


@pytest.mark.parametrize("native", [False, True])
def test_current_batch_schema_rejects_invented_refs_and_unselected_categories(native):
    from jsonschema import Draft202012Validator

    from codey.operations.kernel_protocol import build_turn_snapshot, decode_text_turn, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
    from codey.providers.text_tool_schema import text_tool_schema

    root = large_basis()
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.write"})),
                          task_kind="research_review", research_review_basis=root)
    snapshot = build_turn_snapshot(session, native=native)
    tools = tools_from_specs(snapshot.model_specs)
    validator = Draft202012Validator(text_tool_schema(tools))
    raw = {"tool": "submit_research_review", "args": {"assessment": assessment(next_batch(session, root))}}
    assert validator.is_valid(raw)
    long_reason = copy.deepcopy(raw)
    long_reason["args"]["assessment"]["claims"][0]["units"][0]["reason"] = "x" * 4001
    # Sampler projection avoids expensive bounded-string grammars; the same
    # frozen kernel contract retains the authoritative length check.
    assert validator.is_valid(long_reason)
    assert normalize_turn(decode_text_turn(json.dumps(long_reason)), snapshot=snapshot).protocol_error
    for mutate in (lambda body: body["claims"][0].update(ref="f999"),
                   lambda body: body["claims"][0].update(reason="extra"),
                   lambda body: body["requirements"].append({}),
                   lambda body: body["claims"][0]["units"].clear()):
        corrupt = copy.deepcopy(raw)
        mutate(corrupt["args"]["assessment"])
        assert not validator.is_valid(corrupt)
        reply = (AssistantTurn(tool_calls=(ProviderToolCall("call", corrupt["tool"], corrupt["args"]),))
                 if native else decode_text_turn(json.dumps(corrupt)))
        assert normalize_turn(reply, snapshot=snapshot).protocol_error


def test_batches_cover_each_unit_once_and_prior_claims_are_not_resubmitted():
    root = large_basis()
    pristine = copy.deepcopy(root)
    previous = []
    scopes = batch_scopes(root)
    assert len(scopes) > 1
    for index in range(len(scopes)):
        view = batch_view(root, index, previous)
        assert all("answer" not in row and "findings" not in row for row in view["coverage"])
        assert all(ref in view["investigation_scope"] for row in view["coverage"] for ref in row["investigations"])
        if view["claims"] and not any(view[key] for key in ("paragraphs", "requirements", "investigations")):
            assert view["coverage"] == view["report_paths"] == view["report_paragraphs"] == []
        raw = assessment(view)
        parsed = parse_review(json.dumps(raw), view)
        if view["paragraphs"]:
            assert view["prior_claim_judgments"]
            corrupt = copy.deepcopy(raw)
            corrupt["paragraphs"][0]["units"][0]["finding_units"] = [{"ref": "f99", "unit": 1}]
            with pytest.raises(ValueError):
                parse_review(json.dumps(corrupt), view)
        previous.append(parsed)
    aggregate = merge_assessments(previous)
    assert not review_issues(parse_review(json.dumps(aggregate), root))
    assert root == pristine
    with pytest.raises(ValueError, match="duplicate"):
        merge_assessments([*previous, previous[0]])
    with pytest.raises(ValueError):
        parse_review(json.dumps(merge_assessments(previous[:-1])), root)


def test_receipts_bind_original_batches_and_dependencies_and_require_complete_scope():
    root = large_basis()
    session = SimpleNamespace(_memory_results={})
    for index in range(len(batch_scopes(root))):
        view = next_batch(session, root)
        raw = assessment(view)
        receipt = {"basis_digest": root["basis_digest"], "batch_digest": view["batch_digest"],
            "assessment": raw, "assessment_effect_id": str(index),
            "review_complete": index + 1 == len(batch_scopes(root))}
        session._memory_results[str(index)] = ToolResult(
            ToolCall("submit_research_review", {"assessment": raw}), "recorded", ok=True, canonical=receipt)
        if not receipt["review_complete"]:
            with pytest.raises(ValueError, match="every frozen batch"):
                submitted_review(session, root)
    assert next_batch(session, root) is None
    assert len(submitted_review(session, root)["assessment_effect_ids"]) == len(batch_scopes(root))
    for key in ("basis_digest", "batch_digest"):
        original = session._memory_results["0"].canonical[key]
        session._memory_results["0"].canonical[key] = "different"
        with pytest.raises(ValueError, match="frozen scope"):
            batch_receipts(session, root)
        session._memory_results["0"].canonical[key] = original
    raw = session._memory_results["0"].call.args["assessment"]
    raw["claims"][0]["units"][0]["reason"] = "Altered dependency."
    with pytest.raises(ValueError, match="original submission"):
        batch_receipts(session, root)
    session._memory_results["0"].canonical["assessment"] = copy.deepcopy(raw)
    with pytest.raises(ValueError, match="frozen scope"):
        batch_receipts(session, root)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.usefixtures("public_source_dns")
def test_shared_driver_batches_repair_and_publishes_once(tmp_path, native):
    task = "Explain writer concurrency. " * 12 + "Distinguish historical version restrictions."
    result = _run(tmp_path, task=task, budget=60, native=native)
    assert result.stop_reason == "done", result.summary
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("publication" in row for row in rows) == 1
    assert sum(row.get("attempt") == 8 for row in rows) > 1
    assert sum(row.get("attempt") == 10 for row in rows) > 1


@pytest.mark.usefixtures("public_source_dns")
def test_real_restart_between_batches_reuses_settled_judgment(tmp_path):
    task = "Explain writer concurrency. " * 12 + "Distinguish historical version restrictions."
    source = ("from pathlib import Path; from tests.test_research_semantic_review import _run; import sys; "
              "_run(Path(sys.argv[1]), task=sys.argv[2], budget=60, boundary='review_batch_committed')")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), task], capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    before = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert not any("publication" in row for row in before)
    result = _run(tmp_path, task=task, budget=60, resume=True)
    assert result.stop_reason == "done", result.summary
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum(row.get("attempt") == 8 and row.get("turn") == 1 for row in rows) == 1
    assert sum("fetch" in row for row in rows) == 1
    assert sum("publication" in row for row in rows) == 1
    original = (tmp_path / "calls.jsonl").read_bytes()
    assert _run(tmp_path, task=task, budget=60, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == original
