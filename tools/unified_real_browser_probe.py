"""Natural live-site tasks through the production entry, with immutable run records."""
from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.research.browser_search import BrowserSearchProvider
from codey.types.json_value import JsonObject, canonical_json_object
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target


def run_live(directory: Path, task: str, target: Target, *, max_turns: int = 16) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    source = BrowserSearchProvider(headless=True)
    recording = ExperimentProvider(target, directory, fixed_acquisition=False)
    events: list[JsonObject] = []

    def observe(event: dict[str, object]) -> None:
        row = canonical_json_object(event)
        events.append(row)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, *, port: int) -> ExperimentProvider:
        if provider_id != "local":
            raise ValueError("live probe requires the selected local model")
        return recording

    def source_factory() -> BrowserSearchProvider:
        return source

    request = HeadlessRequest(None, task, intent="research", provider_id="local", max_turns=max_turns,
        strict_research=True, project_changes_required=False,
        requested_capabilities=("web.read", "browser.observe", "browser.interact"),
        state_home=directory / "state", research_store_root=directory / "knowledge",
        model_selection={"model": target.model, "base_url": target.base_url})
    manifest: JsonObject = {"task": task, "model": target.model, "seed": target.seed,
        "temperature": 0, "max_turns": max_turns, "required_tools": [],
        "scope": "real_site_production_entry_no_page_interception_no_scripted_actions"}
    (directory / "request.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    started = time.monotonic()
    summary: JsonObject = dict(manifest)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}))
            stack.enter_context(patch("codey.app.provider_services.provider_availability", return_value={"local": True}))
            stack.enter_context(patch("codey.research.search_factory.default_research_search_provider", source_factory))
            stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", source_factory))
            result = run_headless(request, emit_jsonl=observe, connect_provider=connect)
        terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
        summary.update(stop_reason=result.stop_reason, answer=terminal.get("summary", ""))
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        (directory / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        source.close()
        recording.close()
    receipts = tool_receipts(directory / "state")
    summary["receipts"] = [{"identity": row.identity, "tool": row.result.call.name,
        "args": row.result.call.args, "ok": row.result.ok, "text": row.result.model_text,
        "canonical": canonical_json_object(row.result.canonical), "audit": canonical_json_object(row.result.audit)}
        for row in receipts]
    summary["seconds"] = round(time.monotonic() - started, 3)
    summary["quality_verdict"] = "Review original sources, requested behavior and report; done alone is insufficient."
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


class Options(argparse.Namespace):
    directory: Path
    task: str
    base_url: str
    max_turns: int


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--max-turns", type=int, default=16)
    options = parser.parse_args(namespace=Options())
    summary = run_live(options.directory, options.task, select_target(180, options.base_url), max_turns=options.max_turns)
    print(json.dumps({key: value for key, value in summary.items() if key != "receipts"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
