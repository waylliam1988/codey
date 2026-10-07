"""Exercise recovery/search in an isolated, visible Codey WebView2 window."""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import webview
from playwright.sync_api import expect, sync_playwright

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app import server

ARTIFACTS = Path(__file__).resolve().parents[1] / ".e2e-artifacts/recovery-search-2026-10-07"
LOCAL = {
    "connected": True, "base_url": "http://127.0.0.1:5001/v1",
    "model": "koboldcpp/Gemma4-12B-Q4", "display_name": "Gemma4 12B",
    "models": ["koboldcpp/Gemma4-12B-Q4", "second-model"],
    "context": {"context_window_tokens": 262144}, "native_tools_mode": "auto",
    "has_api_key": True, "thinking_options": ["off", "minimal", "low", "medium", "high"],
}


def exercise(page, url, window):
    checks, errors, submissions = [], [], []
    mode = {"load_failures": 0, "save_failure": False, "run": "worker", "owner": ""}
    state = {"active_id": "a", "projects": [
        {"id": "empty", "name": "alpha", "path": "E:/alpha", "expanded": False},
        {"id": "beta", "name": "beta-project", "path": "E:/beta", "expanded": False},
    ], "sessions": [
        {"id": "a", "title": "Alpha chat", "provider": "deepseek", "messages": []},
        {"id": "b", "title": "Fix failing test", "projectId": "beta", "provider": "mimo", "messages": []},
    ]}
    catalog = [{"id": key, "label": label, "available": True} for key, label in
               [("deepseek", "DeepSeek"), ("mimo", "MiMo"), ("stepfun", "StepFun"),
                ("qwen", "Qwen"), ("glm", "GLM"), ("local", "Local")]]

    def api(route):
        path = route.request.url.split("/api/")[-1].split("?")[0]
        data, status = {"ok": True}, 200
        if path in {"provider_catalog", "providers"}:
            data = {"providers": catalog, "default": "deepseek"}
        elif path == "ui_state" and route.request.method == "GET":
            data = {"state": state}
        elif path == "local_provider":
            if route.request.method == "GET" and mode["load_failures"]:
                mode["load_failures"] -= 1
                status, data = 503, {"error": "simulated connection load failure"}
            elif route.request.method == "POST" and mode["save_failure"]:
                status, data = 400, {"ok": False, "error": "Model is unavailable"}
            else:
                data = {"ok": True, "local": LOCAL}
        elif path == "state":
            data = {"busy": bool(mode["owner"]), "session_id": mode["owner"],
                    "run_id": "review-run" if mode["owner"] else "",
                    "run_status": "running" if mode["owner"] else "idle"}
        elif path == "run":
            submissions.append(route.request.post_data_json)
            status, data = {
                "worker": (503, {"error": "browser worker busy", "hint": "retry"}),
                "busy": (409, {"error": "busy"}),
                "local": (400, {"reason": "local_selection_invalid", "error": "private detail"}),
                "unknown": (500, {"error": "private detail"}),
            }[mode["run"]]
        route.fulfill(status=status, json=data)

    def shot(name):
        page.screenshot(path=str(ARTIFACTS / f"after-{name}.png"))

    def check(description):
        checks.append(description)
        print(description, flush=True)

    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script("window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")
    page.route("**/api/**", api)
    page.goto(url)
    expect(page.locator("#provider-button")).to_be_enabled(timeout=15000)
    page.wait_for_function("CodeyUiState.current().active_id === 'a'")

    # Failed load, repeated keyboard Retry, then edit and failed save.
    mode["load_failures"] = 2
    page.locator("#btn-settings").click()
    expect(page.locator("#local-config-summary")).to_have_text("Could not load connection")
    expect(page.locator("#local-base-url")).to_be_disabled()
    shot("settings-failure")
    page.locator("#local-config-retry").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#local-config-retry")).to_be_enabled()
    expect(page.locator("#local-config-retry")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#local-base-url")).to_have_value(LOCAL["base_url"])
    expect(page.locator("#local-base-url")).to_be_focused()
    shot("settings-recovered")
    check("Settings: two failures then in-place keyboard recovery, focus reaches Address")
    page.locator("#local-model-name").fill("edited-model")
    mode["save_failure"] = True
    page.locator("#local-config-save").click()
    expect(page.locator("#local-config-error")).to_have_text("Model is unavailable")
    expect(page.locator("#local-model-name")).to_have_value("edited-model")
    expect(page.locator("#local-config-retry")).to_be_hidden()
    page.keyboard.press("Escape")
    expect(page.locator("#btn-settings")).to_be_focused()
    check("Settings: save failure preserves edits; Escape returns to its original trigger")

    # Pointer opens the existing search row; Escape restores saved expansion.
    page.locator("#chat-search-toggle").click()
    search = page.locator("#chat-search")
    search.fill("ALPHA")
    expect(page.locator(".project-main")).to_have_text("alpha")
    expect(page.locator(".project-children")).to_have_text("No chats")
    expect(page.locator(".project-toggle path")).to_have_attribute("d", "m6 9 6 6 6-6")
    clear = page.get_by_role("button", name="Clear search", exact=True)
    page.mouse.move(500, 300)
    assert clear.evaluate("e => getComputedStyle(e).color") == "rgb(160, 160, 160)"
    shot("search-clear-default")
    clear.hover()
    assert clear.evaluate("e => getComputedStyle(e).color") == "rgb(230, 230, 230)"
    shot("search-clear-hover")
    clear.click()
    expect(search).to_have_value("")
    expect(search).to_be_focused()
    expect(clear).to_be_hidden()
    shot("search-clear-cleared")
    search.fill("alpha")
    search.press("Tab")
    expect(clear).to_be_focused()
    clear.press("Enter")
    expect(search).to_be_focused()
    expect(search).to_have_value("")
    search.fill("ALPHA")
    page.mouse.move(500, 300)
    check("Search clear: neutral outline default/hover, pointer and keyboard clearing retain input focus")
    shot("empty-project-search")
    search.fill("zz-no-such-name")
    expect(page.locator("#search-empty")).to_be_visible()
    expect(page.locator("#session-list")).to_be_hidden()
    assert page.locator("#btn-settings").bounding_box()["y"] > page.evaluate("innerHeight") - 55
    shot("no-matches")
    search.fill("failing")
    expect(page.locator(".session-title")).to_have_text("Fix failing test")
    search.fill("beta-project")
    expect(page.locator(".session-title")).to_have_text("Fix failing test")
    search.press("Escape")
    expect(page.locator("#chat-search-toggle")).to_be_focused()
    expect(page.locator(".project-children")).to_have_count(0)
    page.locator("#chat-search-toggle").click()
    search.press("Tab")
    expect(search).to_be_hidden()
    check("Search: empty project, absent name, chat title, populated project, Escape and empty blur")

    # Retry the original rejected text while a later draft remains intact.
    page.locator("#task").fill("original task")
    page.keyboard.press("Enter")
    expect(page.locator(".status-row.err").last).to_contain_text("Codey is temporarily busy")
    page.locator("#task").fill("later alpha draft")
    page.locator(".status-row.err").last.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator(".status-row.err")).to_have_count(2)
    assert submissions[-1]["task"] == "original task"
    expect(page.locator("#task")).to_have_value("later alpha draft")
    shot("worker-retry-draft")
    check("Send: Retry submits original text and preserves the later draft")

    mode.update(run="busy", owner="b")
    page.locator("#send").click()
    error = page.locator(".status-row.err").last
    expect(error).to_contain_text("Another chat is running")
    shot("busy-open")
    error.get_by_role("button", name="Open", exact=True).click()
    expect(page.locator("#sess-title")).to_contain_text("Fix failing test")
    page.locator("#task").fill("beta draft")
    page.evaluate("switchSession('a')")
    expect(page.locator("#task")).to_have_value("later alpha draft")
    check("Send: Open reaches the verified owner, both chats retain independent drafts")
    mode["owner"] = ""
    page.evaluate("CodeySse.reconcileRunState()")

    page.locator("#provider-button").click()
    page.locator('.provider-item[data-provider="local"]').first.click()
    mode["run"] = "local"
    page.locator("#send").click()
    error = page.locator(".status-row.err").last
    expect(error).to_contain_text("Select the local model again")
    before = len(submissions)
    error.get_by_role("button", name="Choose model", exact=True).click()
    expect(page.locator("#provider-menu")).to_be_visible()
    shot("choose-model")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("#provider-menu")).to_be_hidden()
    assert len(submissions) == before
    expect(page.locator("#task")).to_have_value("later alpha draft")
    mode["run"] = "unknown"
    page.locator("#send").click()
    expect(page.locator(".status-row.err").last).to_contain_text("Could not send the message")
    expect(page.locator(".status-row.err").last).not_to_contain_text("private detail")
    check("Send: Choose model reuses the keyboard menu without sending; unknown failure remains generic")

    # Resize the actual native window, not just the browser viewport.
    window.resize(500, 540)
    page.wait_for_function("innerWidth <= 500")
    page.keyboard.press("Control+,")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_visible()
    box = page.locator("#local-config-pop").bounding_box()
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= page.evaluate("innerWidth")
    assert box["y"] + box["height"] <= page.evaluate("innerHeight")
    shot("narrow-settings")
    page.keyboard.press("Escape")
    page.locator("#show-sidebar").click()
    page.locator("#chat-search-toggle").click()
    search.fill("alpha")
    expect(page.locator(".project-main")).to_have_text("alpha")
    shot("narrow-search")
    check("Native 500×540 window: Settings fits; sidebar search remains operable")
    assert not errors, errors
    return {"host": "pywebview / Edge WebView2", "isolated_state": True,
            "simulated_api_failures": True, "checks": checks, "javascript_errors": errors,
            "submissions": len(submissions)}


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codey-ui-review-") as temporary:
        ctx = server.AppContext(Path(temporary) / "state")
        server.STATE = ctx
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            debug_port = sock.getsockname()[1]
        webview.settings["REMOTE_DEBUGGING_PORT"] = debug_port
        window = webview.create_window("Codey — isolated UI review", html="<html><body>Starting Codey review…</body></html>",
                                       width=1380, height=900, text_select=True)
        result = {}

        def drive():
            try:
                window.events.loaded.wait(30)
                with sync_playwright() as pw:
                    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}", timeout=30000)
                    page = browser.contexts[0].pages[0]
                    result.update(exercise(page, httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"), window))
                    result["passed"] = True
            except Exception:
                result.update(passed=False, traceback=traceback.format_exc())
                print(result["traceback"], flush=True)
            finally:
                (ARTIFACTS / "native-review.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                window.destroy()

        try:
            webview.start(drive, gui="edgechromium", private_mode=True, storage_path=str(Path(temporary) / "webview"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            ctx.close()
            server.STATE = None
        return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
