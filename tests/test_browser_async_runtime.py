"""Exercise the task driver against Chromium rather than simulated Playwright handles."""
from __future__ import annotations

import asyncio
import errno
import shutil
import time
from pathlib import Path
from uuid import uuid4

import pytest
from playwright.async_api import Page

from codey.automation.browser_binding import PageBinding
from codey.automation.browser_contracts import BrowserAction, BrowserCommand, BrowserConfig, BrowserToolOutput
from codey.automation.browser_runtime import BrowserRuntime
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tests.support.real_cdp import chromium_executable

HTML = """<title>Records</title><label>City<input aria-label=City></label>
<button aria-controls=result onclick="document.querySelector('#result').textContent='173 QUARTZ_851'">Query</button>
<div id=result>Waiting</div><iframe srcdoc='<input aria-label=Nested>'></iframe>"""


@pytest.mark.parametrize("race", ["removed_lockfile", "late_cache_file"])
def test_profile_cleanup_finishes_after_chromium_shutdown_races(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch, race: str) -> None:
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "lockfile").touch()
    remove = shutil.rmtree
    attempts = 0

    def racing_remove(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if race == "removed_lockfile":
                (path / "lockfile").unlink()
                raise FileNotFoundError(2, "Chromium removed its lockfile", str(path / "lockfile"))
            (path / "cache").touch()
            raise OSError(errno.ENOTEMPTY, "Chromium added a cache file during shutdown", str(path))
        remove(path)

    runtime = BrowserRuntime(BrowserConfig())
    runtime._profile = profile
    monkeypatch.setattr("codey.automation.browser_cleanup.shutil.rmtree", racing_remove)
    asyncio.run(runtime.close())
    assert attempts == 2 and not profile.exists()


async def fixture_policy(request: JsonObject) -> JsonObject:
    return {"decision": "fulfill", "content_type": "text/html", "headers": {"content-type": "text/html"}, "body": HTML}


async def invoke(runtime: BrowserRuntime, action: BrowserAction, arguments: JsonObject | None = None) -> JsonValue:
    packet = await runtime.dispatch(BrowserCommand(uuid4().hex, action, arguments or {}, "test-task", "test-browser",
                                                    "test-effect", time.monotonic() + 30))
    return packet.message["value"]


def test_async_native_query_frames_and_capture() -> None:
    async def exercise() -> None:
        runtime = BrowserRuntime(BrowserConfig(browser_path=chromium_executable()), network_policy=fixture_policy)
        try:
            opened = BrowserToolOutput.read(await invoke(runtime, BrowserAction.NAVIGATE, {"url": "https://example.com/record"}))
            assert opened.ok, opened.model_text
            snap = BrowserToolOutput.read(await invoke(runtime, BrowserAction.SNAPSHOT))
            assert snap.ok, snap.model_text
            rows = snap.observation["elements"]
            assert isinstance(rows, list)
            refs = {row["name"]: row["ref"] for row in rows if isinstance(row, dict)}
            filled = BrowserToolOutput.read(await invoke(runtime, BrowserAction.FILL, {"ref": refs["City"], "value": "Fuzhou"}))
            assert filled.ok, filled.model_text
            fresh = BrowserToolOutput.read(await invoke(runtime, BrowserAction.SNAPSHOT))
            assert fresh.ok
            clicked = BrowserToolOutput.read(await invoke(runtime, BrowserAction.CLICK, {"ref": refs["Query"], "query": True}))
            assert clicked.ok, clicked.model_text
            captured = decode_json_object(await invoke(runtime, BrowserAction.QUERY_RESULT,
                                                        {"baseline": clicked.observation["query_baseline"]}))
            assert captured["query_confirmed"] is True
            assert "173 QUARTZ_851" in str(captured["text"])
            frames = BrowserToolOutput.read(await invoke(runtime, BrowserAction.FRAME_LIST))
            observed = frames.observation["frames"]
            assert isinstance(observed, list) and len(observed) == 2
            nested = decode_json_object(observed[1])
            selected = BrowserToolOutput.read(await invoke(runtime, BrowserAction.FRAME_SELECT, {"frame_id": nested["frame_id"]}))
            assert selected.ok, selected.model_text
            inner = BrowserToolOutput.read(await invoke(runtime, BrowserAction.SNAPSHOT))
            assert inner.ok and 'Nested' in inner.model_text
        finally:
            await runtime.close()
    asyncio.run(exercise())


def test_wait_can_be_cancelled_without_blocking_same_page_input() -> None:
    async def exercise() -> None:
        runtime = BrowserRuntime(BrowserConfig(browser_path=chromium_executable()), network_policy=fixture_policy)
        try:
            await invoke(runtime, BrowserAction.NAVIGATE, {"url": "https://example.com/record"})
            waiting = asyncio.create_task(invoke(runtime, BrowserAction.WAIT, {"condition": "text", "value": "never"}))
            await asyncio.sleep(0.1)
            snap = BrowserToolOutput.read(await invoke(runtime, BrowserAction.SNAPSHOT))
            assert snap.ok
            waiting.cancel()
            result = BrowserToolOutput.read(await waiting)
            assert not result.ok and result.observation["reason"] == "cancelled"
            assert result.observation["delivery"] == "not_delivered"
        finally:
            await runtime.close()
    asyncio.run(exercise())


def test_tab_list_handles_popup_arriving_from_another_tab_during_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def open_popup(page: Page) -> Page:
        await page.set_content('<button onclick="window.open(\'about:blank\')">Open popup</button>')
        async with page.expect_popup() as popup:
            await page.get_by_role("button", name="Open popup").click()
        return await popup.value

    async def exercise() -> None:
        runtime = BrowserRuntime(BrowserConfig(browser_path=chromium_executable()))
        try:
            for _ in range(2):
                opened = BrowserToolOutput.read(await invoke(runtime, BrowserAction.TAB_NEW))
                assert opened.ok, opened.model_text
            first, second = (state.page for state in runtime._pages.values())
            first_popup = await open_popup(first)
            bind = runtime._page_binding

            async def bind_with_other_popup(page: Page, *, owned: bool) -> PageBinding:
                if page is first_popup:
                    await open_popup(second)
                return await bind(page, owned=owned)

            monkeypatch.setattr(runtime, "_page_binding", bind_with_other_popup)
            listing = BrowserToolOutput.read(await invoke(runtime, BrowserAction.TAB_LIST))
            assert listing.ok, listing.model_text
            tabs = listing.observation["tabs"]
            assert isinstance(tabs, list) and len(tabs) == 4
            again = BrowserToolOutput.read(await invoke(runtime, BrowserAction.TAB_LIST))
            assert again.ok and again.observation["tabs"] == tabs
        finally:
            await runtime.close()

    asyncio.run(exercise())
