"""Real Chromium route cancellation, listener ownership and cleanup after failure."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Error, Response, Route, async_playwright

from codey.automation.browser_contracts import BrowserAction, BrowserConfig, BrowserToolOutput
from codey.automation.browser_network import BrowserNetwork
from codey.automation.browser_runtime import BrowserRuntime
from codey.types.json_value import JsonObject
from tests.support.real_cdp import chromium_executable
from tests.test_browser_async_runtime import invoke


@pytest.mark.parametrize("cancel_close", [False, True])
def test_close_reaps_slow_routes_and_retains_unrelated_handlers(cancel_close: bool) -> None:
    async def exercise() -> None:
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        errors: list[str] = []
        loop = asyncio.get_running_loop()
        def collect_error(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            errors.append(str(context))
        loop.set_exception_handler(collect_error)
        async def policy(request: JsonObject) -> JsonObject:
            if str(request["url"]).endswith("/slow"):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return {"decision": "abort", "reason": "released"}
            return {"decision": "fulfill", "headers": {"content-type": "text/html"},
                "body": "<title>Initial</title><script>fetch('/slow').catch(()=>{});</script>"}
        network = BrowserNetwork(policy)
        unrelated = asyncio.create_task(release.wait())
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:
                context = await browser.new_context(service_workers="block")
                page = await context.new_page()
                external_responses: list[int] = []
                def external_response(response: Response) -> None:
                    external_responses.append(response.status)
                async def external_route(route: Route) -> None:
                    await route.fulfill(status=201, content_type="text/html", body="<title>External</title>")
                page.on("response", external_response)
                await context.route("**/external", external_route)
                network.watch(page, "target")
                await network.guard(context)
                await page.goto("https://example.com/start", wait_until="domcontentloaded")
                await asyncio.wait_for(entered.wait(), timeout=5)
                network._event("target", {"kind": "fixture"})
                closing = asyncio.create_task(network.close())
                if cancel_close:
                    await asyncio.sleep(0.1)
                    closing.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await closing
                else:
                    await asyncio.wait_for(closing, timeout=6)
                assert cancelled.is_set()
                assert not unrelated.done()
                assert not network._detach and not network._targets and not network._guarded_contexts
                assert not network._responses and not network._diagnostics
                assert not network._route_tasks
                await page.goto("https://example.com/external")
                assert await page.title() == "External" and external_responses[-1] == 201
                assert not network._responses
                await network.close()
            finally:
                release.set()
                await unrelated
                await browser.close()
        await asyncio.sleep(0)
        assert not errors, errors
    asyncio.run(exercise())


def test_response_facts_do_not_require_async_complete_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable_headers(self: Response) -> dict[str, str]:
        raise Error("complete headers unavailable")
    monkeypatch.setattr(Response, "all_headers", unavailable_headers)
    async def exercise() -> None:
        async def policy(request: JsonObject) -> JsonObject:
            path = urlsplit(str(request["url"])).path
            failed = path == "/failed"
            return {"decision": "fulfill", "status": 503 if failed else 200,
                "headers": {"content-type": "text/html; charset=utf-8"},
                "body": ("<script>location.replace('/home')</script>" if path == "/redirect" else
                         "Failed" if failed else "<form action='/failed'><button>Submit</button></form>")}
        network = BrowserNetwork(policy)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=chromium_executable(), headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                network.watch(page, "target")
                await network.guard(context)
                await page.goto("https://example.com/redirect")
                await page.wait_for_url("**/home")
                assert network.response_facts("target", page.url)["http_status"] == 200
                await page.locator("button").click()
                assert urlsplit(page.url).path == "/failed"
                assert network.response_facts("target", page.url) == {
                    "http_status": 503, "content_type": "text/html; charset=utf-8"}
                for _ in range(3):
                    transient = await context.new_page()
                    network.watch(transient, "transient")
                    await transient.goto("https://example.com/redirect", wait_until="commit")
                    await transient.close()
                await page.close()
                await network.close()
            finally:
                await browser.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_close", [False, True])
def test_network_failure_does_not_skip_browser_process_and_profile_cleanup(cancel_close: bool) -> None:
    async def policy(request: JsonObject) -> JsonObject:
        return {"decision": "fulfill", "body": "<title>Owned</title>"}
    class FailedNetwork(BrowserNetwork):
        async def close(self) -> None:
            await super().close()
            if cancel_close:
                raise asyncio.CancelledError("network cleanup failure")
            raise ValueError("network cleanup failure")
    async def exercise() -> None:
        runtime = BrowserRuntime(BrowserConfig(browser_path=chromium_executable()))
        runtime._network = FailedNetwork(policy)
        opened = BrowserToolOutput.read(await invoke(runtime, BrowserAction.NAVIGATE, {"url": "https://example.com/"}))
        assert opened.ok
        browser, process, profile = runtime._browser, runtime._process, runtime._profile
        assert browser is not None and process is not None and profile is not None
        try:
            with pytest.raises(asyncio.CancelledError if cancel_close else ValueError, match="network cleanup failure"):
                await runtime.close()
            assert not browser.is_connected() and process.poll() is not None and not profile.exists()
        finally:
            if browser.is_connected():
                runtime._network = BrowserNetwork(policy)
                await runtime.close()
    asyncio.run(exercise())
