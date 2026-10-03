from __future__ import annotations

import argparse
import functools
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.app import provider_services, sibling_probe
from codey.app import server as codey_server
from codey.providers import controls as provider_controls
from codey.runtime.core import cancellation

TASK = (
    "Create result.txt containing exactly 'browser e2e passed' and run the tests. "
    "Finish only after the tests pass."
)


def _close_event_stream(page: Page) -> None:
    page.wait_for_function("window.__codeyE2eStreams && window.__codeyE2eStreams.length > 0")
    page.evaluate("window.__codeyE2eStreams.forEach(stream => stream.close())")


class ScriptedWriter:
    name = "Browser E2E Writer"
    location = "scripted://writer"

    def __init__(self) -> None:
        self.reload_entered = threading.Event()
        self.reload_release = threading.Event()

    def new_chat(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        if "Decide how to handle the user request below in one step." in text:
            auto_prompt = text.partition("Decide how to handle the user request below in one step.")[2]
            # This fixture's requests are single lines. Prior focus and appended
            # experience text must not select a different scripted response.
            request = auto_prompt.partition("\nUser request:\n")[2].partition("\n")[0]
            if "Explain box breathing without project access" in request:
                return "Box breathing uses equal inhale, hold, exhale, and hold phases."
            if "Discuss a breathing app without changing files" in request:
                return "Start with one guided breathing rhythm.\n\nAdd customization after the basic exercise feels calm."
            if "Create result.txt containing exactly" in request:
                return "ACTION: project\nPLAN: Create result.txt with the requested content and run the tests."
            if "Request a shell command" in request:
                return "ACTION: project\nPLAN: Request the shell command and wait for approval."
            if "Stay active across one UI reload" in request:
                self.reload_entered.set()
                deadline = time.monotonic() + 60
                while not self.reload_release.wait(0.1):
                    cancellation.check()
                    if time.monotonic() >= deadline:
                        raise AssertionError("browser did not release the reload probe")
                cancellation.check()
                return "reload completed"
            if "Finish while state reconciliation is delayed" in request:
                cancellation.wait(1.5)
                return "delayed state completed"
        if "Wait until stopped by the UI" in text:
            cancellation.wait(30)
            raise AssertionError("responsive stop did not cancel the provider wait")
        if "The user approved and ran this shell command" in text:
            return '{"tool":"done","args":{"summary":"approval continuation completed"}}'
        if "The user denied this shell command; it was not executed:" in text:
            return '{"tool":"done","args":{"summary":"denial continuation completed"}}'
        if "Request a shell command" in text:
            return (
                '{"tool":"shell","args":{"command":"git status --short",'
                '"path":"."}}'
            )
        if text.startswith("[result: edit]"):
            return '{"tool":"run","args":{"command":"python -m unittest","path":"."}}'
        if text.startswith("[result: run]"):
            return '{"tool":"done","args":{"summary":"browser flow completed"}}'
        if "Create result.txt containing exactly" in text:
            return (
                '{"tool":"edit","args":{"path":"result.txt",'
                '"content":"browser e2e passed"}}'
            )
        raise AssertionError("unrecognized browser fixture prompt: " + text[:120])

    def close(self) -> None:
        pass


class ScriptedReviewer:
    name = "Browser E2E Reviewer"
    location = "scripted://reviewer"

    def new_chat(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        if "private read-only project reviewer" in text:
            del timeout
            return (
                '{"tool":"done","args":{"summary":"Advisor note: inspect the simple '
                'breathing loop and keep the implementation focused."}}'
            )
        if "private read-only advisor" in text:
            del timeout
            return "Advisor note: keep the answer concise and practical."
        del text, timeout
        return '{"verdict":"approved","summary":"E2E change is correct","findings":[]}'

    def close(self) -> None:
        pass


def _make_project(root: Path) -> None:
    (root / "test_result.py").write_text(
        "import unittest\n"
        "from pathlib import Path\n\n"
        "class ResultTests(unittest.TestCase):\n"
        "    def test_result_file(self):\n"
        "        path = Path(__file__).with_name('result.txt')\n"
        "        self.assertEqual(path.read_text(encoding='utf-8'), 'browser e2e passed')\n",
        encoding="utf-8",
    )


def _wait_for_file(path: Path, *, exists: bool, page: Page, timeout_ms: int = 15_000) -> None:
    deadline = timeout_ms
    step = 100
    while deadline > 0:
        if path.exists() is exists:
            return
        page.wait_for_timeout(step)
        deadline -= step
    state = "exist" if exists else "be removed"
    raise AssertionError(f"expected {path.name} to {state}")


def _save_screenshot(page: Page, path: Path, *, full_page: bool = True) -> str:
    try:
        page.screenshot(path=str(path), full_page=full_page, timeout=5_000)
        return str(path)
    except Exception as exc:  # pragma: no cover - exercised with a fake page in unit tests.
        marker = path.with_suffix(path.suffix + ".txt")
        marker.write_text(
            f"screenshot skipped: {type(exc).__name__}: {exc}",
            encoding="utf-8",
        )
        return str(marker)


def _wait_for_continuation(page: Page, base_url: str, summary: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = page.request.get(base_url + "api/state").json()
        terminal = snapshot.get("last_terminal_event") or {}
        if not snapshot.get("busy") and terminal.get("summary") == summary:
            return
        page.wait_for_timeout(100)
    raise AssertionError("continuation did not finish: " + json.dumps(snapshot, ensure_ascii=False))


def _exercise_page(
    page: Page,
    base_url: str,
    project: Path,
    artifacts: Path,
    writer: ScriptedWriter,
) -> dict:
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#task").fill("Explain box breathing without project access.")
    page.locator("#send").click()
    expect(page.locator(".msg.asst .body")).to_contain_text(
        "Box breathing uses equal inhale",
        timeout=15_000,
    )
    expect(page.locator("#chat")).not_to_contain_text('{"tool"')

    expect(page.locator("#btn-add-project")).to_be_visible()
    page.locator("#btn-add-project").click()
    expect(page.locator("#composer-context")).to_contain_text(project.name)

    page.locator("#provider-button").click()
    glm = page.locator('[data-provider="glm"]')
    expect(glm).to_be_visible()
    glm.click()
    expect(page.locator("#provider-name")).to_have_text("GLM")
    page.locator("#provider-button").click()
    qwen = page.locator('[data-provider="qwen"]')
    expect(qwen).to_be_visible()
    qwen.click()
    expect(page.locator("#provider-name")).to_have_text("Qwen")

    done_prefixes = page.locator(".sr-prefix", has_text="Done")
    done_before_discussion = done_prefixes.count()
    page.locator("#task").fill("Discuss a breathing app without changing files.")
    page.locator("#send").click()
    expect(page.locator(".msg.asst .body").last).to_contain_text(
        "Start with one guided breathing rhythm",
        timeout=15_000,
    )
    expect(done_prefixes).to_have_count(done_before_discussion)
    expect(page.locator("#chat")).not_to_contain_text("No files changed")
    if (project / "result.txt").exists():
        raise AssertionError("project discussion unexpectedly changed a file")

    page.locator("#task").fill(TASK)
    page.locator("#send").click()
    expect(page.locator("#chat")).to_contain_text("Done", timeout=30_000)
    expect(
        page.locator(".msg.asst .body", has_text="browser flow completed")
    ).to_have_count(1)
    # The changed file is a document, so completion is honest-but-limited:
    # the receipt records the observed green run without claiming a verified
    # code change (0.4.13 provenance semantics).
    expect(page.locator("#chat")).to_contain_text("exit 0: python -m unittest")
    expect(page.locator("#chat")).not_to_contain_text("[Completion blocked:")
    expect(page.locator("#chat")).not_to_contain_text('{"tool"')
    expect(page.locator("#chat")).not_to_contain_text("[agent]")

    result_file = project / "result.txt"
    _wait_for_file(result_file, exists=True, page=page)
    if result_file.read_text(encoding="utf-8") != "browser e2e passed":
        raise AssertionError("result.txt has unexpected content")

    details = page.get_by_role("button", name="Details", exact=True).last
    expect(details).to_be_visible()
    state_before_details = page.evaluate("JSON.stringify(activeSession().messages)")
    details.click()
    panel = page.locator(".run-details").last
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("Run details")
    expect(panel).to_contain_text("Work")
    expect(panel).to_contain_text("Model")
    expect(panel).to_contain_text("Actions")
    expect(panel).to_contain_text("Safety")
    expect(panel).not_to_contain_text("Provider")
    expect(page.locator("#changes-drawer")).to_have_attribute("aria-hidden", "true")
    details_style = panel.evaluate(
        """el => {
          const style = getComputedStyle(el);
          return {
            backgroundColor: style.backgroundColor,
            borderRadius: style.borderRadius,
            boxShadow: style.boxShadow,
            maxWidth: style.maxWidth,
          };
        }"""
    )
    if details_style != {
        "backgroundColor": "rgba(0, 0, 0, 0)",
        "borderRadius": "0px",
        "boxShadow": "none",
        "maxWidth": "640px",
    }:
        raise AssertionError(f"unexpected run details style: {details_style}")
    details_screenshot = artifacts / "ui-run-details.png"
    details_screenshot_path = _save_screenshot(page, details_screenshot)
    details.click()
    expect(panel).to_be_hidden()
    if state_before_details != page.evaluate("JSON.stringify(activeSession().messages)"):
        raise AssertionError("Run Details changed persistent chat state")

    view_diff = page.get_by_role("button", name="View diff", exact=True)
    expect(view_diff).to_be_visible()
    view_diff.click()
    expect(page.locator("#changes-drawer")).to_have_attribute("aria-hidden", "false")
    expect(page.locator("#changes-body")).to_contain_text("result.txt")
    changed_file = page.locator(".change-file button")
    expect(changed_file).to_have_count(1)
    changed_file.click()
    expect(page.locator(".diff-line.add")).to_contain_text("browser e2e passed")
    done_screenshot = artifacts / "ui-done.png"
    done_screenshot_path = _save_screenshot(page, done_screenshot)

    restore = page.locator("#changes-restore")
    expect(restore).to_be_enabled()
    restore.click()
    _wait_for_file(result_file, exists=False, page=page)
    expect(page.locator("#changes-body")).to_contain_text("No changes")
    page.wait_for_timeout(500)
    restored_screenshot = artifacts / "ui-restored.png"
    restored_screenshot_path = _save_screenshot(page, restored_screenshot)

    page.locator("#changes-close").click()
    page.locator("#task").fill(
        "Request a shell command for git status --short and wait for approval."
    )
    page.locator("#send").click()
    expect(page.locator("#chat")).to_contain_text("Approval required")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#chat")).to_contain_text("Approval required")
    deny = page.get_by_role("button", name="Deny", exact=True)
    expect(deny).to_be_visible()
    _close_event_stream(page)
    deny.click()
    expect(page.locator("#chat")).to_contain_text("Denied")
    expect(page.locator("#chat")).to_contain_text("git status --short")
    _wait_for_continuation(page, base_url, "denial continuation completed")
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("button", name="Deny", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Allow", exact=True)).to_have_count(0)
    expect(page.locator(".shell-output")).to_have_count(1)

    def drop_approval_response(route) -> None:
        route.fetch()
        route.abort()

    page.locator("#task").fill(
        "Request a shell command for git status --short and wait for approval."
    )
    page.locator("#send").click()
    expect(page.locator("#chat")).to_contain_text("Approval required")
    deny = page.get_by_role("button", name="Deny", exact=True)
    expect(deny).to_be_visible()
    _close_event_stream(page)
    page.route("**/api/shell_approval", drop_approval_response)
    deny.click()
    expect(deny).to_be_enabled()
    page.unroute("**/api/shell_approval", drop_approval_response)
    _wait_for_continuation(page, base_url, "denial continuation completed")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".shell-output")).to_have_count(2)
    expect(page.get_by_role("button", name="Deny", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Allow", exact=True)).to_have_count(0)

    page.locator("#task").fill(
        "Request a shell command for git status --short and wait for approval."
    )
    page.locator("#send").click()
    expect(page.locator("#chat")).to_contain_text("Approval required")
    allow = page.get_by_role("button", name="Allow", exact=True)
    expect(allow).to_be_visible()
    _close_event_stream(page)
    page.route("**/api/shell_approval", drop_approval_response)
    allow.click()

    _wait_for_continuation(page, base_url, "approval continuation completed")

    page.unroute("**/api/shell_approval", drop_approval_response)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".shell-output")).to_have_count(3)
    expect(page.get_by_role("button", name="Deny", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Allow", exact=True)).to_have_count(0)
    restored_messages = page.evaluate(
        "() => activeSession().messages.map(m => ({type: m.type, approved: m.approved, text: m.text || ''}))"
    )
    executed_index = max(
        index
        for index, message in enumerate(restored_messages)
        if message["type"] == "shell_result" and message["approved"]
    )
    answer_index = max(
        index
        for index, message in enumerate(restored_messages)
        if message["type"] == "asst"
        and "approval continuation completed" in message["text"]
    )
    if executed_index >= answer_index:
        raise AssertionError("continued task result appeared before its shell execution")

    reload_answers = page.locator(".msg.asst .body", has_text="reload completed")
    reload_before = reload_answers.count()
    page.locator("#task").fill("Stay active across one UI reload.")
    page.locator("#send").click()
    expect(page.locator("#stop")).to_be_visible()
    if not writer.reload_entered.wait(5):
        raise AssertionError("reload probe did not enter the provider send")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#stop")).to_be_visible(timeout=3_000)
    expect(page.locator("#status")).to_contain_text("Running")
    writer.reload_release.set()
    expect(page.locator("#stop")).to_be_hidden(timeout=8_000)
    expect(reload_answers).to_have_count(reload_before + 1)
    page.wait_for_timeout(500)
    expect(reload_answers).to_have_count(reload_before + 1)

    delayed_answers = page.locator(".msg.asst .body", has_text="delayed state completed")
    delayed_before = delayed_answers.count()
    page.locator("#task").fill("Finish while state reconciliation is delayed.")
    page.locator("#send").click()
    expect(page.locator("#stop")).to_be_visible()

    def delay_state_response(route) -> None:
        response = route.fetch()
        time.sleep(2.5)
        route.fulfill(response=response)

    page.route("**/api/state", delay_state_response)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#stop")).to_be_hidden(timeout=6_000)
    expect(page.locator("#status")).not_to_contain_text("Running")
    expect(delayed_answers).to_have_count(delayed_before + 1)
    expect(page.locator("#provider-button")).to_be_enabled()
    page.unroute("**/api/state", delay_state_response)

    page.locator("#task").fill("Wait until stopped by the UI.")
    page.locator("#send").click()
    stop = page.locator("#stop")
    expect(stop).to_be_visible()
    stopped_at = time.monotonic()
    stop.click()
    expect(stop).to_be_hidden(timeout=3_000)
    if time.monotonic() - stopped_at >= 3.0:
        raise AssertionError("Stop did not cancel the provider wait within 3 seconds")
    expect(page.locator("#provider-button")).to_be_enabled()
    page.locator("#task").fill("ready after stop")
    expect(page.locator("#send")).to_be_enabled()

    return {
        "ok": True,
        "url": base_url,
        "checks": [
            "plain New Chat without project tools",
            "project picker",
            "provider selection",
            "project discussion without file changes",
            "project answer before changed receipt",
            "SSE task lifecycle",
            "agent edit and test",
            "task receipt",
            "run details inline receipt",
            "diff drawer",
            "snapshot restore",
            "shell approval denial",
            "shell approval reconnect recovery",
            "shell approval HTTP reconciliation",
            "shell result snapshot reconciliation",
            "shell result before continued task completion",
            "SSE reconnect reconciliation",
            "stale state cannot override newer SSE completion",
            "responsive stop",
        ],
        "screenshots": [
            details_screenshot_path,
            done_screenshot_path,
            restored_screenshot_path,
        ],
    }


def run_ui_e2e(*, headed: bool = False, artifacts: str | Path | None = None) -> dict:
    artifact_dir = Path(artifacts or ".e2e-artifacts").resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="codey-ui-e2e-")).resolve()
    project = temp_root / "project"
    project.mkdir()
    _make_project(project)

    original_state = codey_server.STATE
    original_connect_provider = provider_services.connect_provider
    original_connect_existing_provider = provider_services.connect_existing_provider
    original_connect_fresh_provider_tab = provider_services.connect_fresh_provider_tab
    original_provider_tab_availability = provider_services.provider_tab_availability
    original_pick_folder = codey_server.pick_folder
    writer = ScriptedWriter()
    httpd: codey_server.CodeyHTTPServer | None = None
    try:
        codey_server.STATE = codey_server.AppContext(temp_root / "state")
        provider_controls.set_teach_handler(
            functools.partial(sibling_probe.handle_control_teach, codey_server.STATE)
        )
        provider_services.connect_provider = lambda provider_id: writer
        provider_services.connect_fresh_provider_tab = lambda provider_id: ScriptedReviewer()
        provider_services.provider_tab_availability = lambda: {
            "deepseek": True,
            "mimo": True,
            "qwen": True,
            "stepfun": True,
            "glm": True,
        }
        provider_services.connect_existing_provider = lambda provider_id: ScriptedReviewer()
        codey_server.pick_folder = lambda mode="open", initial=None: str(project)

        httpd = codey_server.CodeyHTTPServer(("127.0.0.1", 0), codey_server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        base_url = f"http://{host}:{port}/"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=not headed)
            context = browser.new_context(viewport={"width": 1380, "height": 900})
            page = context.new_page()
            page.add_init_script("""
                const OriginalEventSource = window.EventSource;
                window.__codeyE2eStreams = [];
                window.EventSource = class extends OriginalEventSource {
                    constructor(...args) {
                        super(...args);
                        window.__codeyE2eStreams.push(this);
                    }
                };
            """)
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            try:
                result = _exercise_page(page, base_url, project, artifact_dir, writer)
            except Exception as exc:
                failure_artifact = _save_screenshot(page, artifact_dir / "ui-failure.png")
                if hasattr(exc, "add_note"):
                    exc.add_note(f"UI failure artifact: {failure_artifact}")
                raise
            if page_errors:
                raise AssertionError("browser page errors: " + "; ".join(page_errors))
            context.close()
            browser.close()
        return result
    finally:
        writer.reload_release.set()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        codey_server.STATE = original_state
        provider_services.connect_provider = original_connect_provider
        provider_services.connect_fresh_provider_tab = original_connect_fresh_provider_tab
        provider_services.provider_tab_availability = original_provider_tab_availability
        provider_services.connect_existing_provider = original_connect_existing_provider
        codey_server.pick_folder = original_pick_folder
        if original_state is not None:
            provider_controls.set_teach_handler(
                functools.partial(sibling_probe.handle_control_teach, original_state)
            )
            provider_controls.set_doctor_handler(
                functools.partial(sibling_probe.handle_profile_doctor, original_state)
            )
        shutil.rmtree(temp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--artifacts", default=".e2e-artifacts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = run_ui_e2e(headed=args.headed, artifacts=args.artifacts)
    except Exception as exc:
        data = {"ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print("PASS" if data["ok"] else f"FAIL: {data.get('error', '')}")
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
