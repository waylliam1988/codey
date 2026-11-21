"""Frame IDs and backend nodes come from their actual Chromium execution worlds."""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from codey.automation.browser_document import same_document
from codey.automation.browser_frames import backend_node, frame_context, resolve_backend
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tests.support.real_cdp import chromium_executable


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
