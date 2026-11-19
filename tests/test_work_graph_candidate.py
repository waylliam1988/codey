"""Graph boundaries plus real production kernel integration, without a model server."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV, LOCAL_OPENAI_MODEL_ENV, NATIVE_TOOLS_ENV
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest
from codey.operations.task_session import TaskSession
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.research.ledger import ResearchLedger
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain import tool_spec
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object
from codey.workspace.revision import scan_workspace_fingerprint
from tools.agent_work_graph_probe import RETRY_NEW, append, controlled, records
from tools.agent_work_graph_query_probe import cases, kernel_binding, regression_checks
from tools.work_graph_candidate import (
    MAX_RESPONSE_CHARS,
    TOOL_NAME,
    WorkContextService,
    WorkInputs,
    build_snapshot,
    constraint_spans,
    installed_tool,
    production_inputs,
    query_snapshot,
)


def fixture(directory: Path, name: str = "relay") -> Path:
    project = directory / "project"
    for key, body in cases()[name].files.items():
        path = project / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return project


def test_exact_constraint_spans_preserve_decimals_and_intervals() -> None:
    text = "Round only to 0.01.\nClamp valid integer seconds to 2..17 inclusive.\nAn unrelated sentence."
    quotes = [text[start:end] for start, end in constraint_spans(text)]
    assert quotes == ["Round only to 0.01.", "Clamp valid integer seconds to 2..17 inclusive."]


def test_unopened_sources_and_hidden_grader_never_enter_graph(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    snapshot = build_snapshot(project, WorkInputs())
    serialized = json.dumps({key: node.text for key, node in snapshot.nodes.items()})
    assert "2..17 inclusive" not in serialized
    assert cases()["relay"].grader not in serialized
    assert snapshot.nodes[RETRY_NEW].kind == "source_locator"


def test_committed_source_forms_constraint_to_code_to_test_candidates(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    ledger = ResearchLedger()
    body = "Retry only GET or HEAD requests. Clamp valid integer seconds to 2..17 inclusive."
    ledger.record_open(RETRY_NEW, RETRY_NEW, "Relay v2", body)
    snapshot = build_snapshot(project, WorkInputs(ledger))
    requirements = [node for node in snapshot.nodes.values() if node.kind == "constraint_candidate" and node.path == RETRY_NEW]
    assert len(requirements) == 2
    for node in requirements:
        assert body[node.start:node.end] == node.text
        assert any(edge.source == node.id and edge.relation == "applies_to_candidate" and edge.target == "relay/rules.py"
                   for edge in snapshot.edges)
        assert any(edge.source == node.id and edge.relation == "related_test_by_import" for edge in snapshot.edges)
    payload = query_snapshot(snapshot, query="retry", path="relay/rules.py", view="gaps")
    assert payload["gaps"]
    assert "behavioral_proof_not_established" in json.dumps(payload)
    assert "verified_constraint" not in json.dumps(payload)


def test_fake_workspace_audit_cannot_establish_current_pass(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    fp = scan_workspace_fingerprint(project)
    result = ToolResult(call=ToolCall("run", {"command": "python -m pytest -q"}), ok=True,
        model_text="1 passed", audit={"exit_code": 0, "workspace_fingerprint": fp, "workspace_revision": 1})
    snapshot = build_snapshot(project, WorkInputs(results=(("fake", result),), fingerprint=fp))
    node = snapshot.nodes["execution:fake"]
    assert node.version == ""
    assert "matches_current_workspace=False" in node.text


def test_model_claim_is_not_a_verification_observation(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    result = ToolResult(call=ToolCall("done", {"summary": "all tests passed"}), ok=True, model_text="all tests passed")
    snapshot = build_snapshot(project, WorkInputs(results=(("claim", result),)))
    assert not any(node.kind == "execution_observation" for node in snapshot.nodes.values())


def test_graph_respects_project_ignore_and_rejects_stale_snapshot(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    config = project / ".codey/config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"schema_version": 1, "scan": {"ignored_paths": ["secret"]}}), encoding="utf-8")
    (project / "secret").mkdir()
    (project / "secret/token.md").write_text("must never leak SECRET_SENTINEL", encoding="utf-8")
    snapshot = build_snapshot(project, WorkInputs())
    assert "secret/token.md" not in snapshot.nodes
    before = scan_workspace_fingerprint(project)
    (project / "relay/rules.py").write_text("# edited\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed while indexing"):
        build_snapshot(project, WorkInputs(fingerprint=before))


@pytest.mark.parametrize("args", [{"query": ""}, {"query": "a" * 501}, {"query": "retry", "path": "../secret"},
    {"query": "retry", "path": "D:/secret"}, {"query": "retry", "limit": True},
    {"query": "retry", "limit": 100}, {"query": "retry", "view": "execute"}])
def test_query_rejects_invalid_and_escape_inputs(tmp_path: Path, args: JsonObject) -> None:
    project = fixture(tmp_path)
    service = WorkContextService(project, WorkInputs)
    result = service.execute(ToolCall(TOOL_NAME, args))
    assert not result.ok
    assert result.model_text.startswith("ERROR:")


def test_same_corpus_flat_vs_graph_and_response_limit(tmp_path: Path) -> None:
    project = fixture(tmp_path, "checkout")
    snapshot = build_snapshot(project, WorkInputs())
    graph = query_snapshot(snapshot, query="round checkout", path="shop/amounts.py", view="gaps", graph=True)
    flat = query_snapshot(snapshot, query="round checkout", path="shop/amounts.py", view="gaps", graph=False)
    assert graph["relations"] and graph["gaps"]
    assert flat["relations"] == [] and flat["gaps"] == []
    assert len(json.dumps(graph, ensure_ascii=False)) <= MAX_RESPONSE_CHARS
    assert len(json.dumps(flat, ensure_ascii=False)) <= MAX_RESPONSE_CHARS
    assert all(decode_json_object(node)["id"] in snapshot.nodes for node in decode_nodes(graph))


def decode_nodes(payload: JsonObject) -> list[JsonObject]:
    nodes = payload["nodes"]
    assert isinstance(nodes, list)
    return [decode_json_object(node) for node in nodes]


def test_registration_uses_read_grant_and_restores_registry_on_exception(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    baseline = dict(tool_spec.tool_specs())
    with pytest.raises(RuntimeError, match="sentinel"), installed_tool(WorkContextService(project, WorkInputs)):
        spec = tool_spec.spec_for_tool(TOOL_NAME)
        assert spec is not None and spec.grant == "project.read"
        assert tool_spec.custom_executor_for(TOOL_NAME) is not None
        raise RuntimeError("sentinel")
    assert tool_spec.tool_specs() == baseline
    assert tool_spec.custom_executor_for(TOOL_NAME) is None


class ReplayProvider(ApiProvider):
    """Only the transport is replayed; tool execution and receipts are real."""

    def __init__(self, native: bool, actions: list[tuple[str, JsonObject]]) -> None:
        super().__init__("http://127.0.0.1:1/v1", "offline-graph-integration", native_tools=native,
                         context_window_tokens=262144, context_reserve_tokens=32768, context_keep_recent_tokens=32000)
        self.actions = list(actions)
        self.native = native
        self.calls = 0

    def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                  tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                  tool_call_contract: str = "", tool_call_observations: str = "", tool_call_history_groups: int = 0,
                  output_token_limit: int = 0, require_tool_call: bool = False) -> dict[str, object]:
        self.calls += 1
        if not self.actions:
            raise AssertionError("Unexpected extra model request in integration test")
        name, args = self.actions.pop(0)
        if self.native:
            message = {"role": "assistant", "content": "", "tool_calls": [{"id": f"offline-{self.calls}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
        else:
            message = {"role": "assistant", "content": json.dumps({"tool": name, "args": args})}
        return {"choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if self.native else "stop"}]}


@pytest.mark.parametrize("native", [True, False])
def test_real_headless_native_and_json_source_refresh_and_durable_query_receipts(tmp_path: Path, native: bool) -> None:
    project = fixture(tmp_path)
    actions: list[tuple[str, JsonObject]] = [
        (TOOL_NAME, {"query": "retry", "path": "relay/rules.py", "view": "gaps"}),
        ("open_url", {"url": RETRY_NEW}),
        (TOOL_NAME, {"query": "retry", "path": "relay/rules.py", "view": "gaps"}),
        ("done", {"summary": "Read the v2 source and inspected candidate constraints: " + RETRY_NEW}),
    ]
    provider = ReplayProvider(native, actions)

    def connect(provider_id: str, *, port: int) -> ApiProvider:
        return provider

    seen: list[JsonObject] = []

    def inspect(session: TaskSession, request: KernelRunRequest) -> None:
        assert not session.read_files, "Graph snippets must not grant an edit read receipt"
        seen.extend(canonical_json_object(result.canonical) for result in session._memory_results.values()
                    if result.call.name == TOOL_NAME)

    with (patch.dict("os.environ", {NATIVE_TOOLS_ENV: "1" if native else "0",
          LOCAL_OPENAI_BASE_URL_ENV: "http://127.0.0.1:1/v1", LOCAL_OPENAI_MODEL_ENV: "offline-graph-integration"}),
          controlled(cases()["relay"], tmp_path),
          kernel_binding(tmp_path, "graph", inspect)):
        result = run_headless(HeadlessRequest(project, "Inspect Relay requirements. Do not modify any project files.",
            provider_id="local", intent="chat", max_turns=8, state_home=tmp_path / "state",
            project_changes_required=False, sources_open_required=False,
            requested_capabilities=("web.read",), model_selection={"model": "offline-graph-integration", "base_url": "http://127.0.0.1:1/v1"}),
            emit_jsonl=lambda row: append(tmp_path / "events.jsonl", row), connect_provider=connect)
    assert result.stop_reason == "done", records(tmp_path / "events.jsonl")[-5:]
    assert len(seen) == 2
    assert "2..17 inclusive" not in json.dumps(seen[0])
    assert "2..17 inclusive" in json.dumps(seen[1])
    receipts: object = json.loads((tmp_path / "query-receipts.json").read_text(encoding="utf-8"))
    assert isinstance(receipts, list) and len(receipts) == 2
    assert [decode_json_object(row)["canonical"] for row in receipts] == seen
    assert all(decode_json_object(row)["call_id"] for row in receipts) if native else True


def test_permission_denial_applies_to_resource_adapter(tmp_path: Path) -> None:
    from codey.policies.task_policy import build_task_policy
    from codey.task.model import TaskSubmission

    project = fixture(tmp_path)
    policy = build_task_policy(TaskSubmission(session_id="s", project=str(project), task="inspect",
        max_turns=2, continue_task=False, provider_id="local", denied_capabilities=("project.read",)), task_kind="chat")
    session = TaskSession(policy=policy, task_kind="chat", project=str(project), max_turns=2)
    with pytest.raises(PermissionError):
        production_inputs(session, KernelExecutionDeps(project_path=project))


def test_missing_or_useless_regression_does_not_pass_mutation_gate(tmp_path: Path) -> None:
    project = fixture(tmp_path, "checkout")
    case = cases()["checkout"]
    assert regression_checks(tmp_path, case)["present"] is False
    (project / "tests/test_regression.py").write_text("def test_useless():\n    assert True\n", encoding="utf-8")
    result = regression_checks(tmp_path, case)
    assert result["passes_final"] is True
    assert result["detects_original_bug"] is False


def test_production_codec_fixture_is_single_module_with_explicit_mutation() -> None:
    case = cases()["codec"]
    assert "codec/json_value.py" in case.files
    assert "if not isinstance(value, int):" in case.files["codec/json_value.py"]
    assert len(case.files) == 7


def test_real_run_failure_counterexample_and_edit_invalidate_old_observation(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    (project / "tests/test_regression.py").write_text(
        "from relay.client import plan_retry\n\ndef test_ascii():\n    assert plan_retry(503, 'GET', '\u0663') == 4\n", encoding="utf-8")
    old = (project / "relay/rules.py").read_text(encoding="utf-8")
    new = ("def retry_delay(status, method, retry_after):\n"
           "    if status not in (429, 503) or method not in ('GET', 'HEAD'):\n        return None\n"
           "    if not isinstance(retry_after, str):\n        return 4\n"
           "    text = retry_after.strip()\n"
           "    if not text or not text.isascii() or not text.isdecimal():\n        return 4\n"
           "    return min(max(int(text), 2), 17)\n")
    query: JsonObject = {"query": "retry verification", "view": "gaps", "path": "relay/rules.py"}
    actions: list[tuple[str, JsonObject]] = [
        ("read_file", {"path": "relay/rules.py"}),
        ("run", {"command": "python -m pytest -q tests/test_regression.py"}),
        (TOOL_NAME, query),
        ("edit", {"path": "relay/rules.py", "replacements": [{"old_string": old, "new_string": new}]}),
        (TOOL_NAME, query),
        ("run", {"command": "python -m pytest -q"}),
        (TOOL_NAME, query),
        ("done", {"summary": "Fixed strict retry input validation and verified."}),
    ]
    provider = ReplayProvider(True, actions)

    def connect(provider_id: str, *, port: int) -> ApiProvider:
        return provider

    with (patch.dict("os.environ", {NATIVE_TOOLS_ENV: "1", LOCAL_OPENAI_BASE_URL_ENV: "http://127.0.0.1:1/v1",
          LOCAL_OPENAI_MODEL_ENV: "offline-graph-integration"}), controlled(cases()["relay"], tmp_path),
          kernel_binding(tmp_path, "graph")):
        result = run_headless(HeadlessRequest(project, "Fix retry input validation and add regression verification.",
            provider_id="local", intent="chat", max_turns=12, state_home=tmp_path / "state",
            project_changes_required=True, sources_open_required=False, requested_capabilities=("web.read",),
            model_selection={"model": "offline-graph-integration", "base_url": "http://127.0.0.1:1/v1"}),
            emit_jsonl=lambda row: append(tmp_path / "events.jsonl", row), connect_provider=connect)
    assert result.stop_reason == "done", records(tmp_path / "events.jsonl")[-5:]
    queries = records(tmp_path / "queries.jsonl")
    assert len(queries) == 3
    assert "matches_current_workspace=True" in json.dumps(queries[0])
    assert "matches_current_workspace=False" in json.dumps(queries[1])
    assert "matches_current_workspace=True" in json.dumps(queries[2])
    final = decode_json_object(json.loads((tmp_path / "final-graph.json").read_text(encoding="utf-8")))
    assert '"relation": "observed_failure"' in json.dumps(final)
    checks: object = json.loads((tmp_path / "production-checks.json").read_text(encoding="utf-8"))
    assert isinstance(checks, list) and len(checks) == 2
    assert decode_json_object(checks[0])["current"] is False
    assert decode_json_object(checks[1])["current"] is True
    assert "failed" in str(decode_json_object(checks[0])["output"])
    assert "passed" in str(decode_json_object(checks[1])["output"])


def test_scan_budget_reports_omission_instead_of_complete_index(tmp_path: Path) -> None:
    project = fixture(tmp_path)
    with patch("tools.work_graph_candidate.MAX_INDEX_FILES", 2):
        snapshot = build_snapshot(project, WorkInputs())
    assert snapshot.warnings
    assert "omitted files may contain more matches" in " ".join(snapshot.warnings)
