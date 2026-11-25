from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.progress_comparison import project_failure_frames, pytest_failure_ids
from codey.operations.project.edit_base import (
    compact_symbol_retry,
    current_definition,
    is_compact_symbol_retry,
    unchanged_definition,
)
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, TurnFinish
from codey.research.answer_context import _render_context, answer_context
from codey.research.answer_contract import AnswerScope, answer_scope, submit_answer, validate_submission
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.research.passages import ReadPassage, describe_window, passage_ref
from codey.research.primary_sources import (
    PRIMARY_ACQUISITION_ID,
    PRIMARY_QUESTION_ID,
    primary_acquisition,
)
from codey.research.report_evidence import CLAIM_CHECK_VERSION, check_report_evidence
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import explicit_web_intent
from codey.types.json_value import JsonObject, canonical_json_object, require_json_integer, require_json_string
from codey.workspace.revision import WorkspaceRevisionStore


@pytest.mark.parametrize("case", ["write", "stale", "ambiguous"])
def test_small_symbol_edit_uses_frozen_read_and_preserves_surroundings(tmp_path: Path, case: str) -> None:
    original = b"prefix = 'untouched'\r\n\r\ndef f():\r\n    return 0\r\n\r\nsuffix = 'kept'\r\n"
    if case == "ambiguous":
        original += b"def f(): return -1\r\n"
    path = tmp_path / "a.py"
    path.write_bytes(original)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})),
                          task_kind="project", project=str(tmp_path))
    assert execute_turn(session, [ToolCall("read_file", {"path": "a.py"})], run_id="read", turn=1,
                        project_path=tmp_path)[0].ok
    snapshot = build_turn_snapshot(session)
    assert not is_compact_symbol_retry(snapshot)
    partial = AssistantTurn(text='{"tool":"edit","args":{"replacements":[', finish=TurnFinish.OUTPUT_LIMIT)
    retry = compact_symbol_retry(snapshot, partial)
    retry = restore_snapshot(snapshot_description(retry))
    assert is_compact_symbol_retry(retry)
    assert "replacements" not in dict(next(spec for spec in retry.model_specs if spec.name == "edit").parameters)
    plan = normalize_turn(decode_text_turn(json.dumps(
        {"tool": "edit", "args": {"path": "a.py", "symbol": "f", "content": "def f():\n    return 1"}})), snapshot=retry)
    assert not plan.protocol_error and plan.calls[0].args["base_result_ref"] == dict(snapshot.project.symbol_reads)["a.py"]
    if case == "stale":
        original += b"# external change\r\n"
        path.write_bytes(original)
    result = execute_turn(session, plan.calls, run_id="edit", turn=2, snapshot=retry, project_path=tmp_path,
        workspace_revision_store=WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state")))[0]
    assert result.ok == (case == "write"), result.model_text
    assert path.read_bytes() == (original.replace(b"return 0", b"return 1") if case == "write" else original)


def test_project_traceback_observation_is_bounded_to_workspace(tmp_path: Path) -> None:
    output = (f'  File "{tmp_path / "test_app.py"}", line 14, in test_display\n'
              f'    self.assertEqual(display_name("raw"), "expected")\n  File "{tmp_path.parent / "outside.py"}", '
              'line 9, in unexpected\n    unrelated()')
    assert project_failure_frames(output, tmp_path) == ({
        "path": "test_app.py", "line": 14, "function": "test_display",
        "source": 'self.assertEqual(display_name("raw"), "expected")'},)


def test_noop_method_suggests_symbol_on_the_observed_version_only(tmp_path: Path) -> None:
    old = "    def f(self):\r\n        return 0"
    project_file = tmp_path / "a.py"
    project_file.write_bytes(("class C:\r\n" + old + "\r\n").encode("utf-8"))
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})),
                          task_kind="project", project=str(tmp_path))
    edit = ToolResult(ToolCall("edit", {"path": "a.py", "replacements": [{"old_string": old, "new_string": old}]}),
                      "no changes", ok=True, audit={"changed": False})
    target = unchanged_definition(edit)
    assert target == ("a.py", "f", old)
    assert current_definition(session, tmp_path, *target) is None
    assert execute_turn(session, [ToolCall("read_file", {"path": "a.py"})], run_id="read", turn=1,
                        project_path=tmp_path)[0].ok
    assert current_definition(session, tmp_path, *target) == "f"
    project_file.write_bytes(project_file.read_bytes() + b"# external edit\r\n")
    assert current_definition(session, tmp_path, *target) is None


def test_pytest_failure_frames_bind_crlf_blocks_to_parameterized_nodes(tmp_path: Path) -> None:
    output = "\r\n".join((
        "================== FAILURES ==================",
        "________________ test_retry[True] ________________",
        "tests/test_order.py:52: in test_retry",
        "../app/order.py:48: in place_order",
        ">       db.execute(statement)",
        "E       sqlite3.IntegrityError: UNIQUE constraint failed",
        "../app/order.py:49: IntegrityError",
        "../../library/order.py:92: InternalError",
        "________________ test_other ________________",
        ">       assert a == b",
        "E       assert 1 == 2",
        "alt/test_order.py:30: AssertionError",
        "================== short test summary info ==================",
        "FAILED tests/test_order.py::test_retry[True] - IntegrityError",
        "FAILED alt/test_order.py::test_other - AssertionError",
        "================== 2 failed in 0.51s ==================",
    ))
    failures = pytest_failure_ids(output, 1)
    assert failures is not None
    frames = project_failure_frames(output, tmp_path, cwd="checks", pytest_failures=failures)
    assert [(row["path"], row["line"]) for row in frames] == [
        ("checks/alt/test_order.py", 30), ("app/order.py", 49), ("checks/tests/test_order.py", 52),
    ]
    assert frames[1]["failure"] == "FAILED tests/test_order.py::test_retry[True]"
    assert frames[1]["assertions"] == ["sqlite3.IntegrityError: UNIQUE constraint failed"]
    assert frames[1]["source"] == "db.execute(statement)"
    assert pytest_failure_ids(output.rsplit("\r\n", 1)[0], 1) is None


def test_symbol_edit_reuses_a_previous_round_inside_codemode_and_preserves_literal_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("class Container:\n    def f(self):\n        return 'old'\n\nlast = 'kept'\n", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})),
                          task_kind="project", project=str(tmp_path))
    assert execute_turn(session, [ToolCall("read_file", {"path": "a.py"})], run_id="read", turn=1,
                        project_path=tmp_path)[0].ok
    snapshot = build_turn_snapshot(session)
    revisions = WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state"))
    executor = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="code", effect_scope="task",
        turn=2, project_path=tmp_path, allow_writes=True, workspace_revision_store=revisions)
    replacement = '    def f(self):\n        return """first\nleft edge\n    four spaces\n"""'
    result = execute_turn(session, [ToolCall("codemode", {"code":
        "return unwrap(await tools.edit({path:'a.py',symbol:'f',content:inputs.source}));",
        "inputs": {"source": replacement}})], run_id="code", effect_scope="task", turn=2, project_path=tmp_path,
        executors={"codemode": executor}, snapshot=snapshot, workspace_revision_store=revisions)[0]
    assert result.ok, result.model_text
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = tree.body[0]
    assert isinstance(owner, ast.ClassDef) and isinstance(method := owner.body[0], ast.FunctionDef)
    returned = method.body[0]
    assert isinstance(returned, ast.Return) and returned.value is not None
    assert ast.literal_eval(returned.value) == "first\nleft edge\n    four spaces\n"
    assert path.read_text(encoding="utf-8").endswith("last = 'kept'\n")
    assert sum(receipt.call.name == "read_file" for receipt in session._memory_results.values()) == 1


def test_first_party_required_uses_committed_passages_and_bound_retry() -> None:
    session = TaskSession(TaskPolicy(frozenset({"web.read", "control"}), strict_research=True),
                          task_kind="research", task_text="Check current primary sources for storage behavior.")
    ledger = ResearchLedger()
    secondary = "https://dev.to/example/storage"
    primary = "https://sqlite.org/wal.html"

    def open_source(url: str, key: str) -> None:
        body = "The source discusses constraints on data storage."
        ledger.record_open(url, url, "Storage documentation", body)
        source = next(row for row in ledger.opened_sources if row.final_url == url)
        canonical = canonical_json_object({"read_passages": describe_window(url, source.text_hash, body, 0, len(body)),
                                          "research_observation": ledger_observation(ledger, "open", url=url)})
        session._memory_results[key] = ToolResult(ToolCall("open_url", {"url": url}), body, ok=True, canonical=canonical)

    open_source(secondary, "secondary")
    ledger.record_search("storage behavior", [{"url": primary, "title": "Project documentation"}])
    session.search_results.update(r1=primary)
    scope = AnswerScope.from_payload(answer_scope(session).to_payload())
    assert scope.primary_passages == ()
    answered: JsonObject = {"state": "answered", "markdown": "Storage behavior has important conditions. [e1]"}
    with pytest.raises(ValueError, match="primary_source_unverified"):
        submit_answer(scope, answered)
    partial = submit_answer(scope, {**answered, "state": "partial"})
    assert partial["state"] == "partial" and partial["markdown"].endswith("**Evidence status: partial.**")
    assert validate_submission(partial, session.task_text) == partial
    reviewed = check_report_evidence(partial, ledger.read_passages, claim_check_version=CLAIM_CHECK_VERSION)
    checks = reviewed["claim_checks"]
    assert isinstance(checks, list) and all(
        isinstance(row, dict) and require_json_integer(row["report_end"], "span") <=
        len(require_json_string(answered["markdown"], "authored markdown")) for row in checks)
    with pytest.raises(ValueError, match="report_tail_incomplete"):
        submit_answer(scope, {**answered, "state": "partial", "markdown": "Storage has limits [e1].\n1. **A new condition"})
    call = primary_acquisition(session, ledger, missing_primary=True, may_search=True, may_open=True)
    assert call is not None
    assert call == ToolCall("open_result", {"result_id": "r1"}, call_id=PRIMARY_ACQUISITION_ID)
    session._memory_results["retried"] = ToolResult(call, "failed acquisition", ok=False)
    assert primary_acquisition(session, ledger, missing_primary=True, may_search=True, may_open=True) is None
    open_source(primary, "primary")
    qualified = answer_scope(session)
    with pytest.raises(ValueError, match="primary_source_unverified"):
        submit_answer(qualified, answered)
    assert submit_answer(qualified, {**answered, "markdown": "The publisher documents storage limits. [e2]"})["state"] == "answered"


def test_first_party_followup_can_cover_another_question_after_source_recovery() -> None:
    session = TaskSession(TaskPolicy(frozenset({"web.read", "control"}), strict_research=True),
                          task_kind="research", task_text="Would you enable WAL? "
                          "How would you share and back up the data reliably? Check current primary sources.")
    ledger = ResearchLedger()
    ledger.record_open("https://sqlite.org/wal.html", "https://sqlite.org/wal.html", "SQLite WAL", "The WAL guidance.")
    ledger.record_search("SQLite WAL and network share", [])
    prior = ToolCall("open_result", {"result_id": "r1"}, call_id=PRIMARY_ACQUISITION_ID)
    session._memory_results["source"] = ToolResult(prior, "Opened", ok=True)
    call = primary_acquisition(session, ledger, missing_primary=False, may_search=True, may_open=True)
    assert call is not None
    assert call.call_id == PRIMARY_QUESTION_ID and call.name == "web_search"
    assert call.args["query"] == "site:sqlite.org How would you share and back up the data reliably?"
    session._memory_results["question"] = ToolResult(call, "Searched", ok=True)
    assert primary_acquisition(session, ledger, missing_primary=False, may_search=True, may_open=True) is None


def test_primary_followup_can_select_first_question_and_reuse_existing_evidence() -> None:
    session = TaskSession(TaskPolicy(frozenset({"web.read", "control"}), strict_research=True),
                          task_kind="research", task_text="Would you enable WAL? Can we use SMB? Check primary sources.")
    ledger = ResearchLedger()
    for url in ("https://sqlite.org/wal.html", "https://example.gov/storage"):
        ledger.record_open(url, url, "Publisher", "Storage")
    ledger.record_search("SMB protocol", [])
    first = primary_acquisition(session, ledger, missing_primary=False, may_search=True, may_open=True)
    assert first is not None and first.args["query"] == "Would you enable WAL?"
    text = "Enable WAL on a single host. SMB share constraints vary."
    source = ledger.opened_sources[0]
    ledger.read_passages["read"] = ReadPassage("read", source, text, (), 0, len(text))
    assert primary_acquisition(session, ledger, missing_primary=False, may_search=True, may_open=True) is None


def test_explicit_use_and_cite_captures_each_user_source() -> None:
    urls = frozenset({"https://sqlite.org/wal.html", "https://sqlite.org/useovernet.html", "https://sqlite.org/backup.html"})
    task = ("Use and cite the official SQLite WAL, network deployment and online backup documentation at "
            "https://sqlite.org/wal.html, https://sqlite.org/useovernet.html and https://sqlite.org/backup.html.")
    assert explicit_web_intent(task).urls == urls
    session = TaskSession(TaskPolicy(frozenset({"web.read", "control"}), strict_research=True),
                          task_kind="research", task_text=task)
    assert answer_scope(session).primary_passages == ()


def test_tight_first_party_budget_keeps_complete_passages_from_multiple_sources() -> None:
    session = TaskSession(TaskPolicy(frozenset({"web.read", "control"}), strict_research=True),
                          task_kind="research", task_text="Use official SQLite documentation. "
                          "Can two computers use network WAL? How can we make an online backup?")
    ledger = ResearchLedger()
    contents = {
        "https://sqlite.org/wal.html": "SQLite WAL requires the same host. It cannot use a network filesystem.\n",
        "https://sqlite.org/backup.html": "Use the online backup API to take a consistent snapshot.\n",
    }
    preferred: set[str] = set()
    for url, excerpt in contents.items():
        body = excerpt + "Unrelated detailed example. " * 170
        ledger.record_open(url, url, "Documentation", body)
        source = next(row for row in ledger.opened_sources if row.final_url == url)
        relevant, appendix = (ReadPassage(passage_ref(url, source.text_hash, start, end), source, body, (), start, end)
                              for start, end in ((0, len(excerpt)), (len(excerpt), len(body))))
        preferred.add(relevant.ref)
        ledger.read_passages.update({p.ref: p for p in (relevant, appendix)})
        session._memory_results[url] = ToolResult(ToolCall("open_url", {"url": url}), body, ok=True,
            canonical=canonical_json_object({"research_observation": ledger_observation(ledger, "open", url=url),
                                            "read_passages": [relevant.descriptor(), appendix.descriptor()]}))
    scope = answer_scope(session)
    visible = tuple(row for row in scope.aliases if row[1] in preferred)
    expected = _render_context(session, ledger, scope, visible)
    assert answer_context(session, ledger, scope, count_tokens=len, token_budget=len(expected)) == expected
    assert len(scope.aliases) == 4
