"""Frame IDs and backend nodes come from their actual Chromium execution worlds."""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from codey.automation.browser_contracts import BrowserAction
from codey.automation.browser_document import same_document
from codey.automation.browser_frames import backend_node, frame_context, resolve_backend
from codey.automation.browser_session import BrowserSession
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tests.support.real_cdp import chromium_executable
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser


def test_duplicate_srcdoc_frames_have_distinct_documents_and_resolvable_nodes() -> None:
    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<iframe srcdoc="<input aria-label=City>"></iframe>'
                                       '<iframe srcdoc="<input aria-label=City>"></iframe>')
                cdp = await page.context.new_cdp_session(page)
                raw: object = await cdp.send("Target.getTargetInfo")
                target = require_json_string(decode_json_object(decode_json_object(raw)["targetInfo"])["targetId"], "target")
                await cdp.detach()
                documents: list[JsonObject] = []
                for frame in page.frames[1:]:
                    async with frame_context(page, frame, target) as context:
                        documents.append(context.document)
                        node = await frame.query_selector("input")
                        assert node is not None
                        identity = await backend_node(context, node)
                        restored = await resolve_backend(context, identity)
                        assert await restored.get_attribute("aria-label") == "City"
                        await restored.dispose()
                        await node.dispose()
                assert len(documents) == 2
                assert not same_document(documents[0], documents[1])
            finally:
                await browser.close()
    asyncio.run(exercise())


def test_click_blocked_in_ancestor_frame_reports_the_actual_hit_layer(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, """<iframe title="Review" srcdoc="<button onclick='window.clicked=1'>Approve</button>"></iframe>
        <div aria-label="Parent overlay" style="position:fixed;inset:0;z-index:10;background:white"></div>""")
    frames = browser.action(BrowserAction.FRAME_LIST).observation["frames"]
    assert isinstance(frames, list)
    child = next(decode_json_object(frame) for frame in frames if decode_json_object(frame)["main"] is False)
    assert browser.action(BrowserAction.FRAME_SELECT, {"frame_id": child["frame_id"]}).ok
    document = browser.document_identity()
    assert document is not None and document["frame_id"] == child["frame_id"]
    button = next(row for row in elements(browser.snapshot().observation) if row["name"] == "Approve")
    ref = require_json_string(button["ref"], "ref")
    attempt = browser.click(ref)
    assert not attempt.ok and attempt.observation["delivery"] == "not_delivered"
    assert attempt.observation["ref"] == ref and attempt.observation["reason"] == "occluded"
    assert attempt.observation["hit_frame_depth"] == 1 and attempt.observation["hit_frame_url"] == URL
    point = decode_json_object(attempt.observation["hit_point"])
    assert isinstance(point["x"], (int, float)) and isinstance(point["y"], (int, float))
    assert same_document(attempt.observation["document"], document)
    assert browser.evaluate("Boolean(window.clicked)").observation["value"] is False


def test_parent_snapshot_previews_child_controls_with_their_own_document(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, "<button>Top control</button><iframe srcdoc=\"<button>Child control</button>\"></iframe>")
    parent = browser.snapshot(max_elements=2)
    frames = parent.observation["frames"]
    assert parent.ok and isinstance(frames, list)
    frame, = [decode_json_object(raw) for raw in frames]
    child, = elements(frame)
    assert child["name"] == "Child control"
    child_ref = require_json_string(child["ref"], "child ref")
    assert browser.click(child_ref).ok
    assert browser.document_identity() == frame["document"]
