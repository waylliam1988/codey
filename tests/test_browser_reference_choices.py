"""Observed choices and evidence-based reference failures share the original leaves."""
from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from codey.automation.browser_session import BrowserSession, BrowserToolOutput
from codey.codemode.catalog import snapshot_catalog
from codey.operations.browser.browser_references import BrowserReferences, reference_failure
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.kernel.kernel_protocol import refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import validate_args_with_spec
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, require_json_string
from tests.test_browser_context_delivery import URL, install
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_readable_references import binding, observed_snapshot, program
from tests.test_browser_result_adoption_ab import DOCUMENT, session_with_snapshot


@pytest.mark.parametrize("name", ["Locality", '欄位“查询”', 'champ "ville"', 'حقل', 'field-' + uuid4().hex])
def test_choices_are_whole_observed_combinations_and_shared_across_surfaces(name: str) -> None:
    ref = uuid4().hex
    observation: JsonObject = {"generation": 1, "complete": True, "elements_truncated": False,
        "elements": [{"ref": ref, "role": "textbox", "name": name, "context_id": "one", "disabled": False},
                     {"ref": uuid4().hex, "role": "button", "name": "other", "context_id": "two", "disabled": False}],
        "contexts": [{"id": "one", "label": "region one"}, {"id": "two", "label": "region two"}]}
    references = BrowserReferences.observe("receipt-" + uuid4().hex, observation, DOCUMENT)
    session = session_with_snapshot()
    frozen = refresh_snapshot_contract(replace(offer_browser_tools(build_turn_snapshot(session), session),
                                              browser_references=references))
    restored = restore_snapshot(snapshot_description(frozen))
    assert restored == frozen
    outer = next(spec for spec in restored.model_specs if spec.name == "browser_fill")
    nested = next(row for row in snapshot_catalog(restored).entries if row.name == "browser_fill")
    nested_ref = decode_json_object(canonical_json_object(nested.parameters)["properties"])["ref"]
    assert canonical_json_object(dict(outer.parameters)["ref"]) == nested_ref
    assert not validate_args_with_spec(outer, {"ref": {"role": "textbox", "name": name}, "value": "chosen"})
    for selector in ({"role": "button", "name": name},
                     {"role": "textbox", "name": name, "context": "region two"}):
        assert validate_args_with_spec(outer, {"ref": selector, "value": "chosen"})
    assert references.resolve({"role": "textbox", "name": name}) == ref
    for spec in restored.model_specs:
        if spec.name in {"browser_fill", "browser_click"}:
            example = "Example ref argument: " + json.dumps(references.choices(spec.name)[0], ensure_ascii=False)
            assert not spec.json_examples
            assert example in spec.description
    example = "Example ref argument: " + json.dumps(references.choices("browser_fill")[0], ensure_ascii=False)
    assert example in nested.description
    # Raw refs remain a formal capability, including refs acquired inside JS.
    assert not validate_args_with_spec(outer, {"ref": "newly-observed-in-program", "value": "chosen"})


def test_action_choices_are_compact_and_do_not_offer_clicking_the_input() -> None:
    refs = binding()
    assert len(refs.choices()) == len(refs.controls)
    assert all(isinstance(choice, dict) and choice["role"] == "textbox" for choice in refs.choices("browser_fill"))
    assert all(isinstance(choice, dict) and choice["role"] != "textbox" for choice in refs.choices("browser_click"))
    legacy = replace(refs, presentation_version=2)
    assert len(legacy.choices()) > len(refs.choices())


@pytest.mark.parametrize("same_context", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_candidate_uniqueness_includes_disabled_neighbors(same_context: bool, disabled: bool) -> None:
    refs = binding(duplicate=True, same_context=same_context, disabled=disabled)
    choices = refs.choices()
    assert {"role": "textbox", "name": "备注"} not in choices
    assert bool(choices) is (not same_context)
    for candidate in choices:
        actual = refs.resolve(candidate)
        assert not next(row for row in refs.controls if row.ref == actual).disabled


@pytest.mark.parametrize("supplied,reason", [
    ("original", "stale_ref"), ('[original] textbox "old label"', "invalid_ref_format"),
    ("unobserved", "unknown_ref"), ("original-with-invented-suffix", "unknown_ref"),
])
def test_diagnosis_uses_receipts_and_never_rebinds(supplied: str, reason: str) -> None:
    receipt = ToolResult(ToolCall("browser_snapshot", {}), "observed", ok=True,
        canonical={"value": {"elements": [{"ref": "original", "role": "textbox", "name": "old label"}]}})
    output = BrowserToolOutput("unknown", False, {"action": "fill", "ref": supplied,
                                                "reason": "unknown_ref", "delivery": "not_delivered"})
    result = reference_failure(output, {"formal-snapshot": receipt})
    assert result.observation["reason"] == reason
    assert result.observation["ref"] == supplied
    assert result.observation["delivery"] == "not_delivered"
    assert reference_failure(output, {}) == output
    assert reference_failure(output, {"failed": replace(receipt, ok=False)}) == output


@pytest.mark.parametrize("delivery", ["uncertain", "delivered", "unverified"])
def test_uncertain_or_delivered_actions_never_get_a_not_delivered_correction(delivery: str) -> None:
    output = BrowserToolOutput("failed after action", False,
        {"ref": "unobserved", "reason": "unknown_ref", "delivery": delivery})
    assert reference_failure(output, {}) is output


def test_original_whole_display_row_is_rejected_then_current_choice_fills_once(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall("browser_navigate", {"url": URL}))[1]
    name = "input-" + uuid4().hex
    install(browser, '<input aria-label="' + name + '">')
    frozen = observed_snapshot(session, browser)
    assert frozen.browser_references is not None
    original = frozen.browser_references.controls[0].ref
    bad = f'[{original}] textbox "{name}"'
    bad_args: JsonObject = {"ref": bad, "value": "chosen"}
    rejected = program(session, tools, frozen, "return unwrap(await tools.browser_fill(" + json.dumps(bad_args) + "));")
    assert not rejected.ok and "display row" in rejected.model_text
    failed = next(row for row in session._memory_results.values() if row.call.name == "browser_fill")
    assert decode_json_object(failed.canonical["value"])["reason"] == "invalid_ref_format"
    assert failed.call.args["ref"] == bad
    assert browser.generation == frozen.browser_references.generation
    selector: JsonObject = {"role": "textbox", "name": name}
    restored = restore_snapshot(snapshot_description(frozen))
    completed = program(session, tools, restored, "return unwrap(await tools.browser_fill({ref:"
                        + json.dumps(selector) + ",value:'chosen'}));", turn=2)
    assert completed.ok, completed.model_text
    fills = [row for row in session._memory_results.values() if row.call.name == "browser_fill" and row.ok]
    assert len(fills) == 1 and fills[0].call.args["ref"] == original


def test_fresh_raw_refs_inside_a_program_remain_available(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall("browser_navigate", {"url": URL}))[1]
    install(browser, '<input aria-label="dynamic">')
    frozen = observed_snapshot(session, browser)
    result = program(session, tools, frozen, "const s=unwrap(await tools.browser_snapshot({}));"
                     "return unwrap(await tools.browser_fill({ref:s.elements[0].ref,value:'fresh'}));")
    assert result.ok, result.model_text


@pytest.mark.parametrize("changed", ["parameters", "grant", "output_schema"])
def test_changed_execution_contract_still_rejects_receipt_replay(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], changed: str,
) -> None:
    session, tools, browser = production_browser
    assert ExecutionDelegate(session=session, research_tools=tools).execute(ToolCall("browser_navigate", {"url": URL}))[1]
    install(browser, '<button onclick="window.delivered=(window.delivered||0)+1">Submit</button>')
    frozen = observed_snapshot(session, browser)
    code = "const r=unwrap(await tools.browser_click({ref:{role:'button',name:'Submit'}}));"
    failed = program(session, tools, frozen, code)
    assert failed.audit.get("error_code") == "missing_return", failed.model_text
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "repair token")
    click = next(spec for spec in frozen.frozen_specs if spec.name == "browser_click")
    if changed == "parameters":
        changed_click = replace(click, parameters=(("ref", {"type": "string", "minLength": 2}),))
    elif changed == "grant":
        changed_click = replace(click, grant="browser.effect")
    else:
        changed_click = replace(click, output_schema={"type": "string"})
    current = refresh_snapshot_contract(replace(frozen, frozen_specs=tuple(
        changed_click if spec.name == "browser_click" else spec for spec in frozen.frozen_specs)))
    rejected = program(session, tools, current, code + "return r;", turn=2, repair_of=token)
    assert not rejected.ok and "capability changed" in rejected.model_text, rejected.model_text
    assert browser.evaluate("window.delivered").observation["value"] == 1
    assert len([row for row in session._memory_results.values() if row.call.name == "browser_click"]) == 1


@pytest.mark.parametrize("case", ["unknown", "disabled", "stale"])
def test_formal_failure_reasons_distinguish_evidence_without_delivering(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], case: str,
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert delegate.execute(ToolCall("browser_navigate", {"url": URL}))[1]
    install(browser, '<input aria-label="field"' + (' disabled' if case == "disabled" else '') + '>')
    frozen = observed_snapshot(session, browser)
    assert frozen.browser_references is not None
    ref = "unobserved-" + uuid4().hex if case == "unknown" else frozen.browser_references.controls[0].ref
    if case == "stale":
        install(browser, '<input aria-label="field">')
    result = program(session, tools, frozen, "return unwrap(await tools.browser_fill({ref:"
                     + json.dumps(ref) + ",value:'never'}));")
    assert not result.ok
    child = next(row for row in session._memory_results.values() if row.call.name == "browser_fill")
    value = decode_json_object(child.canonical["value"])
    assert value["reason"] == {"unknown": "unknown_ref", "disabled": "disabled_control", "stale": "stale_ref"}[case]
    assert value["delivery"] == "not_delivered"
    assert require_json_string(value["ref"], "ref") == ref
