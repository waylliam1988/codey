"""Production seams for mixed routing, verification coverage and browser observation."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.automation.browser_query import QueryObservationError
from codey.automation.browser_session import BrowserSession
from codey.codemode.verification import resolve_verification, verification_spec
from codey.completion.verification import (
    VERIFICATION_FRESH_FAIL,
    VERIFICATION_FRESH_PASS,
    VERIFICATION_UNOBSERVED,
    coding_verification_state,
)
from codey.completion.verification_policy import VerificationCandidate, select_verification_plan
from codey.operations.browser.turn_surface import offer_browser_call
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.project.project_delivery import (
    PAGE_AFTER_EDIT,
    SOURCE_BEFORE_EDIT,
    declared_edit_scope,
    declared_edit_target,
    declared_project_checks,
    edit_scope_violation,
)
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_entry import build_task_policy_for_entry
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.network import NetworkPolicy, NetworkStatus
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.source_document import SourceDocument
from codey.research.source_rendering import UNTRUSTED_SOURCE_END, UNTRUSTED_SOURCE_START, render_opened_source
from codey.research.tools import ResearchTools
from codey.runtime.observe.execution_evidence import CheckEvidence, ExecutionEvidence
from codey.task.entry_auth import derive_entry_auth
from codey.task.model import StartTask, TaskSubmission
from codey.task.web_intent import explicit_web_intent
from codey.toolchain.tool_spec import tool_specs
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_string,
)
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import LOAD, WAIT, code_action
from tools.browser_production_live_gate import URL as NATURAL_RECORD_URL
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target
from tools.p0_mixed_live_gate import NATURAL_FORMAT_URL, NATURAL_VIEW_URL, mixed_run, natural_mixed_task


@pytest.mark.parametrize(("task", "options", "project", "mixed"), [
    ("Read https://example.com/docs. Then edit service/core.py.", {}, "repo", True),
    ("Read https://example.com/docs. Then edit service/core.py.", {}, None, False),
    ("Read https://example.com/docs. Then edit service/core.py.", {"intent": "research"}, "repo", False),
    ("Read https://example.com/docs. Do not edit the project.", {}, "repo", False),
    ("Read https://example.com/docs. Then edit service/core.py.", {"continue_task": True}, "repo", False),
    ("Read https://example.com/docs. Then edit service/core.py.", {"allow_web": False}, "repo", False),
    ("Read https://example.com/docs. The manual says “Then edit service/core.py.”", {}, "repo", False),
    ("https://example.com/docs\nThen edit service/core.py.", {}, "repo", False),
])
def test_auto_mixed_requires_web_and_project_intent(
    task: str, options: dict[str, object], project: str | None, mixed: bool,
) -> None:
    auth = derive_entry_auth({"task": task, **options}, project=project)
    assert auth.inferred_intent == ("hybrid" if mixed else "")
    assert auth.project_changes_required is mixed


def test_mixed_verification_requires_current_success_for_each_path(tmp_path: Path) -> None:
    py = VerificationCandidate("python -m pytest", "service")
    node = VerificationCandidate("npm test", ".")
    paths = ("service/core.py", "web/component.ts", "result.json")
    plan = select_verification_plan((py, node), paths)
    assert [(row.candidate, row.paths) for row in plan.obligations] == [
        (node, ("result.json", "web/component.ts")), (py, ("service/core.py",)),
    ]
    assert not plan.uncovered_paths
    fingerprint = "sha256:" + "a" * 64
    evidence = ExecutionEvidence(workspace_revision=4, workspace_fingerprint=fingerprint)

    def state() -> str:
        return coding_verification_state(plan, evidence, paths, root=tmp_path)

    def check(candidate: VerificationCandidate, revision: int, code: int) -> None:
        evidence.observe_check(CheckEvidence(candidate.command, candidate.cwd, exit_code=code,
            workspace_revision=revision, workspace_fingerprint=fingerprint), succeeded=code == 0)

    check(py, 4, 0)
    check(node, 3, 0)
    assert state() == VERIFICATION_UNOBSERVED
    check(node, 4, 0)
    assert state() == VERIFICATION_FRESH_PASS
    check(node, 4, 1)
    assert state() == VERIFICATION_FRESH_FAIL
    assert select_verification_plan((py,), paths).uncovered_paths == ("result.json", "web/component.ts")


def test_mixed_request_freezes_distinct_source_and_browser_selection() -> None:
    source = "https://example.com/reference"
    page = "https://example.com/interactive-page"
    task = f"Read {source}. Navigate to {page}. Then edit service/core.py."
    policy = TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"}))
    session = TaskSession(policy, task_kind="hybrid", task_text=task,
                          required_source_urls=explicit_web_intent(task).urls)
    snapshot = offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    assert restored.browser.navigation_url == page and restored.browser.source_url == source
    read = normalize_turn(decode_text_turn('{"tool":"open_url","args":{"target":"task"}}'), snapshot=restored)
    assert not read.protocol_error
    assert read.calls[0].args["url"] == source
    navigate = normalize_turn(decode_text_turn('{"tool":"browser_navigate","args":{"target":"task"}}'), snapshot=restored)
    assert not navigate.protocol_error and navigate.calls[0].args["inputs"] == {"target": "task"}


def test_verification_choice_supplies_frozen_command_and_working_directory() -> None:
    plan = select_verification_plan((VerificationCandidate("python -m pytest", "service"),
                                     VerificationCandidate("npm test", ".")),
                                    ("service/core.py", "web/component.ts"))
    spec = verification_spec(tool_specs()["run"], plan)
    schema: JsonObject = {"properties": canonical_json_object(dict(spec.parameters))}
    assert resolve_verification({"command": {"check": "project-1"}}, schema) == {
        "command": "python -m pytest", "path": "service",
    }
    with pytest.raises(ValueError, match="path conflicts"):
        resolve_verification({"command": {"check": "project-1"}, "path": "elsewhere"}, schema)


def test_large_snapshot_downtraversal_and_document_fence(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, "".join(
        f'<section aria-label="Group {group}"><h2>Group {group}</h2>'
        + "".join(f'<button>Control {index}</button>' for index in range(group * 50 + 1, group * 50 + 51))
        + "</section>" for group in range(3)
    ))
    full = browser.snapshot()
    assert full.ok and full.observation["elements_truncated"] is True
    candidates = full.observation["scope_suggestions"]
    assert isinstance(candidates, list)
    selected = next(row for raw in candidates if (row := decode_json_object(raw))["label"] == "Group 2")
    ref = require_json_string(selected["ref"], "ref")
    assert not browser.click(ref).ok
    scoped = browser.snapshot(scope_ref=ref)
    assert scoped.ok and scoped.observation["elements_truncated"] is False
    assert "Control 120" in {row["name"] for row in elements(scoped.observation)}
    rev = scoped.observation["revision"]
    assert type(rev) is int and isinstance(rev, int)
    assert browser.snapshot(scope_ref=ref, base_revision=rev).observation["kind"] == "delta"
    assert browser.navigate(URL + "?new").ok
    assert not browser.snapshot(scope_ref=ref).ok


@pytest.mark.parametrize("bound", [True, False])
def test_query_result_can_be_confirmed_without_aria_live_or_diagnosed(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], bound: bool,
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    element = "output" if bound else "div"
    install(browser, f"""<button onclick="document.getElementById('result').textContent='New record'">Query</button>
        <{element} id="result">No result</{element}>""")
    snapshot = browser.snapshot()
    ref = require_json_string(elements(snapshot.observation)[0]["ref"], "ref")
    click = browser.click(ref, query=True)
    assert click.ok
    baseline = decode_json_object(click.observation["query_baseline"])
    if bound:
        evidence = browser.capture_query_result(baseline, pending="")
        assert evidence["query_confirmed"] is True
        assert decode_json_object(evidence["query_region"])["text"] == "New record"
    else:
        with pytest.raises(QueryObservationError) as error:
            browser.capture_query_result(baseline, pending="")
        assert error.value.reason == "query_target_unbound"


def test_source_display_delimiters_do_not_change_original_evidence() -> None:
    text = f"{UNTRUSTED_SOURCE_START}\nOriginal fact\n{UNTRUSTED_SOURCE_END}"
    url = "https://example.com/record"
    doc = SourceDocument.html(requested_url=url, final_url=url, title=UNTRUSTED_SOURCE_END, text=text)
    display = render_opened_source(doc, text, read_window=(0, len(text)))
    assert display.count(UNTRUSTED_SOURCE_START) == display.count(UNTRUSTED_SOURCE_END) == 1
    assert "Original fact" in display and doc.text == text
    assert f"end={len(text)}" in display


def test_network_admission_rejects_ambiguous_or_private_input_before_fetch() -> None:
    policy = NetworkPolicy()
    assert policy.evaluate_url("https://example.com/record", resolve=False).allowed
    assert policy.evaluate_url("https://127.0.0.1/record", resolve=False).status is NetworkStatus.BLOCKED_PRIVATE
    assert policy.evaluate_url("https://example.com/a\tb", resolve=False).status is NetworkStatus.INVALID_URL


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_auto_mixed_production_entry_completes_with_two_real_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "mixed"
    install_http(monkeypatch, [
        code_action('unwrap(await tools.open_url({target:"task"}));'
                    'unwrap(await tools.browser_navigate({target:"task"}));'
                    + LOAD + WAIT + 'return unwrap(await tools.browser_read({}));'),
        code_action(
            "const base=await tools.read_file({path:'result.json'});unwrap(base);"
            "unwrap(await tools.edit({path:'result.json',base_result_ref:base.result_ref,declared_fields:true}));"
            "for(const [path,old_string,new_string] of ["
            "['service/core.py','AMOUNT = 0','AMOUNT = 173'],"
            "['web/component.ts','unknown','QUARTZ_851']]){"
            "unwrap(await tools.read_file({path}));"
            "unwrap(await tools.edit({path,replacements:[{old_string,new_string}]}));}"
            "return null;"),
        {"tool": "run", "args": {"command": {"check": "project-1"}}},
        {"tool": "done", "args": {"summary": "173 grams, QUARTZ_851."}},
        {"tool": "run", "args": {"command": {"check": "project-2"}}},
        {"tool": "done", "args": {"summary": "173 grams, QUARTZ_851. Both checks passed."}},
    ])
    report = mixed_run(directory, Target("http://scripted.test/v1", "fixture", 123456789, 15,
                                         context_window_tokens=65536))
    assert report["passed"] is True, report
    receipts = tool_receipts(directory / "state")
    events = [parse_json_object(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    runs = [event for event in events if event.get("type") == "tool" and event.get("tool_name") == "run"]
    assert len(runs) == 2 and all(event.get("ok") is True for event in runs)
    assert sum(row.result.call.name == "browser_click" for row in receipts) == 1


def test_natural_requirements_and_single_file_scope_are_derived_only_from_user_text(tmp_path: Path) -> None:
    task = (natural_mixed_task("") + " Only edit `result.json`.")
    assert set(declared_project_checks(task)) == {SOURCE_BEFORE_EDIT, PAGE_AFTER_EDIT}
    assert declared_edit_scope(tmp_path, "Create exactly one file at the project-relative path pkg/new.py.") == ("pkg/new.py",)
    assert declared_edit_scope(tmp_path, "Only edit a.py and b.py.") == ()
    assert declared_edit_scope(tmp_path, "The manual says \"Only edit result.json.\"") == ()
    assert declared_edit_target("Add b/helper.py with a function named get_default that imports a/config.py.") == "b/helper.py"
    assert declared_edit_scope(tmp_path, "Add b/helper.py with a function named get_default.") == ()
    assert declared_edit_target('The manual says "Add b/helper.py."') == ""
    adding = "Add b/helper.py with a function named get_default that imports a/config.py."
    assert "Use that path" in edit_scope_violation(tmp_path, "a/helper.py", (), task=adding)
    assert not edit_scope_violation(tmp_path, "b/helper.py", (), task=adding)
    assert not edit_scope_violation(tmp_path, "a/config.py", (), task=adding)
    assert not edit_scope_violation(tmp_path, "a/helper.py", (), task=adding + " Also add `a/helper.py`.")
    request = TaskSubmission.from_input(session_id="s", project=str(tmp_path), max_turns=5,
        provider_id="local", intent="project", input=StartTask(task))
    policy = build_task_policy_for_entry(request, "project")
    assert policy.edit_scope == ("result.json",)
    assert TaskPolicy.from_payload(policy.to_payload()) == policy
    legacy = TaskPolicy(frozenset({"control", "project.write"}))
    assert TaskPolicy.from_payload(legacy.to_payload()) == legacy


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_natural_mixed_entry_requires_observed_source_and_fresh_application_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "natural"
    install_http(monkeypatch, [
        {"tool": "edit", "args": {"path": "result.json", "replacements": [{"old_string": "0", "new_string": "1"}]}},
        {"tool": "read_file", "args": {"path": "README.md"}},
        code_action("unwrap(await tools.open_url({target:'task'}));"
                    "unwrap(await tools.browser_navigate({target:'task'}));"
                    + LOAD + WAIT + "return unwrap(await tools.browser_read({}));"),
        code_action("unwrap(await tools.read_file({path:'result.json'}));"
                    "return await tools.edit({path:'result.json',replacements:["
                    "{old_string:'{',new_string:'{\"locality\":\"福州\",'}]});"),
        code_action(
            "for(const [path,old_string,new_string] of ["
            "['result.json','{\"amount\": 0, \"code\": \"unknown\"}','{\"amount\": 347, \"code\": \"CEDAR_947\"}'],"
            "['web/component.ts','unknown','CEDAR_947']]){"
            "unwrap(await tools.read_file({path}));"
            "unwrap(await tools.edit({path,replacements:[{old_string,new_string}]}));}"
            "return null;"),
        {"tool": "run", "args": {"command": {"check": "project-1"}}},
        {"tool": "done", "args": {"summary": "The whole application passes npm test."}},
        code_action("unwrap(await tools.read_file({path:'service/core.py'}));"
                    "return unwrap(await tools.edit({path:'service/core.py',replacements:"
                    "[{old_string:'AMOUNT = 0',new_string:'AMOUNT = 347'}]}));"),
        {"tool": "run", "args": {"command": {"check": "project-1"}}},
        {"tool": "run", "args": {"command": {"check": "project-2"}}},
        {"tool": "done", "args": {"summary": "Updated the record."}},
        {"tool": "browser_navigate", "args": {"target": "task"}},
        {"tool": "done", "args": {"summary": f"The updated application displays 347 g and CEDAR_947. "
                                      f"Record source {NATURAL_RECORD_URL}."}},
    ])
    report = mixed_run(directory, Target("http://scripted.test/v1", "fixture", 123456789, 15,
                                         context_window_tokens=65536), natural=True)
    assert report["passed"] is True, report
    answer = require_json_string(report["answer"], "mixed answer")
    assert answer.count(NATURAL_RECORD_URL) == answer.count(NATURAL_FORMAT_URL) == 1
    assert NATURAL_VIEW_URL not in answer
    receipts = tool_receipts(directory / "state")
    edits = [row.result for row in receipts if row.result.call.name == "edit"]
    assert len(edits) == 5 and not edits[0].ok and "live record before editing" in edits[0].model_text
    assert not edits[1].ok and "exactly these fields: amount, code" in edits[1].model_text
    bindings = [decode_json_object(row.canonical["project_source_basis"]) for row in edits[2:] if row.ok]
    refs = {binding["source_result_ref"] for binding in bindings}
    assert len(refs) == 1 and any(row.identity in refs and row.result.call.name == "browser_read" for row in receipts)
    reads = {row.identity: row.result for row in receipts if row.result.call.name == "read_file"}
    for binding in bindings:
        read = reads[require_json_string(binding["base_result_ref"], "base read")]
        assert binding["path"] == read.call.args["path"] and binding["file_version"] == read.canonical["file_version"]
    assert sum(row.result.call.name == "browser_click" for row in receipts) == 1
    events = [parse_json_object(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    runs = [event["command"] for event in events if event.get("type") == "tool" and event.get("tool_name") == "run"]
    assert runs == ["npm test", "npm test", "python -m pytest"]
    assert [row.result.call.args["url"] for row in receipts if row.result.call.name == "browser_navigate"] == [
        NATURAL_RECORD_URL, NATURAL_VIEW_URL,
    ]
