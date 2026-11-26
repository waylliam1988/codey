from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.operations.kernel.kernel_driver import drive_provider_request
from codey.operations.kernel_contracts import ToolExecutor
from codey.operations.provider_request import prepare_provider_request
from codey.operations.provider_session import adapt_provider
from codey.operations.task_session import TaskSession
from codey.providers.requests import SendTextRequest
from codey.research.search_contracts import SearchOutcome
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tests.support.search_fixture import search_outcome
from tests.test_codemode_replay_authority import _durable


def _allow_url(*args: object, **kwargs: object) -> None:
    return None


@dataclass
class _Receipts:
    _memory_results: dict[str, ToolResult]


class _AckProvider:
    def send(self, *args: object) -> str:
        return "ack"


def _run_codemode(
    session: TaskSession,
    project: Path,
    *,
    turn: int,
    call_id: str,
    code: str,
    inputs: JsonObject,
    executor: ToolExecutor | None = None,
    repair_of: str | None = None,
    research_tools: ResearchTools | None = None,
    session_id: str = "",
) -> ToolResult:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.turn_snapshot import build_turn_snapshot
    from codey.runtime.core.models import ToolCall

    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="repair-run", effect_scope="task",
        turn=turn, project_path=project, session_id=session_id,
        executors={"open_url": executor} if executor is not None else None, research_tools=research_tools,
    )
    args: JsonObject = {"code": code, "inputs": inputs}
    if repair_of is not None:
        args["repair_of"] = repair_of
    return execute_turn(
        session,
        [ToolCall("codemode", args, call_id=call_id)],
        run_id="repair-run", effect_scope="task", turn=turn, project_path=project,
        executors={"codemode": parent}, snapshot=snapshot, research_tools=research_tools,
    )[0]


def test_parse_codemode_preserves_repair_token() -> None:
    from codey.protocols.codemode import parse_codemode_args

    program = parse_codemode_args({"code": "return 1;", "repair_of": "cmr_abc123"})
    assert program.repair_of == "cmr_abc123"


def test_kernel_argument_normalization_preserves_repair_token() -> None:
    from codey.operations.kernel.kernel_protocol import _validate_tool_args

    normalized, error = _validate_tool_args("codemode", {
        "code": "return 1;",
        "repair_of": "cmr_abc123",
    })
    assert not error
    assert normalized["repair_of"] == "cmr_abc123"


@pytest.mark.parametrize("failed_child", [False, True])
def test_browser_file_replay_bounds_original_data_and_requires_settled_success(failed_child: bool) -> None:
    from codey.codemode.catalog import build_catalog_snapshot
    from codey.codemode.replay import browser_file_replay_value, repair_catalog_digest, repair_metadata
    from codey.codemode.types import ToolCatalogEntry
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.toolchain.result_schema import browser_output_schema

    tools = ["browser_download", "browser_read", "browser_read"]
    catalog = build_catalog_snapshot([
        ToolCatalogEntry(name, name, output_schema=browser_output_schema(name)) for name in sorted(set(tools))
    ])
    download = {"action": "download", "delivery": "delivered", "text": "file", "ref": "ref",
                "path": "file.txt", "suggested_filename": "file.txt", "handle": "out_file", "bytes": 4,
                "sha256": "a" * 64}
    children = {
        "download": ToolResult(ToolCall("browser_download", {}, "run:parent:child:0"), "file", ok=not failed_child,
                               canonical={"value": download}),
        "large": ToolResult(ToolCall("browser_read", {}, "run:parent:child:1"), "大" * 5000, ok=True),
        "partial": ToolResult(ToolCall("browser_read", {}, "run:parent:child:2"), "partial", ok=True, truncated=True),
    }
    metadata = repair_metadata("session", "run:parent", "result_encoding", list(children),
                               repair_catalog_digest(catalog, tools))
    parent = ToolResult(ToolCall("codemode", {}, "parent"), "failed", ok=False,
                        canonical={"repair": metadata, "child_effect_ids": list(children)})
    result = browser_file_replay_value(_Receipts(children), parent, str(metadata["token"]),
                                      session_id="session", run_id="run", catalog=catalog)
    if failed_child:
        assert result is None
        return
    assert result is not None
    rows = result["replayed"]
    assert isinstance(rows, list)
    file, large, partial = (decode_json_object(item) for item in rows)
    assert file["data"] == download and not file["truncated"]
    assert large["data"] is None and large["truncated"] and large["data_omitted"]
    assert partial["data"] == {"text": children["partial"].model_text}
    assert partial["truncated"] and not partial["data_omitted"]


def test_return_only_repair_reuses_the_original_child_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", _allow_url)

    session = TaskSession(
        TaskPolicy(frozenset({"control", "project.read", "web.read"})),
        project=str(tmp_path),
    )
    class Source:
        calls = 0

        def search(self, query: str, *, limit: int) -> SearchOutcome:
            return search_outcome(query, [], limit=limit)

        def fetch(self, url: str) -> dict[str, object]:
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
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "token")
    assert isinstance(token, str) and token.startswith("cmr_")
    assert source.calls == 1
    repaired = _run_codemode(
        session, tmp_path, turn=2, call_id="repair", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/article"}, repair_of=token,
    )
    assert repaired.ok, repaired.model_text
    assert "V1" in require_json_string(repaired.canonical["value"], "value")
    assert repaired.audit["recovery_disposition"] == "receipt_replay"
    assert source.calls == 1


def test_repair_token_is_bound_to_the_original_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.tools import ResearchTools

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", _allow_url)
    policy = TaskPolicy(frozenset({"control", "project.read", "web.read"}))

    class Source:
        calls = 0

        def search(self, query: str, *, limit: int) -> SearchOutcome:
            return search_outcome(query, [], limit=limit)

        def fetch(self, url: str) -> dict[str, object]:
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
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "token")

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


def test_fresh_codemode_execution_reads_the_new_source_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", _allow_url)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))
    class Source:
        calls = 0

        def search(self, query: str, *, limit: int) -> SearchOutcome:
            return search_outcome(query, [], limit=limit)

        def fetch(self, url: str) -> dict[str, object]:
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
    assert first.ok and "V1" in require_json_string(first.canonical["value"], "value")
    fresh = _run_codemode(
        session, tmp_path, turn=2, call_id="fresh", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/article"},
    )
    assert fresh.ok and "V2" in require_json_string(fresh.canonical["value"], "value")
    assert source.calls == 2


def test_repair_argument_change_fails_without_fetching_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", _allow_url)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))
    class Source:
        calls = 0

        def search(self, query: str, *, limit: int) -> SearchOutcome:
            return search_outcome(query, [], limit=limit)

        def fetch(self, url: str) -> dict[str, object]:
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
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "token")
    mismatch = _run_codemode(
        session, tmp_path, turn=2, call_id="repair", research_tools=research_tools,
        code="return unwrap(await tools.open_url({url: inputs.url}));",
        inputs={"url": "https://example.test/other"}, repair_of=token,
    )
    assert not mismatch.ok
    assert "repair" in mismatch.model_text.lower()
    assert source.calls == 1


def test_receipt_only_repair_rejects_state_writes_without_reapplying_them(tmp_path: Path) -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.turn_snapshot import build_turn_snapshot
    from codey.runtime.core.models import ToolCall
    from tests.test_codemode_replay_authority import fixture

    project, _state, _logdir, mutations, managed, revisions, sink, session = fixture(tmp_path)
    first_call = ToolCall(
        "codemode",
        {"code": "await store('counter', 1); const result = await tools.read_file({path:'a.py'}); return result.value.missing;"},
        call_id="first",
    )
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="r", effect_scope="outer", turn=0,
        project_path=project, managed_outputs=managed, workspace_revision_store=revisions,
        intent_sink=sink, durable=_durable(mutations),
    )
    try:
        failed = execute_turn(
            session, [first_call], executors={"codemode": parent}, run_id="r", effect_scope="outer", turn=0,
            snapshot=snapshot, intent_sink=sink, durable=_durable(mutations), project_path=project,
            managed_outputs=managed, workspace_revision_store=revisions,
        )[0]
        assert not failed.ok
        reference = prepare_provider_request(sink, SendTextRequest("results"), counters=None)
        drive_provider_request(sink, adapt_provider(_AckProvider()), reference)
        token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "token")
        repair_call = ToolCall(
            "codemode",
            {"repair_of": token, "code": "await store('counter', 2); return 1;"},
            call_id="repair",
        )
        repair_snapshot = build_turn_snapshot(session)
        repair_parent = build_execute_turn_executor(
            session=session, snapshot=repair_snapshot, run_id="r", effect_scope="outer", turn=1,
            project_path=project, managed_outputs=managed, workspace_revision_store=revisions,
            intent_sink=sink, durable=_durable(mutations),
        )
        repaired = execute_turn(
            session, [repair_call], executors={"codemode": repair_parent}, run_id="r", effect_scope="outer", turn=1,
            snapshot=repair_snapshot, intent_sink=sink, durable=_durable(mutations), project_path=project,
            managed_outputs=managed, workspace_revision_store=revisions,
        )[0]
        assert not repaired.ok
        assert repaired.audit["error_code"] == "repair_rejected"
        assert decode_json_object(_durable(mutations).task("r").state["codemode_state"])["counter"] == 1
    finally:
        _durable(mutations).close()


def test_parallel_repair_binds_receipts_to_original_child_indices(tmp_path: Path) -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.operations.turn_snapshot import build_turn_snapshot
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    session = TaskSession(
        TaskPolicy(frozenset({"control", "project.read"})), project=str(tmp_path),
    )

    def read(call: ToolCall) -> ToolResult:
        index = int(call.call_id.rsplit(":", 1)[-1])
        if index == 0:
            time.sleep(0.05)
        return ToolResult(call, "", ok=True, canonical={"value": {
            "path": "same", "start_line": 1, "end_line": 1, "total_lines": 1,
            "next_offset": 0, "text_complete": True, "text": f"ORIGINAL_CALL_{index}",
        }})

    def run(turn: int, call: ToolCall) -> ToolResult:
        snapshot = build_turn_snapshot(session)
        parent = build_execute_turn_executor(
            session=session, snapshot=snapshot, run_id="parallel", effect_scope="task", turn=turn,
            project_path=tmp_path, executors={"read_file": read},
        )
        return execute_turn(
            session, [call], executors={"codemode": parent}, run_id="parallel", effect_scope="task", turn=turn,
            project_path=tmp_path, snapshot=snapshot,
        )[0]

    failed = run(0, ToolCall(
        "codemode",
        {"code": "const rows = await Promise.all([tools.read_file({path:'same'}), tools.read_file({path:'same'})]); return rows[0].value.missing;"},
        call_id="first",
    ))
    assert not failed.ok
    print("PARALLEL", failed.model_text, failed.audit, failed.canonical)
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "token")
    repaired = run(1, ToolCall(
        "codemode",
        {"repair_of": token, "code": "const rows = await Promise.all([tools.read_file({path:'same'}), tools.read_file({path:'same'})]); return rows.map(row => row.value.text);"},
        call_id="repair",
    ))
    assert repaired.ok, repaired.model_text
    assert repaired.canonical["value"] == ["ORIGINAL_CALL_0", "ORIGINAL_CALL_1"]


def test_repair_after_durable_restore_reuses_the_original_child_receipt(tmp_path: Path) -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.kernel.kernel_context import KernelContext
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.operations.turn_snapshot import build_turn_snapshot
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
        intent_sink=sink, durable=_durable(mutations),
    )
    first = execute_turn(
        session, [first_call], executors={"codemode": parent}, run_id="r", effect_scope="outer", turn=0,
        snapshot=snapshot, intent_sink=sink, durable=_durable(mutations), project_path=project,
        managed_outputs=managed, workspace_revision_store=revisions,
    )[0]
    assert not first.ok
    token = require_json_string(decode_json_object(first.canonical["repair"])["token"], "token")
    # The direct execute_turn fixture bypasses the provider driver. Complete
    # the existing parent result delivery through the same durable provider
    # effect path used in production before starting the next turn.
    delivery_batch = ToolResultDeliveryStore(durable=_durable(mutations)).load_batches("s", "r")[0]
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
    _durable(mutations).close()

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
            transport=KernelTransportDeps(provider=adapt_provider(_AckProvider()), provider_id="local", run_id="r"),
            execution=KernelExecutionDeps(intent_sink=reopened_sink, project_path=project, workspace_revision_store=revisions),
            observation=KernelObservationDeps(),
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
        assert decode_json_object(repaired.canonical["value"])["text"] == "V1\n"
        assert repaired.audit["recovery_disposition"] == "receipt_replay"
    finally:
        durable.close()


def test_repair_rejects_when_the_replayed_tool_is_revoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", _allow_url)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))

    class Source:
        calls = 0

        def search(self, query: str, *, limit: int) -> SearchOutcome:
            return search_outcome(query, [], limit=limit)

        def fetch(self, url: str) -> dict[str, object]:
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
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "token")
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
    tmp_path: Path, repair_of: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", _allow_url)

    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))
    class Source:
        calls = 0

        def search(self, query: str, *, limit: int) -> SearchOutcome:
            return search_outcome(query, [], limit=limit)

        def fetch(self, url: str) -> dict[str, object]:
            self.calls += 1
            return {"status": "ok", "url": url, "title": "Article", "content_kind": "html",
                    "text": "fresh\n" * 80}

    from codey.research.tools import ResearchTools
    source = Source()
    research_tools = ResearchTools(source, None, None)

    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.operations.turn_snapshot import build_turn_snapshot

    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(
        session=session, snapshot=snapshot, run_id="repair-run", effect_scope="task", turn=1,
        project_path=tmp_path, research_tools=research_tools,
    )
    args: JsonObject = {"code": "return unwrap(await tools.open_url({url: inputs.url}));",
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
