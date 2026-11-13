"""Production CodeMode argument compatibility precedes execution and repair."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, require_json_string


class Source:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return []

    def fetch(self, url: str) -> dict[str, object]:
        self.calls.append(url)
        return {"status": "ok", "url": url, "title": "Article", "content_kind": "html",
                "text": "Original source observation\n" * 80}


def run_program(session: TaskSession, project: Path, tools: ResearchTools, code: str,
                *, turn: int = 1, repair_of: str | None = None) -> ToolResult:
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="alias-repair",
        effect_scope="task", turn=turn, project_path=project, research_tools=tools)
    args: JsonObject = {"code": code}
    if repair_of is not None:
        args["repair_of"] = repair_of
    return execute_turn(session, [ToolCall("codemode", args, f"parent-{turn}")],
        run_id="alias-repair", effect_scope="task", turn=turn, project_path=project,
        executors={"codemode": parent}, snapshot=snapshot, research_tools=tools)[0]


def source_session(project: Path) -> TaskSession:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(project))
    session.search_results["r2"] = "https://example.com/article"
    return session


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("first,second", [("id", "result_id"), ("result_id", "id")])
def test_alias_and_canonical_repair_reuse_one_real_source_receipt(tmp_path: Path, first: str, second: str) -> None:
    session = source_session(tmp_path)
    source = Source()
    tools = ResearchTools(source, None, None)
    failed = run_program(session, tmp_path, tools,
        f"const r = await tools.open_result({{{first}:'r2'}}); return unwrap(r).missing;")
    assert not failed.ok and failed.audit["error_code"] in {"missing_return", "result_encoding"}
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "repair token")
    children = [result for result in session._memory_results.values() if result.call.name == "open_result"]
    assert len(children) == 1 and children[0].call.args == {"result_id": "r2"}
    repaired = run_program(session, tmp_path, tools,
        f"return unwrap(await tools.open_result({{{second}:'r2'}}));", turn=2, repair_of=token)
    assert repaired.ok, repaired.model_text
    assert repaired.audit["recovery_disposition"] == "receipt_replay"
    assert "Original source observation" in str(repaired.canonical["value"])
    assert source.calls == ["https://example.com/article"]


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("args", ["{}", "{id:null}", "{id:''}", "{id:'r0'}", "{id:'R2'}",
    "{id:'https://example.com/article'}", "{id:'r2',result_id:'r2'}", "{id:'r2',extra:true}", "{id:'r99'}"])
def test_invalid_alias_arguments_cannot_open_a_source(tmp_path: Path, args: str) -> None:
    session = source_session(tmp_path)
    source = Source()
    result = run_program(session, tmp_path, ResearchTools(source, None, None),
        f"return unwrap(await tools.open_result({args}));")
    assert not result.ok
    assert not source.calls
    if args == "{id:'r99'}":
        assert "unknown open_result id" in result.model_text
