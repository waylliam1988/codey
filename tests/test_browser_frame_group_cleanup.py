import asyncio
from collections.abc import Awaitable, Callable

import pytest
from playwright.async_api import async_playwright

from codey.automation.browser_frames import FrameContext, backend_node, backend_nodes, frame_context
from codey.types.json_value import JsonObject
from tests.support.real_cdp import chromium_executable


@pytest.mark.parametrize("multiple", [False, True])
def test_node_object_group_is_released_when_frame_marker_cleanup_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, multiple: bool,
) -> None:
    async def exercise() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("<button>Observe</button>")
                frame = page.main_frame
                async with frame_context(page, frame, "fixture") as context:
                    evaluate: Callable[[str, object], Awaitable[object]] = frame.evaluate
                    send = context.send
                    methods: list[str] = []

                    async def fail_delete(expression: str, arg: object = None) -> object:
                        if "delete globalThis" in expression:
                            raise asyncio.CancelledError
                        value: object = await evaluate(expression, arg)
                        return value

                    async def record_send(
                        _context: FrameContext, method: str, arguments: JsonObject | None = None,
                    ) -> JsonObject:
                        methods.append(method)
                        return await send(method, arguments)

                    with monkeypatch.context() as patch:
                        patch.setattr(frame, "evaluate", fail_delete)
                        patch.setattr(FrameContext, "send", record_send)
                        if multiple:
                            nodes = await frame.evaluate_handle("Array.from(document.querySelectorAll('button'))")
                            try:
                                with pytest.raises(asyncio.CancelledError):
                                    await backend_nodes(context, nodes)
                            finally:
                                await nodes.dispose()
                        else:
                            node = await frame.query_selector("button")
                            assert node is not None
                            try:
                                with pytest.raises(asyncio.CancelledError):
                                    await backend_node(context, node)
                            finally:
                                await node.dispose()
                    assert methods.count("DOM.describeNode") == 1
                    assert methods.count("Runtime.releaseObjectGroup") == 1
            finally:
                await browser.close()

    asyncio.run(exercise())
