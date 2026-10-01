"""Red-first locks for concrete omissions found by independent legacy replay."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from codey.operations.kernel_protocol import normalize_turn
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.support.kernel_parity_cases import json_call
from tools.kernel_parity import ROOT, load_baseline, probe


@pytest.fixture(scope="module")
def replay():
    baseline = load_baseline()
    keys = {"loop/batch-read", "loop/parallel-read", "loop/repeat-information", "loop/conversation",
            "loop/cancel-after-send", "loop/verification-forbidden", "loop/candidate-loader"}
    cases = [case for case in baseline["cases"] if case["id"] in keys]
    return baseline["observations"], probe(ROOT, cases)


@pytest.mark.parametrize("key", ["loop/batch-read", "loop/parallel-read", "loop/repeat-information",
                                 "loop/conversation", "loop/cancel-after-send", "loop/verification-forbidden",
                                 "loop/candidate-loader"])
def test_real_loop_restores_legacy_behavior(replay, key):
    import json
    from pathlib import Path

    from tools.kernel_parity import ROOT

    old, new = replay
    if key in ("loop/conversation", "loop/verification-forbidden", "loop/candidate-loader"):
        deltas = json.loads((ROOT / "tests/fixtures/kernel_parity/intentional_deltas.json").read_text(encoding="utf-8"))
        assert key in deltas, f"intentional edit-alias deny must be reviewed for {key}"
        assert new[key] == deltas[key]["after"]
        assert old[key] == deltas[key]["before"]
        return
    assert new[key] == old[key]


@pytest.mark.parametrize("wire", ["json", "native"])
def test_list_dir_omitted_path_defaults_to_project_root(wire):
    reply = json_call("list_dir") if wire == "json" else AssistantTurn(
        text="", tool_calls=(ProviderToolCall(id="c1", name="list_dir", arguments={}),))
    plan = normalize_turn(reply, policy=TaskPolicy(grants=frozenset({"project.read"})))
    assert not plan.protocol_error
    assert plan.calls[0].args == {"path": "."}


@pytest.mark.parametrize("wire", ["json", "native"])
def test_done_rejects_nested_executable_tool_in_summary(wire):
    args = {"summary": json_call("read_file", path="a.py")}
    reply = json_call("done", **args) if wire == "json" else AssistantTurn(
        text="", tool_calls=(ProviderToolCall(id="c1", name="done", arguments=args),))
    plan = normalize_turn(reply, policy=TaskPolicy(grants=frozenset({"control"})))
    assert plan.protocol_error
    assert not plan.calls and plan.control is None


def test_research_done_keeps_bounded_followup_questions_for_synthesis():
    plan = normalize_turn(json_call("done", summary="Report", open_questions=["Next question"]),
                          policy=TaskPolicy(grants=frozenset({"control"})))
    assert not plan.protocol_error
    assert plan.control_args["open_questions"] == ["Next question"]


@pytest.mark.parametrize("verification", ["failed", "stale", "no-identity"])
def test_coding_context_never_calls_failed_or_stale_verification_fresh(verification):
    from codey.operations.kernel_prompt import _coding_context_for_session
    from codey.operations.task_session import TaskSession

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "project.write"})))
    session.edited_files = {"a.py": 2}
    session.workspace_fingerprint = "a" * 64
    session.workspace_revision = 2
    session.verifications = [{"passed": verification != "failed", "exit_code": 1 if verification == "failed" else 0,
                              "revision": 1 if verification == "stale" else 2,
                              "workspace_revision": 2, "workspace_fingerprint": "" if verification == "no-identity" else "a" * 64}]
    assert "Changed files needing verification" in _coding_context_for_session(session)


def test_coding_context_without_project_read_grant_is_not_rendered():
    from codey.operations.kernel_prompt import _coding_context_for_session

    session = SimpleNamespace(policy=TaskPolicy(grants=frozenset({"control"})), task_kind="project",
                              read_files={"secret.py"}, edited_files={}, verifications=[])
    assert _coding_context_for_session(session) == ""


@pytest.mark.parametrize("calls", [
    [{"tool": "read_file", "args": {"path": "a.py"}}, {"tool": "edit", "args": {"path": "a.py", "content": "x"}}],
    [{"tool": "parallel", "args": {"calls": [{"tool": "read_file", "args": {"path": "a.py"}}]}}],
    [{"tool": "read_file", "args": {"path": "a.py"}}, {"tool": "invented_tool", "args": {}}],
])
def test_parallel_rejects_entire_unsafe_or_nested_batch(calls):
    plan = normalize_turn(json_call("parallel", calls=calls), policy=SimpleNamespace(allows=lambda _g: True))
    assert plan.protocol_error and not plan.calls


def test_batch_overflow_never_silently_truncates():
    plan = normalize_turn(json_call("read_files", paths=[f"{i}.py" for i in range(9)]),
                          policy=SimpleNamespace(allows=lambda _g: True))
    assert plan.protocol_error and not plan.calls


def test_no_native_wrapper_synthesizes_child_call_ids():
    plan = normalize_turn(AssistantTurn(text="", tool_calls=(ProviderToolCall(
        id="c1", name="read_files", arguments={"paths": ["a.py", "b.py"]}),)),
        policy=SimpleNamespace(allows=lambda _g: True))
    assert plan.protocol_error and not plan.calls


def test_canonical_research_done_accepts_questions_in_native_schema():
    from codey.toolchain.tool_spec import native_tools_for_policy

    schema = next(row["function"]["parameters"] for row in native_tools_for_policy(
        TaskPolicy(grants=frozenset({"control"}))) if row["function"]["name"] == "done")
    assert "open_questions" in schema["properties"]


def test_unrelated_successful_check_cannot_satisfy_selected_candidate():
    from codey.completion.verification_policy import VerificationCandidate
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession

    fp = "sha256:" + "a" * 64
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write"})))
    session.edited_files = {"a.py": 1}
    session.set_workspace_state(1, fp)
    session.selected_verification = VerificationCandidate("python -m pytest -q", cwd=".", source="project")
    session.verifications = [{"command": "python -m compileall .", "cwd": ".", "revision": 1,
                              "passed": True, "exit_code": 0,
                              "workspace_revision": 1, "workspace_fingerprint": fp}]
    assert not evaluate(session, "finished").complete
    # Success control: the specified check with the same identity must pass,
    # proving the rejection above is replacement (not missing identity).
    session.verifications = [{"command": "python -m pytest -q", "cwd": ".", "revision": 1,
                              "passed": True, "exit_code": 0,
                              "workspace_revision": 1, "workspace_fingerprint": fp}]
    assert evaluate(session, "finished").complete


@pytest.mark.parametrize("kind", ["project", "research"])
def test_repeated_cycle_has_bounded_stop_and_distinct_information_does_not(monkeypatch, kind):
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    tool = "read_file" if kind == "project" else "knowledge_read"
    grant = "project.read" if kind == "project" else "knowledge.read"

    class Provider:
        def __init__(self):
            self.i = 0
        def send(self, _prompt):
            key = "path" if kind == "project" else "id"
            value = ("a.py", "b.py")[self.i % 2]
            self.i += 1
            return json_call(tool, **{key: value})

    for changing in (False, True):
        provider = Provider()
        result = run_task_kernel(TaskSession(policy=TaskPolicy(grants=frozenset({grant})), task_kind=kind, max_turns=12),
            provider=provider, executors={tool: lambda call, provider=provider, changing=changing: ToolResult(call=call,
                model_text=f"information-{provider.i}" if changing else "unchanged information")})
        assert result.stop_reason == ("max_turns" if changing else "no_progress")
        assert result.turns == 12 if changing else result.turns < 12


def test_synthesis_persists_bounded_open_questions(tmp_path):
    from codey.knowledge.changes import KnowledgeChanges
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_iteration import _persist_synthesis
    from codey.research.tools import ResearchTools

    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        tools = ResearchTools(search=SimpleNamespace(), store=store, changes=KnowledgeChanges(root=store.root))
        from codey.policies.task_policy import TaskPolicy

        policy = TaskPolicy(grants=frozenset({
            "control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link",
        }))
        sid = _persist_synthesis(tools, "question", "report", session_id="s", project="", run_id="run-parity-syn",
                                 on_event=lambda _e: None,
                                 open_questions=[f"question {i}" for i in range(6)],
                                 policy=policy)
        assert store.read_note(sid).open_questions == [f"question {i}" for i in range(4)]
    finally:
        store.close()


def test_cancellation_between_tools_settles_remaining_slots_without_executing(monkeypatch):
    import threading

    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    stop = threading.Event()
    calls = []
    reply = json_call("edit", path="a.py", content="a") + "\n" + json_call("edit", path="b.py", content="b")

    def edit(call):
        calls.append(call.args["path"])
        stop.set()
        return ToolResult(call, "wrote file", audit={"changed": True})

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.write"})))
    outcome = run_task_kernel(session, provider=SimpleNamespace(send=lambda _p: reply),
                              stop_flag=stop, executors={"edit": edit})
    assert calls == ["a.py"]
    assert outcome.stop_reason == "stopped"
    assert len(session.executed) == 2
    assert sorted(item["ok"] for item in session.executed.values()) == [False, True]


def test_text_duplicates_lower_once_while_distinct_native_ids_stay_distinct():
    policy = TaskPolicy(grants=frozenset({"project.read"}))
    text = json_call("read_file", path="a.py")
    plan = normalize_turn(text + "\n" + text, policy=policy)
    assert len(plan.calls) == 1
    native = normalize_turn(AssistantTurn(text="", tool_calls=tuple(ProviderToolCall(
        id=key, name="read_file", arguments={"path": "a.py"}) for key in ("c1", "c2"))), policy=policy)
    assert [call.call_id for call in native.calls] == ["c1", "c2"]


def test_native_duplicate_ids_rejected_before_execution():
    plan = normalize_turn(AssistantTurn(text="", tool_calls=tuple(ProviderToolCall(
        id="c1", name="read_file", arguments={"path": path}) for path in ("a.py", "b.py"))),
        policy=TaskPolicy(grants=frozenset({"project.read"})))
    assert plan.protocol_error and not plan.calls


def test_run_result_never_reports_an_earlier_green_after_a_later_edit():
    from codey.operations.project_adapter import _session_checks_passed
    from codey.operations.task_session import TaskSession

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "project.write"})))
    session.edited_files = {"a.py": 2}
    session.verifications = [{"revision": 1, "passed": True, "exit_code": 0}]
    assert not _session_checks_passed(session)


@pytest.mark.parametrize("wrappers", ["conversation", "recorded", "both"])
def test_explicit_provider_normalizer_survives_production_wrappers(wrappers):
    from codey.operations.kernel_transport import call_provider_send
    from codey.operations.provider_session import ConversationProvider
    from codey.operations.task_effects import KernelRecordedProvider
    from codey.providers.local_response_codec import normalize_local_reply

    normalized = []
    mutations = SimpleNamespace(begin_provider_effect=lambda *_a, **_k: None,
                                settle_provider_effect=lambda *_a, **_k: None)
    sink = SimpleNamespace(send_index=0, run_id="r", session_id="s", phase="writer", provider_id="local",
                           delivery_batch_id="", mutations=mutations)

    def normalize(reply):
        normalized.append(reply)
        return normalize_local_reply(reply)

    provider = SimpleNamespace(send=lambda _p: '<|tool_call>call:tool:read_file{path:"a.py"}<tool_call|>',
                               normalize_reply=normalize)
    if wrappers in {"recorded", "both"}:
        provider = KernelRecordedProvider(provider, sink)
    if wrappers in {"conversation", "both"}:
        provider = ConversationProvider(provider, SimpleNamespace(record_exchange=lambda *_a: None))
    reply = call_provider_send(provider, "read")
    assert isinstance(reply, AssistantTurn)
    assert reply.tool_calls[0].arguments == {"path": "a.py"}
    assert len(normalized) == 1


def test_native_protocol_repair_answers_duplicate_id_once():
    from codey.operations.kernel_transport import repair_native_dangling

    receipts = []
    reply = AssistantTurn(text="", tool_calls=tuple(ProviderToolCall(
        id="c1", name="read_file", arguments={"path": path}) for path in ("a.py", "b.py")))
    provider = SimpleNamespace(send_tool_results=lambda messages, _tools: receipts.extend(messages))
    repair_native_dangling(provider, reply, True, [], "duplicate id")
    assert [row["tool_call_id"] for row in receipts] == ["c1"]


def test_native_cancellation_closes_followon_ids_without_executing():
    import threading

    from codey.operations.task_loop import _cancel_after_send

    stop = threading.Event()
    stop.set()
    receipts = []

    def send_results(messages, _tools):
        receipts.extend(messages)
        return (AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c2", name="edit", arguments={}),))
                if len(receipts) == 1 else AssistantTurn(text="closed", tool_calls=()))

    reply = AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c1", name="edit", arguments={}),))
    result = _cancel_after_send(stop, SimpleNamespace(send_tool_results=send_results), reply, True, [], 1,
                               propagate=False)
    assert result.stop_reason == "stopped"
    assert [row["tool_call_id"] for row in receipts] == ["c1", "c2"]


def test_verification_refresh_drops_a_candidate_removed_by_a_later_edit():
    from codey.completion.verification_policy import VerificationCandidate
    from codey.operations.project_verification import refresh_verification_candidates
    from codey.operations.task_session import TaskSession

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.write"})))
    session.edited_files = {"a.py": 1}
    session.verification_candidate_loader = lambda: (VerificationCandidate("python -m pytest -q"),)
    refresh_verification_candidates(session)
    assert session.selected_verification is not None
    session.edited_files["a.py"] = 2
    session.verification_candidate_loader = lambda: ()
    refresh_verification_candidates(session)
    assert session.selected_verification is None


def test_recovery_keeps_questions_and_forbidden_hint_without_importing_candidate_proof():
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.operations.task_session import TaskSession

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    session.verification_forbidden = True
    session.last_done_args = {"open_questions": ["next"]}
    restored = TaskSession.from_payload(session.to_payload())
    assert restored.last_done_args == {"open_questions": ["next"]}
    assert restored.verification_forbidden
    assert restored.selected_verification is None
    payload = session.to_payload()
    payload["last_done_args"] = {"open_questions": "truthy malformed"}
    with pytest.raises(RecoveryFailed):
        TaskSession.from_payload(payload)


def test_research_synthesis_finalizes_claim_support_before_persisting():
    baseline = load_baseline()
    cases = [case for case in baseline["cases"] if case["boundary"] == "research_loop"]
    current = probe(ROOT, cases)
    deltas = json.loads((ROOT / "tests/fixtures/kernel_parity/intentional_deltas.json").read_text(encoding="utf-8"))
    for case in cases:
        if case["id"] in deltas:
            continue
        assert current[case["id"]] == baseline["observations"][case["id"]], case["id"]
