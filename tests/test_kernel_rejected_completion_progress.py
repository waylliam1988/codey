"""Rejected reports share the durable kernel progress budget, in both protocols."""

import json

import pytest

from codey.operations.completion_gate import GateVerdict
from codey.operations.kernel_contracts import KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.support.kernel_runtime import run_task_kernel


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("changing", [False, True])
def test_repeated_rejected_completion_stops_but_real_revisions_get_another_attempt(monkeypatch, native, changing):
    monkeypatch.setattr("codey.operations.kernel_transport.provider_uses_native", lambda *a, **k: native)
    monkeypatch.setattr("codey.operations.completion_gate.evaluate", lambda *a, **k: GateVerdict(
        complete=False, followup="report_sections.counter needs a citation", proof=None))

    class Provider:
        def __init__(self):
            self.turns = 0
            self.closed = []

        def reply(self):
            self.turns += 1
            args = {"summary": f"draft-{self.turns}" if changing else "same rejected draft"}
            return (AssistantTurn(tool_calls=(ProviderToolCall(
                id=f"done-{self.turns}", name="done", arguments=args),)) if native
                else json.dumps({"tool": "done", "args": args}))

        def send(self, prompt):
            return self.reply()

        def send_turn(self, prompt, tools):
            return self.reply()

        def send_tool_results(self, messages, tools):
            return self.reply()

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            self.closed.extend(row.call_id for row in results)
            return AssistantTurn()

    provider = Provider()
    session = TaskSession(policy=TaskPolicy(frozenset({"control"})), max_turns=9)
    result = run_task_kernel(session, request=KernelRunRequest(transport=KernelTransportDeps(provider)))
    assert not result.completed
    assert result.stop_reason == ("max_turns" if changing else "no_progress"), result.summary
    assert provider.turns == (9 + int(native) if changing else 3)
    if not changing:
        assert "same rejected draft" in result.summary  # Preserve partial work without accepting it.
        assert "counter needs a citation" in result.summary
    if native:
        assert f"done-{provider.turns}" in provider.closed


def test_completion_repeat_history_survives_checkpoint_roundtrip_without_fake_tool_batch():
    from codey.operations.kernel_progress import KernelProgress
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.core.progress import ProgressSnapshot

    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"})))
    progress = KernelProgress()
    observation = ToolResult(ToolCall("knowledge_read", {"id": "premise"}), "earlier evidence", ok=True)
    progress.observe([observation], session, batch_id="actual-settled-batch")
    for turn in (1, 2):
        session.turn = turn
        progress.reject_completion({"summary": "unchanged"}, "counter needs citation", session)
    saved = ProgressSnapshot.from_payload(json.loads(json.dumps(progress.snapshot().to_payload())))
    restored = KernelProgress(snapshot=saved)
    assert not restored.stopped
    session.turn = 3
    restored.reject_completion({"summary": "unchanged"}, "counter needs citation", session)
    assert restored.stopped
    assert restored.snapshot().last_batch_id == "actual-settled-batch"
    # A genuinely new read resets stagnation and interrupts the exact cycle.
    restored.observe([ToolResult(observation.call, "new evidence", ok=True)], session, batch_id="next-real-batch")
    assert not restored.stopped


def test_novel_findings_cannot_reset_same_local_report_rejection_forever():
    from codey.operations.kernel_progress import KernelProgress
    from codey.runtime.core.models import ToolCall, ToolResult

    session = TaskSession(TaskPolicy(frozenset({"control"})))
    progress = KernelProgress(stagnant_turns=3)
    for number in range(4):
        progress.observe([ToolResult(ToolCall("save_finding", {"claim": f"paraphrase-{number}"}),
                                     f"saved f{number + 1}", ok=True)], session, batch_id=f"batch-{number}")
        progress.reject_completion({"findings": [f"f{number + 1}"]}, f"Report still too long {number}", session,
                                   failure_key="report_length:Report format r1")
    assert progress.stopped


@pytest.mark.parametrize("observations", [0, 5, 20])
def test_domain_repair_budget_survives_history_eviction_and_restarts(observations):
    from codey.operations.kernel_progress import KernelProgress
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.core.progress import ProgressSnapshot

    session = TaskSession(TaskPolicy(frozenset({"control"})))
    progress = KernelProgress(stagnant_turns=3)
    for number in range(3):
        for index in range(observations):
            batch = f"batch-{number}-{index}"
            result = ToolResult(ToolCall("knowledge_read", {"id": batch}), batch, ok=True)
            progress.observe([result], session, batch_id=batch)
            saved = ProgressSnapshot.from_payload(json.loads(json.dumps(progress.snapshot().to_payload())))
            progress = KernelProgress(stagnant_turns=3, snapshot=saved)
            progress.observe([result], session, batch_id=batch)
            assert progress.snapshot() == saved  # redelivery does not change either budget
        progress.reject_completion({"findings": [f"f{number + 1}"]}, f"Too long: {1200 + number}", session,
                                   failure_key="report_length:Report format r1")
        saved = ProgressSnapshot.from_payload(json.loads(json.dumps(progress.snapshot().to_payload())))
        progress = KernelProgress(stagnant_turns=3, snapshot=saved)
        assert saved.repair_rejections == number + 1
        assert saved.schema_version == 2
        assert progress.stopped == (number == 2)
    # New tool output does not erase an exhausted domain budget.
    progress.observe([ToolResult(ToolCall("save_finding", {"claim": "another variant"}), "new f9", ok=True)], session)
    assert progress.stopped
    # A different validation issue is a new episode, rather than a label/text change.
    progress.reject_completion({}, "Missing citation", session, failure_key="citation:coverage[0]")
    assert not progress.stopped and progress.snapshot().repair_rejections == 1


def test_legacy_progress_preserves_payload_and_upgrades_only_with_domain_observation():
    from codey.operations.kernel_progress import KernelProgress
    from codey.runtime.core.progress import ProgressSnapshot

    payload = {"limit": 3, "seen": [], "attempts": [], "idle": 0, "last_batch_id": ""}
    legacy = ProgressSnapshot.from_payload(payload)
    assert legacy.to_payload() == payload
    progress = KernelProgress(3, snapshot=legacy)
    assert progress.snapshot() == legacy
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    progress.reject_completion({}, "old failure", session)
    assert progress.snapshot().schema_version == 1
    progress.reject_completion({}, "domain failure", session, failure_key="length:report")
    assert progress.snapshot().schema_version == 2
    assert progress.snapshot().repair_rejections == 1


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"schema_version": 3}, {"repair_rejections": True},
    {"repair_rejections": -1}, {"repair_rejections": 4}, {"repair_rejections": 0},
    {"repair_failure_key": ""}, {"repair_failure_key": 42}, {"unexpected": 0},
])
def test_repair_progress_codec_rejects_invalid_observations(changes):
    from codey.runtime.core.progress import ProgressSnapshot

    payload = ProgressSnapshot(limit=3, schema_version=2, repair_failure_key="length:report", repair_rejections=1).to_payload()
    with pytest.raises(ValueError):
        ProgressSnapshot.from_payload({**payload, **changes})


def run_domain_repair_probe(root, *, native=False, boundary="", resume=False):
    """Production kernel repeatedly repairs the same real length constraint."""
    import hashlib

    from codey.research.passages import passage_ref
    from tests.test_research_deterministic_completion import _run
    from tests.test_research_findings import URL

    text = "Readers and a writer may run concurrently. Only one writer at a time."
    passage = passage_ref(URL, hashlib.sha256(text.encode()).hexdigest()[:16], 0, len(text))

    def factory(provider):
        send = provider.send

        def reply(prompt):
            row = json.loads(send(prompt))
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            turn = next(event["turn"] for event in reversed(calls) if "turn" in event)
            if turn == 1:
                row["args"] = {"questions": ["What limits writer concurrency?"], "question_details": [{"task_refs": ["r1"]}]}
            elif turn == 5 or turn >= 6 and (turn - 6) % 7 == 6:
                number = 1 if turn == 5 else 1 + 6 * ((turn - 6) // 7 + 1)
                row = {"tool": "submit_research_answer", "args": {"state": "answered", "findings": [f"f{number}"]}}
            elif turn == 4 or turn >= 6:
                row = {"tool": "save_finding", "args": {"question_ref": "q1", "passage_ref": passage,
                    "claim": "同一时刻只有一个写者。" * 6 + str(turn)}}
            return json.dumps(row)

        provider.send = reply
        return provider

    return _run(root, native=native, boundary=boundary, boundary_turn=12 if boundary == "control_ready" else 13,
                resume=resume, budget=40, task="Explain writer limit. 给出约20字的中文研究结论。", provider_factory=factory)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("boundary", ["control_ready", "provider_request_ready"])
def test_domain_budget_survives_process_exit_on_rejection_and_request_admission(tmp_path, native, boundary):
    import subprocess
    import sys

    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint

    source = ("from pathlib import Path; import sys; "
              "from tests.test_kernel_rejected_completion_progress import run_domain_repair_probe; "
              "run_domain_repair_probe(Path(sys.argv[1]), native=sys.argv[2]=='True', boundary=sys.argv[3])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), str(native), boundary],
                           capture_output=True, timeout=60)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        saved = operation_from_checkpoint(durable.task("r"), "s").counters.progress
        assert saved.schema_version == 2 and saved.repair_rejections == 2
        assert saved.repair_failure_key.startswith("report_length:")
    finally:
        durable.close()
    result = run_domain_repair_probe(tmp_path, native=native, resume=True)
    expected_turns = 5 + 7 * (saved.limit - 1)
    assert result.stop_reason == "no_progress" and result.turns == expected_turns, result.summary
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("attempt" in row for row in calls) == expected_turns
    assert not any("publication" in row for row in calls)
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert run_domain_repair_probe(tmp_path, native=native, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before
