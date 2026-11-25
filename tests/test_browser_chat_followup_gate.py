"""Check the final browser experiment with fixed replies and real browser receipts."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations import provider_receipts
from codey.operations.kernel.kernel_protocol import TurnSnapshot, refresh_snapshot_contract
from codey.operations.provider_request_context import ProviderRequestContext
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.ledger import ResearchLedger
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_chat_followup_gate as gate
from tools import browser_followup_ab as followup
from tools import browser_leaf_exposure_ab as base
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import Target, production_source_digest

TARGET = Target("http://scripted.test/v1", "fixture", 123456789, 15)


def request(tool: str, **args: object) -> JsonObject:
    return {"tool": tool, "args": decode_json_object(args)}


def observed(directory: Path, name: str) -> str:
    snapshots = [item.result for item in tool_receipts(directory / "state") if item.result.ok
                 and item.result.call.name == "browser_snapshot"]
    elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
    assert isinstance(elements, list)
    return next(str(e["ref"]) for e in elements if isinstance(e, dict) and e.get("name") == name)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_paired_matrix_checks_real_provider_schema_and_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[JsonObject] = []
    arms = [arm for arm in gate.ARMS if arm != "bound_text"]
    for arm in arms:
        directory = tmp_path / arm

        def proposals(arm: str = arm, directory: Path = directory) -> Iterator[JsonObject]:
            snapshot = request("browser_snapshot") if arm != "codemode" else request(
                "codemode", code="return unwrap(await tools.browser_snapshot({}));")
            yield snapshot
            fill: JsonObject = {"ref": observed(directory, "备注"), "value": base.NOTE}
            yield request("browser_fill", **fill) if arm != "codemode" else request(
                "codemode", code="return unwrap(await tools.browser_fill(inputs));", inputs=fill)
            if arm in {"codemode", "direct"}:
                yield snapshot
            click: JsonObject = {"ref": observed(directory, "查询记录")}
            if arm == "codemode":
                yield request("codemode", code="return unwrap(await tools.browser_click(inputs));", inputs=click)
            elif arm == "purpose":
                yield request("browser_click", **click, purpose="result")
            else:
                yield request("browser_click", **click)
            if arm != "purpose":
                yield request("browser_read") if arm != "codemode" else request(
                    "codemode", code="return unwrap(await tools.browser_read({}));")
            yield request("done", summary="备注已受理，回执码 NOTE_624。[s1]")

        install_http(monkeypatch, proposals())
        row = gate.run_unit(directory, "quoted", gate.CASES["quoted"], arm, TARGET)
        assert row["passed"], row
        assert row["actual_clicks"] == 1
        assert row["query_linked_reads"] == int(arm == "purpose")
        runs.append({**row, "wording": "original", "repeat": 1})
    assert [row["model_calls"] for row in runs] == [6, 6, 5, 4]
    def audit(rows: list[JsonObject]) -> JsonObject:
        return gate.audit_matrix(tmp_path, rows, target=TARGET, arms=arms, cases=["quoted"], wordings=["original"],
                                 repeats=1, source=production_source_digest(), harness=gate.harness_digest())

    assert audit(runs)["http_requests"] == 21
    purpose = runs[-1]
    bad_clicks: JsonObject = {**purpose, "page_state": {**decode_json_object(purpose["page_state"]), "clicks": 2}}
    assert gate.score(tmp_path / "purpose", "quoted", gate.CASES["quoted"], bad_clicks)["passed"] is False
    wrong_answer: JsonObject = {**purpose, "answer": "已经受理，回执码 OTHER_627。[s1]"}
    assert gate.score(tmp_path / "purpose", "quoted", gate.CASES["quoted"], wrong_answer)["passed"] is False
    wrong_citation: JsonObject = {**purpose, "answer": "备注已受理，回执码 NOTE_624。[s9]"}
    assert gate.score(tmp_path / "purpose", "quoted", gate.CASES["quoted"], wrong_citation)["passed"] is False
    with pytest.raises(AssertionError, match="duplicate"):
        audit([*runs, runs[0]])
    with pytest.raises(AssertionError, match="different task, fixture, or authority"):
        audit([*runs[:-1], {**runs[-1], "fixture_digest": "changed"}])
    read_records = followup.jsonl
    leaks: tuple[tuple[str, JsonValue], ...] = (("browser_followups", True), ("browser_fill_text", base.NOTE))
    for key, value in leaks:
        def leaked_records(path: Path, key: str = key, value: JsonValue = value) -> list[JsonObject]:
            rows = read_records(path)
            if path.name == "snapshots.jsonl":
                return [{**row, "snapshot": {**decode_json_object(row["snapshot"]), key: value}} for row in rows]
            return rows

        with monkeypatch.context() as patch:
            patch.setattr(followup, "jsonl", leaked_records)
            with pytest.raises(AssertionError, match="Production browser follow-ups leaked"):
                audit(runs)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("name", ["async", "explicit_wait"])
def test_old_page_content_never_becomes_query_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    directory = tmp_path / name

    def proposals() -> Iterator[JsonObject]:
        yield request("browser_snapshot")
        yield request("browser_fill", ref=observed(directory, "城市"), value="福州")
        yield request("browser_click", ref=observed(directory, "应用"), purpose="result")
        yield request("done", summary="福州样本：净重 263 克，校验码 FRESH_263。[s1]")

    install_http(monkeypatch, proposals())
    row = gate.run_unit(directory, name, gate.CASES[name], "purpose", TARGET)
    assert row["passed"] and row["wait_order"], row
    assert row["actual_clicks"] == 1 and row["stale_body_reads"] == 0 and row["query_linked_reads"] == 1
    assert row["fill_followups"] == row["result_followups"] == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_timeout_blocks_old_answer_and_recovers_by_reading_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "timeout"
    real_capture = gate.guard_capture
    captures = 0

    def capture(browser: BrowserSession, baseline: JsonObject, *, wait_seconds: float) -> dict[str, object]:
        nonlocal captures
        captures += 1
        return real_capture(browser, baseline, wait_seconds=0 if captures == 1 else 6)

    monkeypatch.setattr(gate, "guard_capture", capture)

    def proposals() -> Iterator[JsonObject]:
        yield request("browser_snapshot")
        yield request("browser_fill", ref=observed(directory, "城市"), value="福州")
        yield request("browser_click", ref=observed(directory, "应用"), purpose="result")
        yield request("done", summary="北京样本 41 克，STALE_041。[s1]")
        yield request("browser_read")
        yield request("done", summary="福州样本 263 克，FRESH_263。[s1]")

    install_http(monkeypatch, proposals())
    row = gate.run_unit(directory, "async", gate.async_case(delay=3500), "purpose", TARGET)
    assert row["passed"], row
    assert row["pending_submissions"] == 1 and row["stale_body_reads"] == 0
    assert row["actual_clicks"] == 1 and row["query_linked_reads"] == 1
    failed_reads = [item.result for item in tool_receipts(directory / "state")
                    if item.result.call.name == "browser_read" and not item.result.ok]
    assert len(failed_reads) == 1
    assert "research_observation" not in failed_reads[0].canonical


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_controls_branch_supplies_refs_for_the_next_click(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "controls"

    def proposals() -> Iterator[JsonObject]:
        yield request("browser_snapshot")
        yield request("browser_click", ref=observed(directory, "筛选菜单"), purpose="controls")
        yield request("browser_click", ref=observed(directory, "展开 C-7"), purpose="result")
        yield request("done", summary="负责人周青，回执码 DETAIL_713。[s1]")

    install_http(monkeypatch, proposals())
    row = gate.run_unit(directory, "controls", gate.CASES["controls"], "purpose", TARGET)
    assert row["passed"] and row["actual_clicks"] == 2, row
    assert row["controls_followups"] == row["result_followups"] == row["query_linked_reads"] == 1
    assert decode_json_object(row["browser_successes"])["browser_read"] == 1


def test_model_purpose_is_stripped_and_native_id_preserved(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
                          task_kind="chat")
    with gate.intervention(tmp_path, "purpose"):
        snapshot = base.expose_leaves(build_turn_snapshot(session, native=True), session)
        for purpose in gate.PURPOSES:
            reply = AssistantTurn(tool_calls=(ProviderToolCall("click-1", "browser_click", {"ref": "r", "purpose": purpose}),))
            plan = gate.normalize_candidate(reply, snapshot, "purpose")
            assert not plan.protocol_error and plan.calls[0].call_id == "click-1"
            args = plan.calls[0].args
            assert (args if purpose == "action" else decode_json_object(args["inputs"])["click"]) == {"ref": "r"}
        invalid = AssistantTurn(tool_calls=(ProviderToolCall("bad", "browser_click", {"ref": "r", "purpose": "unknown"}),))
        assert gate.normalize_candidate(invalid, snapshot, "purpose").protocol_error


def test_changed_region_requires_matching_document_and_a_settled_result() -> None:
    before: JsonObject = {"document": "a", "regions": [{"key": "record", "text": "old", "busy": False}]}
    assert gate.changed_region(before, before) == ""
    assert gate.changed_region(before, {**before, "regions": [{"key": "record", "text": "new", "busy": True}]}) == ""
    assert gate.changed_region(before, {"document": "b", "regions": [{"key": "record", "text": "new", "busy": False}]}) == ""
    assert gate.changed_region(before, {**before, "regions": [{"key": "record", "text": "new", "busy": False}]}) == "new"
    regions = before["regions"]
    assert isinstance(regions, list)
    unrelated: list[JsonValue] = [*regions, {"key": "clock", "text": "tick", "busy": False}]
    assert gate.changed_region(before, {**before, "regions": unrelated}) == ""


def test_explicit_payload_preserves_quotes_and_whitespace_and_skips_ambiguous_text() -> None:
    assert gate.explicit_fill_text(gate.CASES["quoted"].question) == base.NOTE
    assert gate.explicit_fill_text(gate.PARAPHRASES["quoted"]) == base.NOTE
    assert gate.explicit_fill_text(gate.PARAPHRASES["quoted_block"]) == base.NOTE
    assert gate.explicit_fill_text('备注请原样输入下面两行。\n  "first"  \n请保留原文 ') == '  "first"  \n请保留原文 '
    assert gate.explicit_fill_text("不要填备注。请原样输入下面两行。\na\nb") is None
    assert gate.explicit_fill_text(gate.CASES["quoted"].question + "\n这是附加说明。") is None
    assert gate.explicit_fill_text(gate.CASES["quoted_block"].question + "\n```\n第二段\n```") is None
    assert gate.explicit_fill_text(gate.PARAPHRASES["quoted"] + "\n\n另一个段落。") is None
    assert gate.explicit_fill_text('请录入这段备注。\n用户说"明天到"\n请保留原文') is None
    assert gate.explicit_fill_text('不要保留引号和换行录入这段备注。\na\nb') is None


def test_frozen_fill_roundtrip_and_admission(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
                          task_kind="chat")
    with gate.intervention(tmp_path, "bound_text"):
        snapshot = base.expose_leaves(build_turn_snapshot(session, native=True), session)
        bound: TurnSnapshot = gate.with_fill_text(snapshot, base.NOTE)
        bound = refresh_snapshot_contract(bound)
        request_context = ProviderRequestContext(1, bound, None, "", ())
        payload = request_context.to_payload()
        restored = ProviderRequestContext.from_payload(payload).snapshot
        assert restored == bound
        reply = AssistantTurn(tool_calls=(ProviderToolCall("fill-1", "browser_fill", {
            "ref": "current", "source": gate.BOUND_SOURCE}),))
        plan = gate.normalize_candidate(reply, restored, "bound_text")
        assert not plan.protocol_error and plan.calls[0].call_id == "fill-1"
        assert plan.calls[0].args["inputs"] == {"ref": "current", "value": base.NOTE}
        assert base.NOTE not in str(plan.calls[0].args["code"])
        invalid = decode_text_turn('{"tool":"browser_fill","args":{"ref":"current","source":"other"}}')
        assert gate.normalize_candidate(invalid, restored, "bound_text").protocol_error
        invalid = decode_text_turn('{"tool":"browser_fill","args":{"ref":"current","source":"task_text","value":"extra"}}')
        assert gate.normalize_candidate(invalid, restored, "bound_text").protocol_error
        assert gate.normalize_candidate(reply, replace(bound, allowed=("codemode", "browser_fill")), "bound_text").protocol_error
        raw = decode_json_object(payload["snapshot"])
        invalid_binding: JsonObject = {**raw, "experiment_fill_binding": {"version": True,
            "source": gate.BOUND_SOURCE, "text": base.NOTE}}
        with pytest.raises(ValueError):
            provider_receipts.restore_snapshot(invalid_binding)
        with pytest.raises(ValueError):
            provider_receipts.restore_snapshot({k: v for k, v in raw.items() if k != "experiment_fill_binding"})


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("wording", ["original", "paraphrase"])
def test_bound_fill_uses_real_provider_snapshot_and_browser_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wording: str,
) -> None:
    directory = tmp_path / "bound"

    def proposals() -> Iterator[JsonObject]:
        yield request("browser_snapshot")
        yield request("browser_fill", ref=observed(directory, "备注"), source=gate.BOUND_SOURCE)
        yield request("browser_click", ref=observed(directory, "查询记录"), purpose="result")
        yield request("done", summary="备注已受理，回执码 NOTE_624。[s1]")

    install_http(monkeypatch, proposals())
    case = gate.CASES["quoted"]
    if wording == "paraphrase":
        case = replace(case, question=gate.PARAPHRASES["quoted"])
    row = gate.run_unit(directory, "quoted", case, "bound_text", TARGET)
    assert row["passed"] and row["bound_fills"] == row["verified_exact_fills"] == row["actual_clicks"] == 1, row
    assert row["model_calls"] == 4
    payloads = [decode_json_object(item["payload"]) for _, item in artifacts(directory / "state")
                if item.get("kind") == "provider_request"]
    assert payloads
    snap = decode_json_object(payloads[0]["snapshot"])
    assert decode_json_object(snap["experiment_fill_binding"])["text"] == base.NOTE
    with gate.intervention(directory, "bound_text"):
        restored = ProviderRequestContext.from_payload(payloads[0]).snapshot
    assert isinstance(restored, gate.BoundFillSnapshot) and restored.fill_text == base.NOTE
    assert gate.audit_matrix(tmp_path, [{**row, "wording": wording, "repeat": 1}], target=TARGET,
        arms=["bound_text"], cases=["quoted"], wordings=[wording], repeats=1, source=str(row["source_digest"]),
        harness=str(row["harness_digest"]))["http_requests"] == 4


def test_citation_fallback_only_accepts_an_unambiguous_opened_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = ResearchLedger()
    read = {"https://example.com/record", "https://example.com/条目，细节"}

    def opened_url(url: str) -> str:
        return url if url in read else ""

    monkeypatch.setattr(ledger, "canonical_opened_url", opened_url)
    resolve = gate.resolve_citation_boundary
    assert resolve("https://example.com/record，并把原文填入", ledger) == "https://example.com/record"
    assert resolve("https://example.com/条目，细节", ledger) == "https://example.com/条目，细节"
    for url in ("https://example.com/other，并把原文填入", "https://example.com/record，接着https://example.org/other",
                "https://example.com/record-next，并把原文填入"):
        assert resolve(url, ledger) == url
    read.add("https://example.com/record，细节")
    ambiguous = "https://example.com/record，细节，并把原文填入"
    assert resolve(ambiguous, ledger) == ambiguous
