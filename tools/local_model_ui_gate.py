"""Opt-in desktop/browser smoke gate for a real local OpenAI-compatible model.

The deterministic ``tools.ui_e2e`` flow owns UI behavior with scripted
providers.  This gate covers the missing integration boundary: the same
browser UI, the real Codey server, and ``provider_services.connect_provider``
for the configured local model.  It deliberately asserts transport and DOM
state rather than the model's wording.

Usage (after starting a local OpenAI-compatible server)::

    $env:LOCAL_OPENAI_BASE_URL = "http://127.0.0.1:1234/v1"
    $env:LOCAL_OPENAI_MODEL = "my-model"
    python tools/local_model_ui_gate.py --json

The gate is opt-in and never runs as part of the normal test suite.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, expect, sync_playwright

from codey.app import provider_services, sibling_probe
from codey.app import server as codey_server
from codey.app.context import AppContext
from codey.env_names import (
    LOCAL_OPENAI_API_KEY_ENV,
    LOCAL_OPENAI_BASE_URL_ENV,
    LOCAL_OPENAI_MODEL_ENV,
)
from codey.knowledge.store import KnowledgeStore
from codey.providers import controls as provider_controls
from codey.providers.catalog import PROVIDER_LABELS
from codey.providers.local_discovery import probe_local_endpoint_detail
from codey.providers.local_openai import LocalOpenAIProvider
from codey.providers.web_provider import WebChatProvider

UI_CASES = ("chat", "coding", "review", "research", "ghost")


def parse_cases(raw: str) -> tuple[str, ...]:
    values = tuple(item.strip().lower() for item in str(raw or "").split(",") if item.strip())
    if not values:
        return UI_CASES
    unknown = tuple(item for item in values if item not in UI_CASES)
    if unknown:
        raise ValueError(f"unsupported UI case(s): {', '.join(unknown)}")
    return tuple(dict.fromkeys(values))


def _preflight(provider_id: str = "local") -> tuple[str, str]:
    provider_id = str(provider_id or "local").strip().lower()
    if provider_id != "local":
        if provider_id not in PROVIDER_LABELS or provider_id == "local":
            raise RuntimeError(f"unsupported UI gate provider: {provider_id}")
        statuses = provider_services.provider_tab_availability()
        if not statuses.get(provider_id, False):
            raise RuntimeError(f"web provider {provider_id} is unavailable")
        return provider_id, PROVIDER_LABELS[provider_id]
    base_url = os.environ.get(LOCAL_OPENAI_BASE_URL_ENV, "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError(
            f"{LOCAL_OPENAI_BASE_URL_ENV} is required for the local-model UI gate"
        )
    api_key = os.environ.get(LOCAL_OPENAI_API_KEY_ENV, "").strip()
    endpoint, reason = probe_local_endpoint_detail(base_url, api_key=api_key, timeout=5)
    if endpoint is None:
        raise RuntimeError(f"local model /models probe failed for {base_url}: {reason}")
    configured_model = os.environ.get(LOCAL_OPENAI_MODEL_ENV, "").strip()
    if configured_model and endpoint.models and configured_model not in endpoint.models:
        raise RuntimeError(
            f"{LOCAL_OPENAI_MODEL_ENV}={configured_model!r} is not advertised by {base_url}/models"
        )
    model = configured_model or endpoint.default_model
    if not model:
        raise RuntimeError(
            f"{LOCAL_OPENAI_MODEL_ENV} is required when {base_url} returns no model id"
        )
    return endpoint.base_url, model


def _browser_launch_url(server: Any, base_url: str) -> str:
    """Use the server-issued operator bootstrap for a fresh browser context."""
    return str(server.launch_url(base_url))


def _terminal_succeeded(event: dict[str, Any]) -> bool:
    """Read the canonical task terminal field, not a UI-only alias."""
    return event.get("type") == "task_done" and str(event.get("stop_reason") or "") == "done"


def _terminal_belongs_to_run(event: dict[str, Any], run_id: str) -> bool:
    return bool(run_id) and str(event.get("run_id") or "") == run_id


def _start_unscoped_chat(page: Page) -> None:
    """Start a fresh chat with no project so Research stays a research run.

    The product intentionally inherits the active project when the user clicks
    New chat.  The research case must exercise the explicit research mode, so
    the gate creates the same session through the page's existing UI state
    helper with a null project id.
    """
    page.evaluate("newSession(null)")


def _research_gate_prompt() -> str:
    """Return only the user task; strict research guidance comes from production."""
    return (
        "Research the official Python pathlib documentation. First use web_search, then open_url "
        "on a result, and finish with done summarizing the source. Do not create or edit files."
    )


def _configure_research_store(state: Any, root: Path, cases: tuple[str, ...]) -> None:
    if "research" in cases:
        state.knowledge_store = KnowledgeStore(root / "vault")


def _wait_for_terminal(
    page: Page,
    base_url: str,
    *,
    previous_run_id: str = "",
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = page.request.get(base_url + "api/state").json()
        event = snapshot.get("last_terminal_event") or {}
        current_run_id = str(snapshot.get("run_id") or event.get("run_id") or "")
        if (
            not snapshot.get("busy")
            and event
            and (not previous_run_id or current_run_id != previous_run_id)
            and _terminal_belongs_to_run(event, current_run_id)
        ):
            return snapshot
        page.wait_for_timeout(100)
    raise AssertionError(f"local UI task did not reach a terminal state: {json.dumps(snapshot)}")


def _save_screenshot(page: Page, path: Path) -> str:
    try:
        page.screenshot(path=str(path), full_page=True, timeout=5_000)
        return str(path)
    except Exception as exc:  # pragma: no cover - browser-only failure path.
        marker = path.with_suffix(path.suffix + ".txt")
        marker.write_text(f"screenshot skipped: {type(exc).__name__}: {exc}", encoding="utf-8")
        return str(marker)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def analyze_provider_history(path: str | Path) -> dict[str, object]:
    """Classify repeated model terminal calls from a saved provider history.

    A rejected ``done`` is returned to the model as a tool error on the next
    request.  Counting that feedback separately makes the desktop gate output
    distinguish model retries from UI duplicate rendering or JSON decoding
    failures without changing the runtime completion contract.
    """
    rows: list[dict[str, Any]] = []
    parse_errors = 0
    history_path = Path(path)
    if history_path.is_file():
        for line in history_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                parse_errors += 1
                continue
            if isinstance(row, dict):
                rows.append(row)

    done_calls = 0
    completion_rejections = 0
    tool_calls: list[str] = []
    transport_errors = 0
    for row in rows:
        row_type = str(row.get("type") or "")
        if row_type == "error":
            transport_errors += 1
            continue
        if row_type == "response" and isinstance(row.get("text"), str):
            try:
                response_obj = json.loads(row["text"])
            except (TypeError, ValueError, json.JSONDecodeError):
                response_obj = None
            if isinstance(response_obj, dict):
                name = response_obj.get("tool")
                if isinstance(name, str) and name:
                    tool_calls.append(name)
                    if name == "done":
                        done_calls += 1
            continue
        if row_type != "response":
            if row_type == "request":
                content = str(row.get("text") or "")
                if "ERROR: Not done yet" in content:
                    completion_rejections += 1
                payload = row.get("payload")
                messages = payload.get("messages") if isinstance(payload, dict) else None
                # Provider requests replay the full conversation.  Only the
                # final tool message is newly delivered for this turn; walking
                # the whole list would count every prior rejection repeatedly.
                latest = messages[-1] if isinstance(messages, list) and messages else None
                if isinstance(latest, dict) and latest.get("role") == "tool":
                    content = str(latest.get("content") or "")
                    if "ERROR: Not done yet" in content:
                        completion_rejections += 1
            continue
        payload = row.get("payload")
        choices = payload.get("choices") if isinstance(payload, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        for call in calls if isinstance(calls, list) else ():
            function = call.get("function") if isinstance(call, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if not name:
                continue
            tool_calls.append(str(name))
            if str(name) == "done":
                done_calls += 1

    return {
        "rows": len(rows),
        "parse_errors": parse_errors,
        "transport_errors": transport_errors,
        "tool_calls": tuple(tool_calls),
        "done_calls": done_calls,
        "completion_rejections": completion_rejections,
        "done_retried_after_completion_rejection": done_calls > 1 and completion_rejections > 0,
    }


@contextmanager
def _record_local_provider_history(path: Path) -> Iterator[None]:
    """Record provider payloads for this gate without changing provider behavior."""
    original = LocalOpenAIProvider._post_chat
    lock = threading.Lock()
    # A gate artifact describes one run.  Do not mix old failures into the
    # current run's diagnosis when the same artifact directory is reused.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    def post_chat(
        self: LocalOpenAIProvider,
        messages: list[dict[str, Any]],
        tools: list[dict[str, object]] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload = self._request_payload(messages, tools)
        request_row = {
            "type": "request",
            "model": self.model,
            "payload": payload,
        }
        with lock:
            _append_jsonl(path, request_row)
        try:
            response = original(self, messages, tools, timeout=timeout)
        except Exception as exc:
            with lock:
                _append_jsonl(path, {
                    "type": "error",
                    "model": self.model,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            raise
        with lock:
            _append_jsonl(path, {"type": "response", "model": self.model, "payload": response})
        return response

    LocalOpenAIProvider._post_chat = post_chat  # type: ignore[method-assign]  # noqa: B010
    try:
        yield
    finally:
        LocalOpenAIProvider._post_chat = original  # type: ignore[method-assign]  # noqa: B010


@contextmanager
def _record_web_provider_history(path: Path) -> Iterator[None]:
    """Record the real web provider conversation without changing its driver."""
    original_send = WebChatProvider.send
    original_new_chat = WebChatProvider.new_chat
    lock = threading.Lock()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)

    def new_chat(self: WebChatProvider, timeout: float | None = None) -> None:
        with lock:
            _append_jsonl(path, {"type": "new_chat", "provider": self.spec.provider_id})
        return original_new_chat(self, timeout)

    def send(self: WebChatProvider, text: str, timeout: float | None = None) -> str:
        with lock:
            _append_jsonl(path, {
                "type": "request",
                "provider": self.spec.provider_id,
                "text": text,
            })
        try:
            response = original_send(self, text, timeout)
        except Exception as exc:
            with lock:
                _append_jsonl(path, {
                    "type": "error",
                    "provider": self.spec.provider_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            raise
        with lock:
            _append_jsonl(path, {
                "type": "response",
                "provider": self.spec.provider_id,
                "text": response,
            })
        return response

    WebChatProvider.new_chat = new_chat  # type: ignore[method-assign]  # noqa: B010
    WebChatProvider.send = send  # type: ignore[method-assign]  # noqa: B010
    try:
        yield
    finally:
        WebChatProvider.new_chat = original_new_chat  # type: ignore[method-assign]  # noqa: B010
        WebChatProvider.send = original_send  # type: ignore[method-assign]  # noqa: B010


def _send_task(page: Page, base_url: str, prompt: str) -> dict[str, Any]:
    before = page.request.get(base_url + "api/state").json()
    previous_run_id = str(before.get("run_id") or "")
    page.locator("#task").fill(prompt)
    page.locator("#send").click()
    expect(page.locator("#stop")).to_be_visible(timeout=5_000)
    terminal = _wait_for_terminal(page, base_url, previous_run_id=previous_run_id)
    event = terminal.get("last_terminal_event") or {}
    if not _terminal_succeeded(event):
        raise AssertionError(f"local UI task did not complete successfully: {json.dumps(event)}")
    expect(page.locator("#stop")).to_be_hidden(timeout=5_000)
    return event


def _open_changes_and_details(page: Page) -> list[str]:
    checks: list[str] = []
    view_diff = page.get_by_role("button", name="View diff", exact=True).last
    expect(view_diff).to_be_visible(timeout=10_000)
    view_diff.click()
    changes_drawer = page.locator("#changes-drawer")
    expect(changes_drawer).to_have_attribute("aria-hidden", "false")
    expect(changes_drawer.locator("#changes-body")).to_be_visible()
    checks.append("coding receipt opens Changes drawer")
    page.locator("#changes-close").click()
    expect(changes_drawer).to_have_attribute("aria-hidden", "true")

    details = page.get_by_role("button", name="Details", exact=True).last
    expect(details).to_be_visible(timeout=10_000)
    details.click()
    expect(page.locator(".run-details").last).to_be_visible(timeout=10_000)
    expect(page.locator(".run-details").last).to_contain_text("Status")
    checks.append("coding receipt opens Review/details panel")
    return checks


def _open_local_context(page: Page) -> None:
    page.locator("#topbar-more").click()
    page.locator('#top-menu button[data-act="local-context"]').click()
    local_drawer = page.locator("#local-context-drawer")
    expect(local_drawer).to_have_attribute("aria-hidden", "false")
    expect(local_drawer.locator("#local-context-body")).to_be_visible()


def _exercise_page(
    page: Page,
    base_url: str,
    project: Path,
    artifacts: Path,
    *,
    launch_url: str,
    cases: tuple[str, ...],
    provider_id: str,
) -> dict[str, Any]:
    page.goto(launch_url, wait_until="domcontentloaded")
    expect(page.locator("#provider-button")).to_be_enabled(timeout=15_000)

    # Selecting Local is a real browser action.  It also opens the config popover;
    # closing it verifies the local setup path without writing user state.
    page.locator("#provider-button").click()
    local_item = page.locator(f'[data-provider="{provider_id}"]')
    expect(local_item).to_be_visible()
    local_item.click()
    expect(page.locator("#provider-name")).to_have_text(PROVIDER_LABELS[provider_id])
    expect(page.locator("#provider-dot")).to_have_class(re.compile(r"\bok\b"), timeout=15_000)
    if provider_id == "local":
        expect(page.locator("#local-config-pop")).to_have_attribute("aria-hidden", "false")
        page.locator("#local-config-close").click()
        expect(page.locator("#local-config-pop")).to_have_attribute("aria-hidden", "true")

    checks: list[str] = []
    event: dict[str, Any] = {}
    if "chat" in cases:
        event = _send_task(page, base_url, "Reply with the short phrase: local desktop smoke passed.")
        checks.append("real local chat reaches done")
    if "chat" in cases:
        expect(page.locator(".msg.asst .body")).to_have_count(1, timeout=5_000)

    if "coding" in cases or "review" in cases:
        # The picker is exercised through the real HTTP route; only its native
        # dialog result is patched to a temporary folder.
        page.locator("#btn-add-project").click()
        expect(page.locator("#composer-context")).to_contain_text(project.name)
        checks.append("project picker and composer context")
        coding_prompt = (
            "Create math_utils.py with add(a, b) returning a + b. Create tests/__init__.py "
            "and tests/test_math_utils.py with a unittest for add(2, 3) == 5. Use edit with "
            "content to create the files, do not use shell commands, run python -m unittest discover, "
            "and finish with done."
        )
        event = _send_task(page, base_url, coding_prompt)
        if not event.get("changed"):
            raise AssertionError(f"coding task finished without changed receipt: {json.dumps(event)}")
        if not (project / "math_utils.py").is_file():
            raise AssertionError("coding task did not create math_utils.py")
        checks.append("real local coding reaches changed done")
        if "review" in cases:
            checks.extend(_open_changes_and_details(page))

    if "research" in cases:
        _start_unscoped_chat(page)
        research_toggle = page.locator("#ctx-research")
        research_toggle.click()
        expect(research_toggle).to_have_attribute("aria-pressed", "true")
        event = _send_task(
            page,
            base_url,
            _research_gate_prompt(),
        )
        if event.get("mode") != "research":
            raise AssertionError(f"research UI task used unexpected mode: {json.dumps(event)}")
        research_receipt = page.locator(".sr-prefix", has_text="Research").last
        expect(research_receipt).to_be_visible(timeout=10_000)
        open_research = research_receipt.locator("xpath=..").get_by_role("button", name="Open", exact=True)
        expect(open_research).to_be_visible()
        open_research.click()
        research_drawer = page.locator("#research-drawer")
        expect(research_drawer).to_have_attribute("aria-hidden", "false")
        expect(research_drawer.locator(".research-tab")).to_have_count(4)
        for tab in ("Evidence", "Sources", "Graph", "Notes"):
            research_drawer.get_by_role("button", name=tab, exact=True).click()
        page.locator("#research-close").click()
        expect(research_drawer).to_have_attribute("aria-hidden", "true")
        checks.append("research receipt opens all research drawer tabs")

    if "ghost" in cases:
        _open_local_context(page)
        local_drawer = page.locator("#local-context-drawer")
        expect(local_drawer).to_have_attribute("aria-hidden", "false")
        # Opening Changes while Local context is open verifies backend-backed
        # drawer exclusivity without fabricating a second UI state.
        page.evaluate("(project) => openChangesDrawer(project)", str(project))
        changes_drawer = page.locator("#changes-drawer")
        expect(changes_drawer).to_have_attribute("aria-hidden", "false")
        expect(local_drawer).to_have_attribute("aria-hidden", "true")
        page.locator("#changes-refresh").click()
        expect(changes_drawer.locator("#changes-body")).to_be_visible()
        page.locator("#changes-close").click()
        expect(changes_drawer).to_have_attribute("aria-hidden", "true")
        _open_local_context(page)
        page.locator("#local-context-close").click()
        expect(local_drawer).to_have_attribute("aria-hidden", "true")
        checks.append("ghost/local context and Changes drawer mutual exclusion")

    screenshot = artifacts / "local-model-ui.png"
    page.screenshot(path=str(screenshot), full_page=True)
    return {
        "ok": True,
        "checks": [
            f"real {provider_id} provider selected in browser",
            *(["local config popover open and close"] if provider_id == "local" else []),
            *checks,
        ],
        "screenshot": str(screenshot),
        "terminal_reason": event.get("stop_reason"),
    }


def run_local_model_ui_gate(
    *,
    headed: bool = False,
    artifacts: str | Path | None = None,
    cases: str = ",".join(UI_CASES),
) -> dict[str, Any]:
    provider_id = str(os.environ.get("UI_GATE_PROVIDER", "local") or "local").strip().lower()
    base_url, model = _preflight(provider_id)
    selected_cases = parse_cases(cases)
    artifact_dir = Path(artifacts or ".e2e-artifacts").resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="codey-local-ui-gate-")).resolve()
    history_path = artifact_dir / "local-model-provider-history.jsonl"
    project = temp_root / "project"
    project.mkdir()
    original_state = codey_server.STATE
    original_pick_folder = codey_server.pick_folder
    httpd: codey_server.CodeyHTTPServer | None = None
    page_errors: list[str] = []
    try:
        state = AppContext(temp_root / "state")
        _configure_research_store(state, temp_root, selected_cases)
        codey_server.STATE = state
        provider_controls.set_teach_handler(
            functools.partial(sibling_probe.handle_control_teach, codey_server.STATE)
        )
        codey_server.pick_folder = lambda mode="open", initial=None: str(project)
        provider_services.reset_provider_availability_cache()
        httpd = codey_server.CodeyHTTPServer(("127.0.0.1", 0), codey_server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        address = httpd.server_address
        host = str(address[0])
        port = int(address[1])
        server_url = f"http://{host}:{port}/"
        recorder = (
            _record_local_provider_history(history_path)
            if provider_id == "local"
            else _record_web_provider_history(history_path)
        )
        with recorder, sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=not headed)
                context = browser.new_context(viewport={"width": 1380, "height": 900})
                page = context.new_page()
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                try:
                    result = _exercise_page(
                        page,
                        server_url,
                        project,
                        artifact_dir,
                        launch_url=_browser_launch_url(httpd, server_url),
                        cases=selected_cases,
                        provider_id=provider_id,
                    )
                except Exception as exc:
                    failure_artifact = _save_screenshot(page, artifact_dir / "local-model-ui-failure.png")
                    if hasattr(exc, "add_note"):
                        exc.add_note(f"UI failure artifact: {failure_artifact}")
                    raise
                finally:
                    context.close()
                    browser.close()
        if page_errors:
            raise AssertionError("browser page errors: " + "; ".join(page_errors))
        result.update({
            "base_url": base_url,
            "model": model,
            "provider": provider_id,
            "server_url": server_url,
            "cases": list(selected_cases),
            "provider_history": str(history_path),
            "provider_history_analysis": analyze_provider_history(history_path),
        })
        return result
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if "state" in locals():
            state.close()
        codey_server.STATE = original_state
        codey_server.pick_folder = original_pick_folder
        provider_services.reset_provider_availability_cache()
        # Keep failed runs for model/UI diagnosis. Successful runs are cleaned
        # after copying the durable state into the artifact directory.
        if 'result' in locals() and isinstance(result, dict) and result.get("ok"):
            shutil.rmtree(temp_root, ignore_errors=True)
        else:
            preserved = artifact_dir / "local-model-ui-state"
            if preserved.exists():
                shutil.rmtree(preserved, ignore_errors=True)
            shutil.copytree(temp_root, preserved, dirs_exist_ok=True)
            shutil.rmtree(temp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--artifacts", default=".e2e-artifacts")
    parser.add_argument("--cases", default=",".join(UI_CASES))
    parser.add_argument("--provider", default=None, choices=tuple(PROVIDER_LABELS))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.provider:
            os.environ["UI_GATE_PROVIDER"] = args.provider
        data = run_local_model_ui_gate(
            headed=args.headed,
            artifacts=args.artifacts,
            cases=args.cases,
        )
    except Exception as exc:
        data = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print("PASS" if data["ok"] else f"FAIL: {data.get('error', '')}")
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
