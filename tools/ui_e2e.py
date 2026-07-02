from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey import provider_controls
from codey import server as codey_server


TASK = (
    "Create result.txt containing exactly 'browser e2e passed' and run the tests. "
    "Finish only after the tests pass."
)


class ScriptedWriter:
    name = "Browser E2E Writer"
    location = "scripted://writer"

    def __init__(self) -> None:
        self.step = 0

    def new_chat(self) -> None:
        self.step = 0

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
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


def _exercise_page(page: Page, base_url: str, project: Path, artifacts: Path) -> dict:
    page.goto(base_url, wait_until="domcontentloaded")
    expect(page.locator("#btn-add-project")).to_be_visible()
    page.locator("#btn-add-project").click()
    expect(page.locator("#composer-context")).to_contain_text(project.name)

    page.locator("#provider-button").click()
    qwen = page.locator('[data-provider="qwen"]')
    expect(qwen).to_be_visible()
    qwen.click()
    expect(page.locator("#provider-name")).to_have_text("Qwen")

    page.locator("#task").fill(TASK)
    page.locator("#send").click()
    expect(page.locator("#chat")).to_contain_text("Done", timeout=30_000)
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
    expect(page.locator("#chat")).to_contain_text("Command approval")
    deny = page.get_by_role("button", name="Deny", exact=True)
    expect(deny).to_be_visible()
    deny.click()
    expect(page.locator("#chat")).to_contain_text("Denied")
    expect(page.locator("#chat")).to_contain_text("git status --short")

    return {
        "ok": True,
        "url": base_url,
        "checks": [
            "project picker",
            "provider selection",
            "SSE task lifecycle",
            "agent edit and test",
            "review status",
            "task receipt",
            "diff drawer",
            "snapshot restore",
            "shell approval denial",
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
    httpd: ThreadingHTTPServer | None = None
    try:
        codey_server.STATE = codey_server.State(temp_root / "state")
        provider_controls.set_teach_handler(codey_server.STATE.handle_control_teach)
        codey_server.connect_provider = lambda provider_id: writer
        codey_server.connect_existing_provider = lambda provider_id: ScriptedReviewer()
        codey_server.provider_availability = lambda: {
            "deepseek": True,
            "qwen": True,
            "mimo": True,
        }
        codey_server.pick_folder = lambda mode="open", initial=None: str(project)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), codey_server.Handler)
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
