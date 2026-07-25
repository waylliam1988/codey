from __future__ import annotations

import argparse
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

from codey import cancellation, provider_controls
from codey import server as codey_server


TASK = (
    "Create result.txt containing exactly 'browser e2e passed' and run the tests. "
    "Finish only after the tests pass."
)


def _close_event_stream(page: Page) -> None:
    page.wait_for_function("typeof evtSrc !== 'undefined' && evtSrc !== null")
    page.evaluate("evtSrc.close()")


class ScriptedWriter:
    name = "Browser E2E Writer"
    location = "scripted://writer"

    def __init__(self) -> None:
        self.step = 0

    def new_chat(self) -> None:
        self.step = 0

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        if "private first-draft answer" in text:
            if "Explain box breathing without project access" in text:
                return "Draft box breathing answer from the selected model."
            return "Draft answer from the selected model."
        if "private first-draft new-project implementation plan" in text:
            return "Draft implementation plan from the selected model."
        if "Synthesize the private advisor notes" in text:
            if "The user approved and ran this shell command" in text:
                return "approval continuation completed"
            if "Stay active across one UI reload" in text:
                return "reload completed"
            if "Finish while state reconciliation is delayed" in text:
                return "delayed state completed"
            if "Request a shell command" in text:
                return "git status request completed"
            if "Wait until stopped by the UI" in text:
                return "stopped"
            if "Explain box breathing without project access" in text:
                return "Combined box breathing answer from hidden advisors."
            if "Discuss a breathing app without changing files" in text:
                return "Combined project breathing answer without file changes."
            if "Create result.txt containing exactly" in text:
                return "browser flow completed"
            return "Combined answer."
        if text == "Explain box breathing without project access.":
            return "Box breathing uses equal inhale, hold, exhale, and hold phases."
        if "Wait until stopped by the UI" in text:
            cancellation.wait(30)
            raise AssertionError("responsive stop did not cancel the provider wait")
        if "Stay active across one UI reload" in text:
            cancellation.wait(4)
            return '{"tool":"done","args":{"summary":"reload completed"}}'
        if "Finish while state reconciliation is delayed" in text:
            cancellation.wait(1.5)
            return '{"tool":"done","args":{"summary":"delayed state completed"}}'
        if "The user approved and ran this shell command" in text:
            return '{"tool":"done","args":{"summary":"approval continuation completed"}}'
        if "Discuss a breathing app without changing files" in text:
            if "Private ChangeBrief" in text and "read-only project audit" in text:
                return (
                    '{"tool":"done","args":{"summary":"Combined project breathing '
                    'answer without file changes."}}'
                )
            return (
                '{"tool":"done","args":{"summary":"Start with one guided breathing '
                'rhythm.\\n\\nAdd customization only after the basic exercise feels calm."}}'
            )
        if "Request a shell command" in text:
            return (
                '{"tool":"shell","args":{"command":"git status --short",'
                '"path":"."}}'
            )
        if "[tool_result tool=edit" in text:
            self.step = 2
            return '{"tool":"run","args":{"command":"python -m unittest","path":"."}}'
        if "[tool_result tool=run" in text:
            self.step = 3
            return '{"tool":"done","args":{"summary":"browser flow completed"}}'
        self.step = 1
        return (
            '{"tool":"edit","args":{"path":"result.txt",'
            '"content":"browser e2e passed"}}'
        )

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


def _exercise_page(
    page: Page,
    base_url: str,
    project: Path,
    artifacts: Path,
) -> dict:
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator("#task").fill("Explain box breathing without project access.")
    page.locator("#send").click()
    expect(page.locator(".msg.asst .body")).to_contain_text(
        "Combined box breathing answer",
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
        "Combined project breathing answer",
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
    expect(page.locator("#chat")).to_contain_text("checks passed")
    expect(page.locator("#chat")).to_contain_text("DeepSeek approved")
    expect(page.locator("#chat")).not_to_contain_text('{"tool"')
    expect(page.locator("#chat")).not_to_contain_text("[agent]")

    result_file = project / "result.txt"
    _wait_for_file(result_file, exists=True, page=page)
    if result_file.read_text(encoding="utf-8") != "browser e2e passed":
        raise AssertionError("result.txt has unexpected content")

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
    page.screenshot(path=str(done_screenshot), full_page=True)

    restore = page.locator("#changes-restore")
    expect(restore).to_be_enabled()
    restore.click()
    _wait_for_file(result_file, exists=False, page=page)
    expect(page.locator("#changes-body")).to_contain_text("No changes")
    page.wait_for_timeout(500)
    restored_screenshot = artifacts / "ui-restored.png"
    page.screenshot(path=str(restored_screenshot), full_page=True)

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

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = page.request.get(base_url + "api/state").json()
        terminal = snapshot.get("last_terminal_event") or {}
        if (
            not snapshot.get("busy")
            and terminal.get("summary") == "approval continuation completed"
        ):
            break
        page.wait_for_timeout(100)
    else:
        raise AssertionError(
            "approved continuation did not finish while disconnected: "
            + json.dumps(snapshot, ensure_ascii=False)
        )

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
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#stop")).to_be_visible(timeout=3_000)
    expect(page.locator("#status")).to_contain_text("Running")
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
            "plain New Chat hidden consensus",
            "project picker",
            "provider selection",
            "project discussion without file changes",
            "project hidden consensus",
            "project answer before changed receipt",
            "SSE task lifecycle",
            "agent edit and test",
            "review status",
            "task receipt",
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
        "screenshots": [str(done_screenshot), str(restored_screenshot)],
    }


def run_ui_e2e(*, headed: bool = False, artifacts: str | Path | None = None) -> dict:
    artifact_dir = Path(artifacts or ".e2e-artifacts").resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="codey-ui-e2e-")).resolve()
    project = temp_root / "project"
    project.mkdir()
    _make_project(project)

    original_state = codey_server.STATE
    original_connect_provider = codey_server.connect_provider
    original_connect_existing = codey_server.connect_existing_provider
    original_provider_availability = codey_server.provider_availability
    original_pick_folder = codey_server.pick_folder
    writer = ScriptedWriter()
    httpd: codey_server.CodeyHTTPServer | None = None
    try:
        codey_server.STATE = codey_server.State(temp_root / "state")
        provider_controls.set_teach_handler(codey_server.STATE.handle_control_teach)
        codey_server.connect_provider = lambda provider_id: writer
        codey_server.connect_existing_provider = lambda provider_id: ScriptedReviewer()
        codey_server.provider_availability = lambda: {
            "deepseek": True,
            "mimo": True,
            "qwen": True,
            "stepfun": True,
            "glm": True,
        }
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
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            try:
                result = _exercise_page(page, base_url, project, artifact_dir)
            except Exception:
                page.screenshot(path=str(artifact_dir / "ui-failure.png"), full_page=True)
                raise
            if page_errors:
                raise AssertionError("browser page errors: " + "; ".join(page_errors))
            context.close()
            browser.close()
        return result
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        codey_server.STATE = original_state
        codey_server.connect_provider = original_connect_provider
        codey_server.connect_existing_provider = original_connect_existing
        codey_server.provider_availability = original_provider_availability
        codey_server.pick_folder = original_pick_folder
        provider_controls.set_teach_handler(original_state.handle_control_teach)
        provider_controls.set_doctor_handler(original_state.handle_profile_doctor)
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
