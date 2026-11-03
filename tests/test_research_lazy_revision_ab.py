"""Exercise the real snapshot, parser and executor around deferred revision tools."""

import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.answer_contract import answer_scope
from codey.research.revision_tools import (
    PREPARE,
    preparation_result,
    revision_guidance,
    revision_ready,
)
from codey.runtime.core.models import ToolCall, ToolResult
from tests.manual.research_lazy_revision_ab import treatment
from tests.manual.research_workflow_ablation import REVISION_GUIDANCE


def session():
    return TaskSession(TaskPolicy(frozenset({"knowledge.read", "knowledge.write", "web.read", "control"}),
                                  strict_research=True, required_checks=("research_submission",)),
                       task_kind="research", task_text="Read the old evidence; revise only when requested.")


def parse(snapshot, name, args):
    return normalize_turn(decode_text_turn(json.dumps({"tool": name, "args": args})), snapshot=snapshot)


@pytest.mark.parametrize("native", [False, True])
def test_preparation_has_no_write_and_only_next_snapshot_exposes_original_schema(native):
    s = session()
    with treatment("full_control"):
        before = build_turn_snapshot(s, native=native)
    original_commit = next(spec for spec in before.model_specs if spec.name == "knowledge_commit")
    with treatment("production"):
        first = build_turn_snapshot(s, native=native)
        assert "knowledge_commit" not in first.tool_names
        assert PREPARE in first.tool_names
        assert {spec.name: spec for spec in before.model_specs if spec.name != "knowledge_commit"} == {
            spec.name: spec for spec in first.model_specs if spec.name != PREPARE}
        assert not parse(first, PREPARE, {}).protocol_error
        assert parse(first, "knowledge_commit", {"commit_id": "x", "changes": []}).protocol_error
        delegate = ExecutionDelegate(session=s, research_tools=object(),
                                     research_scope=first.research_scope,
                                     frozen_specs={spec.name: spec for spec in first.frozen_specs})
        result, ok, _ = delegate.execute(ToolCall(PREPARE, {}))
        assert ok and result.canonical == {"knowledge_revision_ready": True}
        assert s._memory_results == {}  # Executor cannot alter durable session facts.
        assert not revision_ready(s)
        s._memory_results["settled-prepare"] = result
        second = build_turn_snapshot(s, native=native)
        assert "knowledge_commit" in second.tool_names and PREPARE not in second.tool_names
        assert next(spec for spec in second.model_specs if spec.name == "knowledge_commit") == original_commit
        assert "knowledge_commit" not in first.tool_names  # Frozen old response cannot acquire the new tool.
        restored = session()
        restored._memory_results = deepcopy(s._memory_results)
        assert revision_ready(restored)
        assert build_turn_snapshot(restored, native=native).tool_names == second.tool_names


@pytest.mark.parametrize("marker,ok,name", [(True, False, PREPARE), ("true", True, PREPARE),
                                           (True, True, "knowledge_read"), (False, True, PREPARE)])
def test_only_successful_settled_preparation_unlocks(marker, ok, name):
    s = session()
    s._memory_results["untrusted-data"] = ToolResult(ToolCall(name, {}), "irrelevant", ok=ok,
                                                    canonical={"knowledge_revision_ready": marker})
    with treatment("production"):
        assert not revision_ready(s)
        assert "knowledge_commit" not in build_turn_snapshot(s).tool_names


@pytest.mark.parametrize("native", [False, True])
def test_settled_failed_read_restores_original_recovery_context_and_survives_restore(native):
    s = session()
    original = build_turn_snapshot(s, native=native)
    s._memory_results["read"] = ToolResult(ToolCall("knowledge_read", {"id": "bad-id"}), "ERROR: not found", ok=False)
    recovered = build_turn_snapshot(s, native=native)
    with treatment("full_control"):
        control = build_turn_snapshot(s, native=native)
    assert recovered.tool_names == control.tool_names
    assert recovered.model_specs == control.model_specs
    assert revision_guidance(s, REVISION_GUIDANCE) == REVISION_GUIDANCE
    assert "knowledge_commit" not in original.tool_names
    restored = session()
    restored._memory_results = deepcopy(s._memory_results)
    assert build_turn_snapshot(restored, native=native).tool_names == recovered.tool_names
    restored.controller_denied = ("knowledge_commit",)
    assert "knowledge_commit" not in build_turn_snapshot(restored, native=native).tool_names
    restored.controller_denied = ()
    restored.policy = replace(restored.policy, grants=frozenset({"control", "knowledge.read"}))
    assert "knowledge_commit" not in build_turn_snapshot(restored, native=native).tool_names


@pytest.mark.parametrize("name,ok", [("knowledge_read", True), ("knowledge_search", False), ("open_url", False)])
def test_successful_reads_and_other_errors_keep_lightweight_tools(name, ok):
    s = session()
    s._memory_results["receipt"] = ToolResult(ToolCall(name, {}), "receipt", ok=ok)
    assert not revision_ready(s)
    assert PREPARE in build_turn_snapshot(s).tool_names


@pytest.mark.parametrize("denial", ["grant", "controller", "scope", "arguments", "prepare_controller"])
def test_preparation_cannot_bypass_grants_denials_task_identity_or_args(denial):
    s = session()
    scope = answer_scope(s)
    args = {}
    if denial == "grant":
        s.policy = replace(s.policy, grants=frozenset({"control", "knowledge.read"}))
    elif denial == "controller":
        s.controller_denied = ("knowledge_commit",)
    elif denial == "prepare_controller":
        s.controller_denied = (" PREPARE_KNOWLEDGE_REVISION ",)
    elif denial == "scope":
        s.task_text = "Different task"
    else:
        args = {"changes": [{"unexpected": "write"}]}
    assert not preparation_result(s, scope, ToolCall(PREPARE, args)).ok
    with treatment("production"):
        if denial in {"grant", "controller", "prepare_controller"}:
            assert PREPARE not in build_turn_snapshot(s).tool_names
        if denial == "prepare_controller":
            assert "knowledge_commit" in build_turn_snapshot(s).tool_names


def test_no_keyword_routing_and_other_tasks_or_explicit_exposure_keep_original_behavior():
    s = session()
    with treatment("production"):
        s.task_text = "修改工具 is a quoted word; just read."
        assert PREPARE in build_turn_snapshot(s).tool_names
        assert revision_guidance(s, REVISION_GUIDANCE) != REVISION_GUIDANCE
        s.model_tool_names = ("knowledge_commit", "knowledge_read", "submit_research_answer")
        assert PREPARE not in build_turn_snapshot(s).tool_names
        assert "knowledge_commit" in build_turn_snapshot(s).tool_names
        assert revision_guidance(s, REVISION_GUIDANCE) == REVISION_GUIDANCE
        s.model_tool_names = None
        s.policy = replace(s.policy, strict_research=False, required_checks=())
        s.task_kind = "project"
        assert PREPARE not in build_turn_snapshot(s).tool_names
        assert revision_guidance(s, REVISION_GUIDANCE) == REVISION_GUIDANCE


def test_preparation_does_not_create_an_investigation_or_citation():
    s = session()
    s._memory_results["prepare"] = preparation_result(s, answer_scope(s), ToolCall(PREPARE, {}))
    assert answer_scope(s).aliases == ()
    assert answer_scope(s).attempt_refs == ()


def run_prepared_recovery(root, native, *, boundary="", resume=False):
    from tests.test_research_deterministic_completion import _run
    from tests.test_research_findings import URL

    def factory(provider):
        original_turn = provider.send_turn

        def send(prompt):
            path = root / "provider-script.jsonl"
            index = len(path.read_text(encoding="utf8").splitlines()) if path.exists() else 0
            sequence = [(PREPARE, {}), ("open_url", {"url": URL}),
                        ("submit_research_answer", {"state": "answered", "markdown": "Only one writer at a time [e1]."})]
            with path.open("a", encoding="utf8") as stream:
                stream.write(json.dumps({"index": index, "prompt": prompt}) + "\n")
            name, args = sequence[index]
            return json.dumps({"tool": name, "args": args})

        def turn(prompt, tools):
            path = root / "provider-script.jsonl"
            started = path.exists() and bool(path.read_text(encoding="utf8").strip())
            names = [tool.name for tool in tools]
            assert ("knowledge_commit" in names) is bool(started)
            assert (PREPARE in names) is not bool(started)
            return original_turn(prompt, tools)

        provider.send = send
        provider.send_turn = turn
        return provider

    with treatment("production"):
        return _run(root, native=native, provider_factory=factory, required_checks=("research_submission",),
                    compact_planning=True, boundary=boundary,
                    boundary_turn=1 if boundary == "tool_effect_pending" else 2, resume=resume)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("boundary", ["provider_request_ready", "tool_effect_pending"])
def test_real_kernel_restores_preparation_after_process_exit_without_repeating_it(tmp_path, native, boundary):
    source = ("from pathlib import Path; import sys; "
              "from tests.test_research_lazy_revision_ab import run_prepared_recovery; "
              "run_prepared_recovery(Path(sys.argv[1]), sys.argv[2]=='True', boundary=sys.argv[3])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), str(native), boundary],
                           capture_output=True, timeout=60)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_prepared_recovery(tmp_path, native, resume=True)
    assert result.stop_reason == "done" and result.turns == 3
    rows = (tmp_path / "provider-script.jsonl").read_bytes()
    assert len(rows.splitlines()) == 3
    assert run_prepared_recovery(tmp_path, native, resume=True) == result
    assert (tmp_path / "provider-script.jsonl").read_bytes() == rows


def test_preparation_replays_only_empty_original_arguments():
    from codey.runtime.effects.safe_tool_replay import replay_args_for_tool_call

    assert replay_args_for_tool_call(ToolCall(PREPARE, {})) == {}
    assert replay_args_for_tool_call(ToolCall(PREPARE, {"changes": []})) is None


def test_production_keeps_the_measured_preparation_schema_and_response():
    from codey.toolchain.tool_spec import spec_for_tool
    from tests.manual.research_lazy_revision_ab import preparation_result as experimental_result
    from tests.manual.research_lazy_revision_ab import preparation_spec

    s = session()
    call = ToolCall(PREPARE, {})
    assert spec_for_tool(PREPARE) == preparation_spec()
    assert preparation_result(s, answer_scope(s), call) == experimental_result(s, answer_scope(s), call)
