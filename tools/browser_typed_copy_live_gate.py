"""Typed browser copies through the existing headless kernel and real Chromium."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser_contracts import RouteDecision
from codey.types.json_value import JsonObject
from tools.browser_experiment_sources import ResumableBrowserSearchProvider
from tools.browser_leaf_exposure_ab import response_actions
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    write_json,
)

URL = "https://example.com/typed-source"
NUMBER = "123456789012345.67890123456789"


@dataclass(frozen=True)
class CopyCase:
    path: str
    task: str
    html: str
    original: str
    expected: str


def copy_case(name: str) -> CopyCase:
    path = "settlement.json" if name == "renamed" else "ledger.json"
    field = "due" if name == "renamed" else "balance"
    value = "-0.1250" if name == "decimal" else NUMBER
    original = f'{{"{field}":0,"label":"unknown","untouched":9007199254740993.123456789}}\n'
    source = f'{{"record":{{"{field}":{value},"label":"000783"}}}}'
    html = '<article>Settlement record is ready.</article><script id="settlement" type="application/json">' + source + '</script>'
    task = f"打开 {URL}，把网页 JSON record 的 {field} 以十进制小数原样写入 {path} 的 {field}。"
    expected = original.replace(f'"{field}":0', f'"{field}":{value}')
    if name == "multi":
        task += f"把 record 的 label 以字符串原样写入 {path} 的 label。"
        expected = expected.replace('"label":"unknown"', '"label":"000783"')
    elif name == "string":
        task = f"打开 {URL}，把网页 JSON record 的 label 原样写入 {path} 的 label，保留前导零。"
        expected = original.replace('"label":"unknown"', '"label":"000783"')
    elif name not in {"decimal", "precision", "renamed"}:
        raise ValueError("unknown typed copy case")
    return CopyCase(path, task + "保留其他字段，不运行测试。", html, original, expected)


def run_copy(directory: Path, target: Target, name: str) -> JsonObject:
    case = copy_case(name)
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    file = project / case.path
    file.write_text(case.original, encoding="utf-8", newline="")

    def route(_provider: ResumableBrowserSearchProvider, request: RouteDecision) -> None:
        allowed = request.request.url == URL
        request.fulfill(status=200 if allowed else 404, body=case.html if allowed else "",
                        content_type="text/html; charset=utf-8")

    def emit(event: dict[str, object]) -> None:
        append_json(directory / "events.jsonl", event)

    def unavailable_review(*_args: object, **_kwargs: object) -> None:
        return None

    recorder = ExperimentProvider(target, directory, fixed_acquisition=False)
    browser = ResumableBrowserSearchProvider(headless=True)

    def search_factory() -> ResumableBrowserSearchProvider:
        return browser

    def connect(*_args: object, **_kwargs: object) -> ExperimentProvider:
        return recorder

    request = HeadlessRequest(project, case.task, intent="hybrid", provider_id="local", max_turns=8,
        state_home=directory / "state", research_store_root=directory / "knowledge", project_changes_required=True,
        requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect"),
        model_selection={"model": target.model, "base_url": target.base_url})
    write_json(directory / "request.json", {"task": case.task, "model": target.model, "case": name})
    available: dict[str, bool] = {"local": True}
    available_tabs: dict[str, bool] = {}
    try:
        with patch.object(ResumableBrowserSearchProvider, "_guard_request", route), \
                patch("codey.research.search_factory.default_research_search_provider", search_factory), \
                patch("codey.operations.task_phases.dispatch.default_research_search_provider", search_factory), \
                patch("codey.app.provider_services.provider_availability", return_value=available), \
                patch("codey.app.provider_services.provider_tab_availability", return_value=available_tabs), \
                patch("codey.app.review_service.run_review", unavailable_review):
            result = run_headless(request, emit_jsonl=emit, connect_provider=connect)
    finally:
        recorder.close()
        browser.close()
    content = file.read_text(encoding="utf-8")
    receipts = tool_receipts(directory / "state")
    actions = response_actions(directory / "provider.jsonl")
    edits = [row.result for row in receipts if row.result.call.name == "edit"]
    row: JsonObject = {"case": name, "passed": result.stop_reason == "done" and content == case.expected,
        "stop_reason": result.stop_reason, "content": content, "expected": case.expected,
        "successful_edits": sum(edit.ok for edit in edits),
        "source_captures": sum(row.result.ok and row.result.call.name == "browser_eval" for row in receipts),
        "model_javascript_proposals": sum(action.get("tool") == "codemode" and isinstance(args := action.get("args"), dict)
                                           and "code" in args for action in actions)}
    write_json(directory / "summary.json", row)
    return row


class Options(argparse.Namespace):
    run_dir: Path
    kobold_base_url: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Options())
    args.run_dir.mkdir(parents=True, exist_ok=False)
    target = select_target(180, args.kobold_base_url)
    before = production_source_digest()
    rows = [run_copy(args.run_dir / name, target, name) for name in ("decimal", "precision", "string", "multi", "renamed")]
    write_json(args.run_dir / "summary.json", {"source_before": before, "source_after": production_source_digest(), "cases": rows})
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
