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
):
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.runtime.core.models import ToolCall

    snapshot = build_turn_snapshot(session)
    builder_args = {
        "session": session, "snapshot": snapshot, "run_id": "repair-run", "effect_scope": "task",
        "turn": turn, "project_path": project,
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
    assert repaired.canonical["value"] == "V1"
    assert repaired.audit["recovery_disposition"] == "receipt_replay"
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
