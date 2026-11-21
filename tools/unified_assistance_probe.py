"""Natural model decisions around a simulated human login on the same durable task."""
from __future__ import annotations

import argparse
import json
import traceback
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from codey.app.headless_runner import HeadlessAppContext, HeadlessRequest, _run_headless_task
from codey.app.task_services import build_task_deps
from codey.automation.browser_contracts import RouteDecision
from codey.durable.questions import DurableUserQuestionStore
from codey.operations.task_entry import run_task_submission
from codey.task.model import ResumeRun, TaskSubmission
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string
from tools.browser_experiment_sources import ResumableBrowserSearchProvider
from tools.browser_production_live_gate import select_target
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target

URL = "https://example.com/assistance-search"
TASK = (f"请打开 {URL}，搜索福州的鱼丸记录，打开实际记录正文，告诉我净重和完整校验码并引用来源。"
        "如遇登录限制，请向我求助并保留原页面，等我处理后重新查看页面并继续；不要猜登录状态或记录内容。")
LOGIN = '<title>Records sign in</title><main>Sign in required to search records. Please ask the user to sign in.</main><input type=password aria-label=Password>'
SEARCH = '''<main><label>Search records<input aria-label="Search records"></label>
<button onclick="const link=document.createElement('a');link.href='https://example.com/assistance-record';
link.textContent='福州鱼丸记录';document.querySelector('output').replaceChildren(link)">Search</button>
<output>Enter a locality to search.</output></main>'''
RECORD = '<title>福州鱼丸记录</title><article>福州：鱼丸净重为 173 克。记录校验码 QUARTZ_851。</article>'


def run_assistance(directory: Path, target: Target) -> JsonObject:
    directory.mkdir(parents=True, exist_ok=False)
    recording = ExperimentProvider(target, directory, fixed_acquisition=False)
    source = ResumableBrowserSearchProvider(headless=True)
    events: list[JsonObject] = []
    assisted = False

    def emit(event: dict[str, object]) -> None:
        row = decode_json_object(event)
        events.append(row)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def route(request: RouteDecision) -> None:
        body = RECORD if request.request.url == "https://example.com/assistance-record" else SEARCH if assisted else LOGIN
        request.fulfill(content_type="text/html", body=body)

    def source_factory() -> ResumableBrowserSearchProvider:
        return source

    def connect(provider_id: str, **_options: object) -> ExperimentProvider:
        if provider_id != "local":
            raise ValueError("the frozen experiment may not switch models")
        return recording

    (directory / "request.json").write_text(json.dumps({"task": TASK, "model": target.model,
        "base_url": target.base_url, "seed": target.seed, "temperature": 0,
        "human_actor": "simulated fixture login; no real site or credentials", "max_turns": 14},
        ensure_ascii=False, indent=2), encoding="utf-8")
    state = HeadlessAppContext(directory / "state", port=0, emit_jsonl=emit, connect_provider=connect)
    summary: JsonObject = {"natural_task_closed": False, "human_actor": "simulated"}
    try:
        with patch.object(source, "_guard_request", route), patch(
            "codey.research.search_factory.default_research_search_provider", source_factory
        ), patch("codey.operations.task_phases.dispatch.default_research_search_provider", source_factory), patch(
            "codey.app.provider_services.provider_availability", return_value={"local": True}
        ):
            deps = build_task_deps(state, connect_provider=state.get_provider)
            request = TaskSubmission("assistance-session", None, TASK, 14, False, "local", intent="chat",
                run_id="assistance-run", sources_open_required=True,
                requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect"),
                model_selection={"model": target.model, "base_url": target.base_url})
            initial, error = _run_headless_task(state, request=HeadlessRequest(None, TASK, provider_id="local",
                intent="chat", max_turns=14, state_home=directory / "state", sources_open_required=True,
                requested_capabilities=request.requested_capabilities, model_selection=request.model_selection),
                session_id=request.session_id, project=None, pre_reserved_run_id="", agent_run=None,
                collect_changes=None, capture_provider_failure=None, connect_reviewer=None, review_policy="")
            if error is not None:
                raise error
            if initial is None:
                raise RuntimeError("headless admission returned no result")
            request = replace(request, run_id=initial.run_id)
            questions = DurableUserQuestionStore(state.durable)
            pending: Callable[[str], object] = questions.pending
            raw = pending(request.session_id)
            if raw is None:
                summary["failure"] = "model did not leave a pending assistance question"
            else:
                question = decode_json_object(raw)
                before = source.browser.resource_binding()
                document = source.browser.document_identity()
                before_receipts = len(tool_receipts(directory / "state"))
                assisted = True
                # The actor changes only the fixture's live DOM. The original page,
                # browser worker, authorization and task identity remain in place.
                changed = source.browser.evaluate("() => {document.body.innerHTML=" + json.dumps(SEARCH) + ";}")
                if not changed.ok:
                    raise RuntimeError(changed.model_text)
                free = question.get("allow_free_text") is True
                options = question.get("options")
                if not free and not isinstance(options, list):
                    raise ValueError("question has no answer options")
                answer = "已在原页面处理，请重新观察后继续。" if free else require_json_string(
                    options[0] if isinstance(options, list) else None, "answer option")
                accept: Callable[[str, int, str], object] = questions.answer
                accepted = decode_json_object(accept(require_json_string(question["id"], "question id"),
                    require_json_integer(question["revision"], "revision"), answer))
                summary["same_browser_binding"] = before == source.browser.resource_binding()
                (directory / "assistance.json").write_text(json.dumps({"question": question, "answer": accepted,
                    "binding_before": before, "binding_after_actor": source.browser.resource_binding(),
                    "document_before": document, "document_after_actor": source.browser.document_identity(),
                    "human_actor": "simulated fixture DOM change"}, ensure_ascii=False, indent=2), encoding="utf-8")
                run_task_submission(deps, replace(request, input=ResumeRun(request.run_id, answer), continue_task=True))
                resumed = tool_receipts(directory / "state")[before_receipts:]
                summary["resumed_snapshot"] = any(row.result.ok and row.result.call.name == "browser_snapshot" for row in resumed)
                summary["resumed_record_read"] = any(row.result.ok and row.result.call.name == "browser_read"
                    and "QUARTZ_851" in row.result.model_text for row in resumed)
            terminals = [event for event in events if event.get("type") == "task_done"]
            terminal: JsonObject = terminals[-1] if terminals else {}
            answer = str(terminal.get("summary") or "")
            summary.update(stop_reason=terminal.get("stop_reason"), answer=answer,
                one_durable_task=len(state.durable.tasks()) == 1,
                question_answered=assisted,
                natural_task_closed=(assisted and terminal.get("stop_reason") == "done"
                    and summary.get("same_browser_binding") is True and summary.get("resumed_snapshot") is True
                    and summary.get("resumed_record_read") is True
                    and "173" in answer and "QUARTZ_851" in answer))
            receipts = tool_receipts(directory / "state")
            summary["tools"] = [{"tool": row.result.call.name, "ok": row.result.ok,
                "ref": row.identity} for row in receipts]
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        (directory / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        state.close()
        source.close()
        recording.close()
    (directory / "natural-verdict.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


class Options(argparse.Namespace):
    directory: Path
    base_url: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    options = parser.parse_args(namespace=Options())
    print(json.dumps(run_assistance(options.directory, select_target(180, options.base_url)), ensure_ascii=False))


if __name__ == "__main__":
    main()
