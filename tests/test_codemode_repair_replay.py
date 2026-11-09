from __future__ import annotations

import pytest


def _run_codemode(
    session: object,
    project: object,
    *,
    turn: int,
    call_id: str,
    code: str,
    inputs: dict[str, object],
    executor: object = None,
    repair_of: str | None = None,
    research_tools: object = None,
    session_id: str = "",
):
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.runtime.core.models import ToolCall

    snapshot = build_turn_snapshot(session)
    builder_args = {
        "session": session, "snapshot": snapshot, "run_id": "repair-run", "effect_scope": "task",
        "turn": turn, "project_path": project, "session_id": session_id,
    }
    if executor is not None:
        builder_args["executors"] = {"open_url": executor}
    if research_tools is not None:
        builder_args["research_tools"] = research_tools
    parent = build_execute_turn_executor(**builder_args)
    args: dict[str, object] = {"code": code, "inputs": inputs}
    if repair_of is not None:
        args["repair_of"] = repair_of
    return execute_turn(
        session,
        [ToolCall("codemode", args, call_id=call_id)],
        run_id="repair-run", effect_scope="task", turn=turn, project_path=project,
        executors={"codemode": parent}, snapshot=snapshot, research_tools=research_tools,
    )[0]


def test_parse_codemode_preserves_repair_token() -> None:
    from codey.codemode.protocol import parse_codemode_args

    program = parse_codemode_args({"code": "return 1;", "repair_of": "cmr_abc123"})
    assert program.repair_of == "cmr_abc123"


def test_kernel_argument_normalization_preserves_repair_token() -> None:
    from codey.operations.kernel_protocol import _validate_tool_args

    normalized, error = _validate_tool_args("codemode", {
        "code": "return 1;",
        "repair_of": "cmr_abc123",
    })
    assert not error
    assert normalized["repair_of"] == "cmr_abc123"


def test_return_only_repair_reuses_the_original_child_receipt(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: None)

    session = TaskSession(
        TaskPolicy(frozenset({"control", "project.read", "web.read"})),
        project=str(tmp_path),
    )
    class Source:
        calls = 0

        def search(self, _query: str, *, limit: int):
            return []

        def fetch(self, url: str):
            self.calls += 1
            return {"status": "ok", "url": url, "title": "Article", "content_kind": "html",
                    "text": f"V{self.calls}\n" * 80}

    from codey.research.tools import ResearchTools
    source = Source()
    research_tools = ResearchTools(source, None, None)

    failed = _run_codemode(
        session, tmp_path, turn=1, call_id="first", research_tools=research_tools,
        code="const result = await tools.open_url({url: inputs.url}); return result.value.missing;",
        inputs={"url": "https://example.test/article"},
    )
    assert not failed.ok
    assert failed.audit["error_code"] in {"missing_return", "result_encoding"}, failed.model_text
    token = failed.canonical["repair"]["token"]
    assert isinstance(token, str) and token.startswith("cmr_")
    assert source.calls == 1
    repaired = _run_codemode(
        session, tmp_path, turn=2, call_id="repair", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/article"}, repair_of=token,
    )
    assert repaired.ok, repaired.model_text
    assert "V1" in repaired.canonical["value"]
    assert repaired.audit["recovery_disposition"] == "receipt_replay"
    assert source.calls == 1


def test_repair_token_is_bound_to_the_original_session(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.tools import ResearchTools

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: None)
    policy = TaskPolicy(frozenset({"control", "project.read", "web.read"}))

    class Source:
        calls = 0

        def search(self, _query: str, *, limit: int):
            return []

        def fetch(self, url: str):
            self.calls += 1
            return {"status": "ok", "url": url, "title": "Article", "content_kind": "html",
                    "text": "V1\n" * 80}

    source = Source()
    research_tools = ResearchTools(source, None, None)
    original = TaskSession(policy, project=str(tmp_path))
    failed = _run_codemode(
        original, tmp_path, turn=1, call_id="first", research_tools=research_tools,
        session_id="session-a",
        code="const result = await tools.open_url({url: inputs.url}); return result.value.missing;",
        inputs={"url": "https://example.test/article"},
    )
    token = failed.canonical["repair"]["token"]

    foreign = TaskSession(policy, project=str(tmp_path))
    foreign._memory_results.update(original._memory_results)
    foreign.executed.update(original.executed)
    rejected = _run_codemode(
        foreign, tmp_path, turn=2, call_id="repair", research_tools=research_tools,
        session_id="session-b",
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/article"}, repair_of=token,
    )
    assert not rejected.ok
    assert "session" in rejected.model_text.lower()
    assert source.calls == 1


def test_fresh_codemode_execution_reads_the_new_source_version(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: None)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))
    class Source:
        calls = 0

        def search(self, _query: str, *, limit: int):
            return []

        def fetch(self, url: str):
            self.calls += 1
            return {"status": "ok", "url": url, "title": "Article", "content_kind": "html",
                    "text": f"V{self.calls}\n" * 80}

    from codey.research.tools import ResearchTools
    source = Source()
    research_tools = ResearchTools(source, None, None)

    first = _run_codemode(
        session, tmp_path, turn=1, call_id="first", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/article"},
    )
    assert first.ok and "V1" in first.canonical["value"]
    fresh = _run_codemode(
        session, tmp_path, turn=2, call_id="fresh", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/article"},
    )
    assert fresh.ok and "V2" in fresh.canonical["value"]
    assert source.calls == 2


def test_repair_argument_change_fails_without_fetching_again(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: None)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))
    class Source:
        calls = 0

        def search(self, _query: str, *, limit: int):
            return []

        def fetch(self, url: str):
            self.calls += 1
            return {"status": "ok", "url": url, "title": "Article", "content_kind": "html",
                    "text": "V1\n" * 80}

    from codey.research.tools import ResearchTools
    source = Source()
    research_tools = ResearchTools(source, None, None)

    failed = _run_codemode(
        session, tmp_path, turn=1, call_id="first", research_tools=research_tools,
        code="const result = await tools.open_url({url: inputs.url}); return result.value.missing;",
        inputs={"url": "https://example.test/article"},
    )
    token = failed.canonical["repair"]["token"]
    mismatch = _run_codemode(
        session, tmp_path, turn=2, call_id="repair", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/other"}, repair_of=token,
    )
    assert not mismatch.ok
    assert "repair" in mismatch.model_text.lower()
    assert source.calls == 1


def test_repair_after_durable_restore_reuses_the_original_child_receipt(tmp_path) -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.kernel_context import KernelContext
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall
    from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectSettlement
    from codey.runtime.effects.replay_policy import ReplayClass
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore
    from tests.test_codemode_replay_authority import fixture

    project, _state, _logdir, mutations, managed, revisions, sink, session = fixture(tmp_path)
    (project / "a.py").write_text("V1\n", encoding="utf-8")
    first_call = ToolCall(
        "codemode",
        {"code": "const result = await tools.read_file({path: inputs.path}); return result.value.missing;",
         "inputs": {"path": "a.py"}},
        call_id="first",
    )
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="r", effect_scope="outer", turn=0,
        project_path=project, managed_outputs=managed, workspace_revision_store=revisions,
        intent_sink=sink, durable=mutations.durable,
    )
    first = execute_turn(
        session, [first_call], executors={"codemode": parent}, run_id="r", effect_scope="outer", turn=0,
        snapshot=snapshot, intent_sink=sink, durable=mutations.durable, project_path=project,
        managed_outputs=managed, workspace_revision_store=revisions,
    )[0]
    assert not first.ok
    token = first.canonical["repair"]["token"]
    # The direct execute_turn fixture bypasses the provider driver. Complete
    # the existing parent result delivery through the same durable provider
    # effect path used in production before starting the next turn.
    delivery_batch = ToolResultDeliveryStore(durable=mutations.durable).load_batches("s", "r")[0]
    mutations.begin_provider_effect(
        "s", "r",
        RuntimeEffectIntent(
            "repair-test-provider-1", "provider_send", "s", "r",
            provider_id="local", turn=1, args_digest="a" * 64,
            replay_class=ReplayClass.UNSAFE,
            delivery_batch_id=delivery_batch.intent.batch_id,
        ),
        driver="writer", delivery_batch_id=delivery_batch.intent.batch_id,
    )
    mutations.settle_provider_effect(
        "s", "r",
        RuntimeEffectSettlement(
            "repair-test-provider-1", "provider_send", "s", "r", "ok",
            sent_state="settled", replay_class=ReplayClass.UNSAFE,
        ),
    )
    mutations.durable.close()

    durable = DurableSession(SQLiteDurableStore(_state / "durable.sqlite3"))
    reopened_mutations = RuntimeMutationLine(durable=durable)
    reopened_sink = KernelEffectSink(
        reopened_mutations, session_id="s", run_id="r", provider_id="local", durable=durable,
        managed_outputs=ManagedOutputStore(_state),
    )
    fresh = TaskSession(session.policy, task_kind=session.task_kind, project=str(project), max_turns=8)
    KernelContext(
        fresh,
        KernelRunRequest(
            transport=KernelTransportDeps(provider=object(), provider_id="local", run_id="r"),
            execution=KernelExecutionDeps(project_path=project, workspace_revision_store=revisions),
            observation=KernelObservationDeps(intent_sink=reopened_sink),
        ),
    ).restore_facts()
    (project / "a.py").write_text("V2\n", encoding="utf-8")
    repair_call = ToolCall(
        "codemode",
        {"code": "return unwrap(await tools.read_file({path: inputs.path}));",
         "inputs": {"path": "a.py"}, "repair_of": token},
        call_id="repair",
    )
    repair_snapshot = build_turn_snapshot(fresh)
    repair_parent = build_execute_turn_executor(
        session=fresh, snapshot=repair_snapshot, run_id="r", effect_scope="outer", turn=1,
        project_path=project, managed_outputs=managed, workspace_revision_store=revisions,
        intent_sink=reopened_sink, durable=durable,
    )
    try:
        repaired = execute_turn(
            fresh, [repair_call], executors={"codemode": repair_parent}, run_id="r", effect_scope="outer", turn=1,
            snapshot=repair_snapshot, intent_sink=reopened_sink, durable=durable, project_path=project,
            managed_outputs=managed, workspace_revision_store=revisions,
        )[0]
        assert repaired.ok, repaired.model_text
        assert repaired.canonical["value"]["text"] == "V1\n"
        assert repaired.audit["recovery_disposition"] == "receipt_replay"
    finally:
        durable.close()


def test_repair_rejects_when_the_replayed_tool_is_revoked(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: None)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))

    class Source:
        calls = 0

        def search(self, _query: str, *, limit: int):
            return []

        def fetch(self, url: str):
            self.calls += 1
            return {"status": "ok", "url": url, "title": "Article", "content_kind": "html", "text": "V1\n" * 80}

    from codey.research.tools import ResearchTools
    source = Source()
    research_tools = ResearchTools(source, None, None)
    failed = _run_codemode(
        session, tmp_path, turn=1, call_id="first", research_tools=research_tools,
        code="const result = await tools.open_url({url: inputs.url}); return result.value.missing;",
        inputs={"url": "https://example.test/article"},
    )
    token = failed.canonical["repair"]["token"]
    session.policy = TaskPolicy(frozenset({"control", "project.read"}))
    revoked = _run_codemode(
        session, tmp_path, turn=2, call_id="repair", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/article"}, repair_of=token,
    )
    assert not revoked.ok
    assert "authorized" in revoked.model_text.lower() or "capability" in revoked.model_text.lower()
    assert source.calls == 1


@pytest.mark.parametrize("repair_of", [None, "cmr_missing"])
def test_missing_or_absent_repair_authorization_never_reuses_a_child(
    tmp_path, repair_of: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *args, **kwargs: None)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))
    class Source:
        calls = 0

        def search(self, _query: str, *, limit: int):
            return []

        def fetch(self, url: str):
            self.calls += 1
            return {"status": "ok", "url": url, "title": "Article", "content_kind": "html",
                    "text": "fresh\n" * 80}

    from codey.research.tools import ResearchTools
    source = Source()
    research_tools = ResearchTools(source, None, None)

    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot

    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="repair-run", effect_scope="task", turn=1,
        project_path=tmp_path, research_tools=research_tools,
    )
    args: dict[str, object] = {"code": "return unwrap(await tools.open_url({url: inputs.url}));",
                                "inputs": {"url": "https://example.test/article"}}
    if repair_of is not None:
        args["repair_of"] = repair_of
    result = execute_turn(
        session, [ToolCall("codemode", args, call_id="repair")], run_id="repair-run",
        effect_scope="task", turn=1, project_path=tmp_path,
        executors={"codemode": parent}, snapshot=snapshot, research_tools=research_tools,
    )[0]
    if repair_of is None:
        assert result.ok
        assert source.calls == 1
    else:
        assert not result.ok
        assert source.calls == 0
