"""The task browser's async Playwright owner, hosted only in its worker process."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from playwright.async_api import (
    Browser,
    CDPSession,
    Dialog,
    Download,
    ElementHandle,
    Error,
    Frame,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from codey.automation.browser_binding import BrowserBinding, PageBinding, browser_endpoint, browser_identity
from codey.automation.browser_contracts import (
    MAX_PENDING_COMMANDS,
    BrowserAction,
    BrowserCommand,
    BrowserCompletion,
    BrowserConfig,
    BrowserToolOutput,
)
from codey.automation.browser_document import same_document
from codey.automation.browser_frames import FrameContext, backend_node, frame_context, resolve_backend
from codey.automation.browser_images import MAX_IMAGE_PIXELS, annotate_capture, image_number, pixel_digest
from codey.automation.browser_input import _HIT_TEST
from codey.automation.browser_ipc import BrowserPacket
from codey.automation.browser_native_input import NativeInput
from codey.automation.browser_observation import VISIBLE_TEXT
from codey.automation.browser_query import changed_query_region
from codey.automation.browser_query_async import bind_query_target
from codey.automation.browser_refs import ElementRecord, ElementRegistry
from codey.automation.browser_snapshot import (
    AX_CONTROL_ROLES,
    collect_ax_tree,
    collect_snapshot,
    diff_snapshot,
    element_metadata,
    element_role,
    file_control_details,
    semantic_fingerprint,
)
from codey.policies.network import check_fetch_url
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    parse_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)

NetworkPolicy = Callable[[JsonObject], Awaitable[JsonObject]]
Progress = Callable[[JsonObject], Awaitable[None]]
_PROPERTY_RETURN = re.compile(r"\s*return[ \t]+(window\.[A-Za-z_$][\w$]*)(?:\s*;)?\s*", re.ASCII)


def _evaluate(subject: Page | Frame | ElementHandle, expression: str) -> Awaitable[object]:
    evaluate: Callable[[str], Awaitable[object]] = subject.evaluate
    return evaluate(expression)


def _send(cdp: CDPSession, method: str, arguments: JsonObject | None = None) -> Awaitable[object]:
    send: Callable[[str, JsonObject | None], Awaitable[object]] = cdp.send
    return send(method, arguments)


@dataclass
class Invocation:
    command: BrowserCommand
    started_at: float = field(default_factory=time.monotonic)
    dispatch_state: Literal["not_sent", "unknown", "confirmed"] = "not_sent"
    phase: str = "queued"
    binary: bytes = b""
    document: JsonObject | None = None
    query_baseline: JsonObject | None = None
    input_state: NativeInput | None = None


@dataclass
class PageState:
    page: Page
    binding: PageBinding
    selected_frame: str = ""


class BrowserRuntime:
    def __init__(self, config: BrowserConfig, *, network_policy: NetworkPolicy | None = None,
                 progress: Progress | None = None, binding: BrowserBinding | None = None,
                 inputs: JsonObject | None = None) -> None:
        self.config, self.network_policy, self.progress = config, network_policy, progress
        self._resume_binding = binding
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._profile: Path | None = None
        self._endpoint = ""
        self._port = config.cdp_port
        self._pages: dict[str, PageState] = {}
        self._active = ""
        self._registry = ElementRegistry[ElementHandle]()
        self._revision = 0
        self._observations: dict[int, JsonObject] = {}
        self._dialogs: dict[str, Dialog] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._navigation: dict[str, JsonObject] = {}
        self._owns_browser = False
        self._visual: dict[str, JsonObject] = {}
        self._file_controls: dict[str, JsonObject] = {}
        self._file_controls_truncated = False
        self._file_document: JsonObject | None = None
        self._connection_lock = asyncio.Lock()
        self._network_requests = 0
        self._popups: dict[str, list[Page]] = {}
        self._downloads: dict[str, Download] = {}
        self._input_states = {target: NativeInput.read(value) for target, value in (inputs or {}).items()}
        self._documents: dict[tuple[str, str], JsonObject] = {}

    async def _route(self, route: Route) -> None:
        if self._network_requests >= MAX_PENDING_COMMANDS:
            await route.abort()
            return
        self._network_requests += 1
        try:
            await self._route_request(route)
        finally:
            self._network_requests -= 1

    async def _route_request(self, route: Route) -> None:
        request = route.request
        if self.network_policy is None:
            reason = await asyncio.to_thread(check_fetch_url, request.url)
            decision: JsonObject = {"decision": "abort" if reason else "continue"}
        else:
            decision = await self.network_policy({"url": request.url, "resource_type": request.resource_type,
                                                  "navigation": request.is_navigation_request()})
        kind = decision.get("decision")
        if kind == "continue":
            await route.continue_()
        elif kind == "abort":
            await route.abort()
        elif kind == "fulfill":
            headers = decode_json_object(decision.get("headers", {}))
            if not all(isinstance(value, str) for value in headers.values()):
                raise ValueError("invalid route response headers")
            await route.fulfill(status=require_json_integer(decision.get("status", 200), "status"),
                headers={key: require_json_string(value, key) for key, value in headers.items()},
                body=base64.b64decode(require_json_string(decision["body_base64"], "body_base64"), validate=True)
                    if "body_base64" in decision else require_json_string(decision.get("body", ""), "body"))
        else:
            raise ValueError("invalid network policy decision")

    async def _connect(self, invocation: Invocation | None = None) -> None:
        async with self._connection_lock:
            await self._connect_once(invocation)

    async def _connect_once(self, invocation: Invocation | None) -> None:
        if self._browser is not None:
            if not self._browser.is_connected():
                raise RuntimeError("browser_resource_lost")
            if self._resume_binding is not None:
                await self._restore_pages(self._resume_binding)
            return
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        binding = self._resume_binding
        if self._endpoint:
            endpoint = self._endpoint
        elif binding is not None:
            self._port = binding.port
            endpoint = await asyncio.to_thread(browser_endpoint, binding.port)
            if browser_identity(endpoint) != binding.browser_id:
                raise RuntimeError("original_browser_instance_lost")
        elif not self.config.isolated and self.config.cdp_port:
            try:
                endpoint = await asyncio.to_thread(browser_endpoint, self.config.cdp_port)
            except OSError:
                if not self.config.launch:
                    raise
                endpoint = await self._launch()
        else:
            if not self.config.launch:
                raise RuntimeError("browser_resource_unavailable")
            endpoint = await self._launch()
        self._endpoint = endpoint
        if invocation is not None:
            await self._mark(invocation, "browser_connected")
        self._browser = await self._playwright.chromium.connect_over_cdp(endpoint, timeout=10_000)
        for context in self._browser.contexts:
            await context.route("**/*", self._route)
        if binding is not None:
            await self._restore_pages(binding)

    async def _restore_pages(self, binding: BrowserBinding) -> None:
        if self._browser is None:
            raise RuntimeError("browser_resource_unavailable")
        self._owns_browser = binding.owned_browser
        self._profile = Path(binding.owned_profile) if binding.owned_profile else None
        expected = {page.target_id: page for page in binding.pages}
        for context in self._browser.contexts:
            for page in context.pages:
                identity = await self._page_binding(page, owned=False)
                if identity.target_id in expected:
                    recorded = expected[identity.target_id]
                    if identity.context_id != recorded.context_id:
                        raise RuntimeError("original_browser_context_lost")
                    self._register(page, recorded)
        if binding.active_target and binding.active_target not in self._pages:
            raise RuntimeError("original_browser_target_lost")
        self._active = binding.active_target
        if self._active:
            self._pages[self._active].selected_frame = binding.selected_frame
        self._resume_binding = None
        for target, inputs in self._input_states.items():
            if inputs.keys and target in self._pages:
                await inputs.release(self._pages[target].page)

    async def _launch(self) -> str:
        if self._playwright is None:
            raise RuntimeError("Playwright not started")
        if self._process is not None:
            raise RuntimeError("original_browser_startup_failed")
        executable = self.config.browser_path
        if not executable:
            if os.name == "nt":
                from codey.automation.browser import _find_browser

                executable = str(_find_browser().path)
            else:
                executable = self._playwright.chromium.executable_path
        if self.config.profile_dir:
            profile = Path(self.config.profile_dir)
            profile.mkdir(parents=True, exist_ok=True)
        else:
            self._profile = Path(tempfile.mkdtemp(prefix="codey-task-browser-"))
            profile = self._profile
        args = [executable, "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"]
        if self.config.headless:
            args.insert(1, "--headless=new")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("task_browser_exited_before_connect")
            try:
                lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
                self._port = int(lines[0])
                endpoint = await asyncio.to_thread(browser_endpoint, self._port)
                self._owns_browser = True
                return endpoint
            except (OSError, ValueError, IndexError):
                await asyncio.sleep(0.05)
        raise TimeoutError("task_browser_startup_deadline")

    @staticmethod
    async def _page_binding(page: Page, *, owned: bool) -> PageBinding:
        cdp = await page.context.new_cdp_session(page)
        try:
            raw: object = await _send(cdp, "Target.getTargetInfo")
            info = decode_json_object(decode_json_object(raw)["targetInfo"])
            return PageBinding(require_json_string(info["targetId"], "targetId"),
                               require_json_string(info.get("browserContextId", ""), "contextId"), owned)
        finally:
            await cdp.detach()

    def _register(self, page: Page, binding: PageBinding) -> PageState:
        if binding.target_id in self._pages:
            return self._pages[binding.target_id]
        state = PageState(page, binding)
        self._pages[binding.target_id] = state

        def dialog_opened(dialog: Dialog) -> None:
            self._dialogs[binding.target_id] = dialog

        def popup_opened(popup: Page) -> None:
            self._popups.setdefault(binding.target_id, []).append(popup)

        def download_started(download: Download) -> None:
            self._downloads[binding.target_id] = download

        page.on("dialog", dialog_opened)
        page.on("popup", popup_opened)
        page.on("download", download_started)
        return state

    async def _register_popups(self) -> None:
        for pages in self._popups.values():
            for page in pages:
                if not page.is_closed() and not any(state.page is page for state in self._pages.values()):
                    self._register(page, await self._page_binding(page, owned=True))

    async def _current(self, invocation: Invocation, *, create: bool = False) -> PageState:
        await self._connect(invocation)
        if self._active in self._pages:
            state = self._pages[self._active]
            if state.page.is_closed():
                raise RuntimeError("browser_target_lost")
            return state
        if not create or self._browser is None:
            raise RuntimeError("browser_not_bound")
        context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context(service_workers="block")
        await context.route("**/*", self._route)
        page = await context.new_page()
        state = self._register(page, await self._page_binding(page, owned=True))
        self._active = state.binding.target_id
        return state

    @asynccontextmanager
    async def _frame(self, state: PageState) -> AsyncGenerator[FrameContext, None]:
        for frame in state.page.frames:
            async with frame_context(state.page, frame, state.binding.target_id) as observed:
                if not state.selected_frame and frame is state.page.main_frame or observed.document["frame_id"] == state.selected_frame:
                    self._documents[(state.binding.target_id, state.selected_frame)] = observed.document
                    yield observed
                    return
        raise RuntimeError("selected_frame_lost")

    async def _mark(self, invocation: Invocation, phase: str, *,
                    dispatch: Literal["not_sent", "unknown", "confirmed"] | None = None) -> None:
        invocation.phase = phase
        if dispatch is not None:
            invocation.dispatch_state = dispatch
        if self.progress is not None:
            await self.progress({"operation_id": invocation.command.operation_id,
                "effect_ref": invocation.command.effect_ref, "phase": phase,
                "elapsed_ms": int((time.monotonic() - invocation.started_at) * 1000),
                "dispatch": invocation.dispatch_state, "binding": await self.binding(),
                "owner_task_id": invocation.command.owner_task_id, "resource_id": invocation.command.resource_id,
                "action": invocation.command.action.value, "document": invocation.document,
                "query_baseline": invocation.query_baseline,
                "input_state": invocation.input_state.payload() if invocation.input_state is not None else None})

    async def dispatch(self, command: BrowserCommand, *, binary: bytes = b"") -> BrowserPacket:
        invocation = Invocation(command, binary=binary)
        key = command.target_id or self._active or "connect"
        independent = {BrowserAction.WAIT, BrowserAction.DIALOG, BrowserAction.DIALOG_RESPOND,
                       BrowserAction.DOCUMENT, BrowserAction.BINDING}
        try:
            async with asyncio.timeout(max(0.0, command.deadline - time.monotonic())):
                if command.action in independent:
                    value, blob = await self._execute(invocation)
                else:
                    lock = self._locks.setdefault(key, asyncio.Lock())
                    async with lock:
                        await self._mark(invocation, "started")
                        value, blob = await self._execute(invocation)
            return BrowserPacket({"type": "result", "operation_id": command.operation_id, "value": value,
                "completion": BrowserCompletion.COMPLETED.value, "generation": self._revision, "binding": await self.binding()}, blob)
        except (asyncio.CancelledError, TimeoutError, Error, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "deadline" if isinstance(exc, TimeoutError) else str(exc)
            delivery = {"not_sent": "not_delivered", "unknown": "uncertain", "confirmed": "delivered"}[invocation.dispatch_state]
            output = BrowserToolOutput("ERROR: " + reason, False, {"action": command.action.value,
                "ref": command.arguments.get("ref", ""), "reason": reason, "phase": invocation.phase, "delivery": delivery,
                "document": invocation.document, "query_baseline": invocation.query_baseline,
                "input_state": invocation.input_state.payload() if invocation.input_state is not None else None})
            if reason == "browser_not_bound":
                output = BrowserToolOutput("ERROR: An HTTP-first source read does not bind the browser. "
                    "Use browser_navigate to explicitly open the authorized page, then observe it.", False, output.observation)
            return BrowserPacket({"type": "result", "operation_id": command.operation_id, "value": output.payload(),
                "completion": BrowserCompletion.FAILED.value, "generation": self._revision, "binding": await self.binding()})
        finally:
            for handle in self._registry.retired_handles():
                with contextlib.suppress(Error):
                    await handle.dispose()

    async def _state(self, invocation: Invocation, *, create: bool = False) -> PageState:
        command = invocation.command
        if command.target_id:
            await self._connect(invocation)
            state = self._pages.get(command.target_id)
            if state is None or state.page.is_closed():
                raise RuntimeError("original_browser_target_lost")
            return PageState(state.page, state.binding, command.frame_id or state.selected_frame)
        return await self._current(invocation, create=create)

    async def _execute(self, invocation: Invocation) -> tuple[JsonValue, bytes]:
        action = invocation.command.action
        if action == BrowserAction.BINDING:
            return await self.binding(), b""
        if action == BrowserAction.RESET_REFS:
            self._registry.invalidate_all()
            self._visual.clear()
            self._observations.clear()
            self._file_controls.clear()
            self._file_document = None
            self._revision += 1
            return None, b""
        if action == BrowserAction.DOCUMENT:
            if not self._active and (self._resume_binding is None or not self._resume_binding.active_target):
                return None, b""
            state = await self._state(invocation)
            if state.binding.target_id in self._dialogs:
                return self._documents.get((state.binding.target_id, state.selected_frame)), b""
            async with self._frame(state) as context:
                return context.document, b""
        if action in {BrowserAction.NAVIGATE, BrowserAction.TAB_NEW, BrowserAction.TAB_LIST, BrowserAction.TAB_SWITCH,
                      BrowserAction.FRAME_LIST, BrowserAction.FRAME_SELECT, BrowserAction.DIALOG, BrowserAction.DIALOG_RESPOND}:
            return (await self._navigation_action(invocation)).payload(), b""
        state = await self._state(invocation)
        if action == BrowserAction.WAIT:
            expected = invocation.command.arguments.get("expected_document")
            if isinstance(expected, dict) and isinstance(expected.get("frame_id"), str):
                state = PageState(state.page, state.binding, str(expected["frame_id"]))
            return (await self._wait(invocation, state)).payload(), b""
        async with self._frame(state) as context:
            invocation.document = context.document
            if action == BrowserAction.PEEK:
                evaluated = await context.send("Runtime.evaluate", {"contextId": context.context_id,
                    "expression": require_json_string(invocation.command.arguments["expression"], "expression"),
                    "returnByValue": True, "throwOnSideEffect": True, "timeout": 1000})
                if "exceptionDetails" in evaluated:
                    raise ValueError("read_only_observation_failed")
                return decode_json_object(evaluated["result"]).get("value"), b""
            if action == BrowserAction.DISCLOSURE:
                from codey.automation.browser_disclosure import DETAILS

                args = invocation.command.arguments
                if args["document"] != context.document:
                    return None, b""
                world = await context.send("Page.createIsolatedWorld", {"frameId": context.document["frame_id"],
                                                                       "worldName": "codey-read-disclosure"})
                selected: JsonObject = {"task": args["task"], "selected": args["selected"]}
                disclosure = await context.send("Runtime.evaluate", {"contextId": world["executionContextId"],
                    "expression": "(" + DETAILS + ")(" + json.dumps(selected) + ")",
                    "returnByValue": True})
                return decode_json_object(disclosure["result"]).get("value"), b""
            if action in {BrowserAction.SNAPSHOT, BrowserAction.LOCATE, BrowserAction.INSPECT}:
                return (await self._observe(invocation, context)).payload(), b""
            if action in {BrowserAction.CLICK, BrowserAction.FILL, BrowserAction.HOVER, BrowserAction.PRESS,
                          BrowserAction.SELECT, BrowserAction.CHECK, BrowserAction.SCROLL, BrowserAction.EVALUATE}:
                return (await self._input(invocation, state, context)).payload(), b""
            if action == BrowserAction.CAPTURE:
                return await self._capture(context), b""
            if action in {BrowserAction.RECORDS, BrowserAction.QUERY_RESULT, BrowserAction.MATCH_QUERY}:
                return await self._query(invocation, context), b""
            return await self._visual_or_file(invocation, state, context)

    async def _navigation_action(self, invocation: Invocation) -> BrowserToolOutput:
        action, args = invocation.command.action, invocation.command.arguments
        if action == BrowserAction.TAB_LIST:
            if self._browser is None and self._resume_binding is None:
                return BrowserToolOutput("No task pages are open.", True, {"tabs": [], "page_url": "", "text": "No task pages are open."})
            await self._connect(invocation)
            await self._register_popups()
            available = [(key, state) for key, state in self._pages.items() if not state.page.is_closed()]
            tabs: list[JsonValue] = [{"index": index, "page_id": key, "url": state.page.url, "title": await state.page.title()}
                for index, (key, state) in enumerate(available)]
            return BrowserToolOutput(json.dumps(tabs), True, {"tabs": tabs, "page_url": self._pages[self._active].page.url if self._active else "",
                                                             "text": json.dumps(tabs)})
        if action == BrowserAction.TAB_SWITCH:
            target = args.get("target_id")
            if not isinstance(target, str) or not target:
                index = require_json_integer(args["index"], "index")
                targets = [key for key, state in self._pages.items() if not state.page.is_closed()]
                if not 0 <= index < len(targets):
                    raise ValueError("tab_unavailable")
                target = targets[index]
            if target not in self._pages or self._pages[target].page.is_closed():
                raise ValueError("tab_unavailable")
            self._active = target
            self._revision += 1
            return BrowserToolOutput("Switched task page.", True, {"action": "tab_switch", "index": args.get("index", 0),
                "url": self._pages[target].page.url, "page_id": target, "delivery": "delivered"})
        if action == BrowserAction.TAB_NEW:
            await self._connect(invocation)
        first_page = action == BrowserAction.TAB_NEW and not self._active
        state = await self._state(invocation, create=action in {BrowserAction.NAVIGATE, BrowserAction.TAB_NEW})
        if action in {BrowserAction.DIALOG, BrowserAction.DIALOG_RESPOND}:
            dialog = self._dialogs.get(state.binding.target_id)
            if action == BrowserAction.DIALOG:
                return BrowserToolOutput("No dialog." if dialog is None else dialog.message, True,
                    {"action": "dialog", "open": dialog is not None, "type": dialog.type if dialog is not None else "",
                     "message": dialog.message if dialog is not None else ""})
            if dialog is None:
                raise ValueError("dialog_unavailable")
            await self._mark(invocation, "dialog_dispatch", dispatch="unknown")
            if args.get("accept") is True:
                await dialog.accept(require_json_string(args.get("text", ""), "text"))
            else:
                await dialog.dismiss()
            self._dialogs.pop(state.binding.target_id, None)
            await self._mark(invocation, "dialog_handled", dispatch="confirmed")
            return BrowserToolOutput("Handled the observed dialog.", True, {"action": "dialog_respond", "delivery": "delivered"})
        if action in {BrowserAction.FRAME_LIST, BrowserAction.FRAME_SELECT}:
            frames: list[JsonValue] = []
            for frame in state.page.frames:
                async with frame_context(state.page, frame, state.binding.target_id) as observed:
                    frames.append({"frame_id": observed.document["frame_id"], "url": frame.url, "name": frame.name,
                                   "document": observed.document, "main": frame is state.page.main_frame})
            if action == BrowserAction.FRAME_SELECT:
                selected = require_json_string(args["frame_id"], "frame_id")
                if not any(isinstance(row, dict) and row.get("frame_id") == selected for row in frames):
                    raise ValueError("frame_unavailable")
                self._pages[state.binding.target_id].selected_frame = selected
                self._revision += 1
            return BrowserToolOutput(json.dumps(frames, ensure_ascii=False), True,
                {"action": action.value, "frames": frames, "frame_id": args.get("frame_id", ""), "delivery": "delivered"})
        url = require_json_string(args.get("url", "about:blank"), "url")
        if url != "about:blank" and (reason := check_fetch_url(url, resolve=False)):
            raise ValueError("navigation_denied: " + reason)
        if action == BrowserAction.TAB_NEW and not first_page:
            page = await state.page.context.new_page()
            state = self._register(page, await self._page_binding(page, owned=True))
            self._active = state.binding.target_id
        previous = state.page.url
        await self._mark(invocation, "navigation_dispatch", dispatch="unknown")
        response = await state.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await self._mark(invocation, "navigation_complete", dispatch="confirmed")
        self._pages[state.binding.target_id].selected_frame = ""
        self._revision += 1
        async with frame_context(state.page, state.page.main_frame, state.binding.target_id) as observed:
            headers = await response.all_headers() if response is not None else {}
            facts: JsonObject = {
                "http_status": response.status if response is not None else None,
                "content_type": headers.get("content-type", "")}
            self._navigation[self._document_key(observed.document)] = facts
        return BrowserToolOutput("Opened " + state.page.url, True, {"action": action.value, "url": state.page.url,
            "previous_url": previous, "page_id": state.binding.target_id, "generation": self._revision,
            **facts, "delivery": "delivered"})

    @staticmethod
    def _document_key(document: JsonObject) -> str:
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    async def _observe(self, invocation: Invocation, context: FrameContext) -> BrowserToolOutput:
        action, args = invocation.command.action, invocation.command.arguments
        self._revision += 1
        if action == BrowserAction.SNAPSHOT and args.get("file_inputs_only") is True:
            return await self._file_snapshot(context)
        if action == BrowserAction.INSPECT:
            record = await self._resolve(context, require_json_string(args["ref"], "ref"), purpose="inspect")
            raw: object = await _evaluate(record.handle, """el => ({tag:el.tagName.toLowerCase(),
                attributes:Object.fromEntries(Array.from(el.attributes,a=>[a.name,a.value])),
                text:(el.innerText||el.textContent||'').slice(0,4000),
                ancestors:Array.from((function*(){for(let n=el.parentElement;n;n=n.parentElement) yield n;})()).slice(0,8)
                    .map(n=>({tag:n.tagName.toLowerCase(),role:n.getAttribute('role')||'',name:n.getAttribute('aria-label')||''})),
                children:Array.from(el.children).slice(0,20).map(n=>({tag:n.tagName.toLowerCase(),text:(n.innerText||'').slice(0,240)}))})""")
            observed = decode_json_object(raw)
            box: object = await record.handle.bounding_box()
            return BrowserToolOutput(json.dumps(observed, ensure_ascii=False), True,
                {"action": "inspect", "ref": args["ref"], **observed, "box": decode_json_object(box) if box is not None else None})
        if action == BrowserAction.LOCATE:
            selector = require_json_string(args.get("selector", "*"), "selector")
            handles = await context.frame.query_selector_all(selector)
            rows: list[JsonValue] = []
            for handle in handles:
                if len(rows) >= 80:
                    break
                if not await handle.is_visible():
                    continue
                metadata, owner = await element_metadata(handle)
                role, name = element_role(metadata), " ".join(str(metadata.get("name") or "").split())[:240]
                if any(key in args and args[key] != value for key, value in (("role", role), ("name", name))):
                    continue
                text: object = await handle.inner_text()
                placeholder = await handle.get_attribute("placeholder") or ""
                if ("text" in args and str(args["text"]) not in str(text)
                        or "placeholder" in args and args["placeholder"] != placeholder):
                    continue
                ref = self._registry.observe_node(ElementRecord(handle, context.document, await backend_node(context, handle),
                    role, name, decode_json_object(metadata["context"]) if isinstance(metadata.get("context"), dict) else None,
                    owner, await bind_query_target(context, handle) if metadata.get("query_relation") else None,
                    self._revision, semantic_fingerprint(metadata), await backend_node(context, owner) if owner is not None else 0))
                rows.append({"ref": ref, "role": role, "name": name, "text": str(text)[:500], "placeholder": placeholder})
            return BrowserToolOutput(json.dumps(rows, ensure_ascii=False), True,
                {"action": "locate", "candidates": rows, "unique": len(rows) == 1, "document": context.document})
        if args.get("scope_ref"):
            await self._resolve(context, require_json_string(args["scope_ref"], "scope_ref"), purpose="scope")
        current = await collect_snapshot(context, self._registry, revision=self._revision,
            max_elements=require_json_integer(args.get("max_elements", 80), "max_elements"),
            max_text=require_json_integer(args.get("max_text", 12000), "max_text"),
            scope_ref=require_json_string(args.get("scope_ref", ""), "scope_ref"),
            selector=require_json_string(args.get("selector", ""), "selector"))
        base = args.get("base_revision")
        value = diff_snapshot(self._observations.get(base) if type(base) is int and isinstance(base, int) else None, current)
        self._observations[self._revision] = current
        while len(self._observations) > 4:
            del self._observations[next(iter(self._observations))]
        # The receipt retains the full observation; the model sees its derived delta.
        receipt = {**current, **{key: value[key] for key in ("kind", "base_revision", "added", "changed", "removed") if key in value}}
        return BrowserToolOutput(json.dumps(value, ensure_ascii=False), True, receipt)

    async def _capture(self, context: FrameContext) -> JsonObject:
        raw: object = await _evaluate(context.frame, """() => ({url:location.href,title:document.title,
            text:(document.body?.innerText||'').slice(0,100001),content_type:document.contentType,
            http_status:performance.getEntriesByType('navigation')[0]?.responseStatus||null,
            password_visible:Array.from(document.querySelectorAll('input[type=password]')).some(el=>el.getClientRects().length),
            render_pending:Array.from(document.querySelectorAll('app-root,#root,#app,#__next')).some(el=>
                !el.innerText.trim() || /^(loading|please wait|加载中|正在加载)[. 。…!]*$/i.test(el.innerText.trim()))})""")
        data = decode_json_object(raw)
        visible_text: object = await _evaluate(context.frame, "(" + VISIBLE_TEXT + ")(document.body)")
        data["text"] = require_json_string(visible_text, "visible text")
        text = require_json_string(data["text"], "text")
        facts = self._navigation.get(self._document_key(context.document), {})
        return {"status": "ok", **data, **facts, "text": text[:100000], "truncated": len(text) > 100000,
                "document": context.document, "observation": "current_page"}

    async def _scroll(self, invocation: Invocation, state: PageState, context: FrameContext) -> BrowserToolOutput:
        args = invocation.command.arguments
        ref = require_json_string(args.get("ref", ""), "ref")
        if ref:
            record = await self._resolve(context, ref)
            await record.handle.scroll_into_view_if_needed(timeout=2000)
            box = await record.handle.bounding_box()
            if box is None:
                raise ValueError("scroll_target_unavailable")
            await state.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        amount = require_json_integer(args["amount"], "amount")
        direction = require_json_string(args["direction"], "direction")
        if direction not in {"up", "down", "left", "right"} or not 1 <= amount <= 20000:
            raise ValueError("invalid_scroll")
        x = amount if direction == "right" else -amount if direction == "left" else 0
        y = amount if direction == "down" else -amount if direction == "up" else 0
        await self._mark(invocation, "wheel_dispatch", dispatch="unknown")
        await state.page.mouse.wheel(x, y)
        await self._mark(invocation, "wheel_complete", dispatch="confirmed")
        self._revision += 1
        return BrowserToolOutput("Scrolled the observed container." if ref else "Scrolled the page.", True,
            {"action": "scroll", "ref": ref, "delivery": "delivered", "effect_verified": False,
             "page_url": context.document["url"], "text": ""})

    async def _input(self, invocation: Invocation, state: PageState, context: FrameContext) -> BrowserToolOutput:
        action, args = invocation.command.action, invocation.command.arguments
        if action == BrowserAction.EVALUATE:
            await self._mark(invocation, "script_dispatch", dispatch="unknown")
            code = require_json_string(args["code"], "code")
            property_return = _PROPERTY_RETURN.fullmatch(code)
            raw: object = await _evaluate(context.frame, property_return[1] if property_return else code)
            await self._mark(invocation, "script_complete", dispatch="confirmed")
            value = decode_json_object({"value": raw})["value"]
            self._revision += 1
            return BrowserToolOutput(json.dumps(value, ensure_ascii=False), True,
                {"action": "eval", "value": value, "truncated": False, "delivery": "delivered"})
        if action == BrowserAction.SCROLL:
            return await self._scroll(invocation, state, context)
        ref = require_json_string(args["ref"], "ref")
        input_state = self._input_states.setdefault(state.binding.target_id, NativeInput())
        invocation.input_state = input_state
        if input_state.keys:
            await input_state.release(state.page)
        record = await self._resolve(context, ref)
        await record.handle.scroll_into_view_if_needed(timeout=2000)
        raw_ready: object = await _evaluate(record.handle, _HIT_TEST)
        ready = decode_json_object(raw_ready)
        if ready.get("ready") is not True:
            return BrowserToolOutput("Input not dispatched: " + str(ready.get("reason", "unavailable")), False,
                {"action": action.value, "ref": ref, "delivery": "not_delivered", **ready})
        ancestor = context.frame
        while ancestor.parent_frame is not None:
            owner = await ancestor.frame_element()
            try:
                outer: object = await _evaluate(owner, _HIT_TEST)
                readiness = decode_json_object(outer)
                if readiness.get("ready") is not True:
                    return BrowserToolOutput("Input not dispatched: enclosing frame is occluded.", False,
                        {"action": action.value, "ref": ref, "delivery": "not_delivered", **readiness})
            finally:
                await owner.dispose()
            ancestor = ancestor.parent_frame
        baseline: JsonObject | None = None
        if action == BrowserAction.CLICK and args.get("query") is True:
            baseline = await self._query_baseline(context, record, require_json_string(args.get("result_ref", ""), "result_ref"))
            invocation.query_baseline = baseline
        await self._mark(invocation, "input_dispatch", dispatch="unknown")
        observed: JsonObject = {"action": action.value, "ref": ref, "page_url": context.document["url"]}
        if action == BrowserAction.CLICK:
            await record.handle.click(timeout=5000, no_wait_after=True)
        elif action == BrowserAction.FILL:
            await record.handle.fill(require_json_string(args["value"], "value"), timeout=5000)
        elif action == BrowserAction.PRESS:
            async def key_progress() -> None:
                await self._mark(invocation, "key_input", dispatch="unknown")

            await input_state.press(state.page, record.handle, require_json_string(args["key"], "key"), key_progress)
        elif action == BrowserAction.HOVER:
            await record.handle.hover(timeout=5000)
        elif action == BrowserAction.SELECT:
            selected = await record.handle.select_option(value=require_json_strings(args["values"], "values"), timeout=5000)
            observed["values"] = list(selected)
        elif action == BrowserAction.CHECK:
            checked = args.get("checked")
            if type(checked) is not bool or not isinstance(checked, bool):
                raise ValueError("checked must be a boolean")
            await record.handle.set_checked(checked, timeout=5000)
            observed["checked"] = await record.handle.is_checked()
        else:
            raise ValueError("unsupported native input")
        await self._mark(invocation, "input_complete", dispatch="confirmed")
        self._revision += 1
        observed["delivery"] = "delivered"
        observed["effect_verified"] = False
        observed["input_state"] = input_state.payload()
        observed["text"] = ""
        if action == BrowserAction.FILL:
            actual: object = await _evaluate(record.handle, "el => 'value' in el ? el.value : el.textContent || ''")
            verified = actual == args["value"]
            observed.update(value_length=len(require_json_string(args["value"], "value")),
                            delivery="verified" if verified else "unverified", effect_verified=verified)
            return BrowserToolOutput("Filled and verified " + ref if verified else "Fill readback differed.", verified, observed)
        if baseline is not None:
            observed["query_baseline"] = baseline
        return BrowserToolOutput("Delivered " + action.value + " to " + ref, True, observed)

    def _control_baseline(self, args: JsonObject) -> Counter[tuple[str, str]]:
        baseline = args.get("baseline_controls")
        if not isinstance(baseline, list):
            raise ValueError("controls_changed requires its original observed controls")
        controls: Counter[tuple[str, str]] = Counter()
        for raw in baseline:
            row = decode_json_object(raw)
            if row.get("disabled") is not True:
                controls[(require_json_string(row["role"], "control role"), require_json_string(row["name"], "control name"))] += 1
        return controls

    async def _wait(self, invocation: Invocation, state: PageState) -> BrowserToolOutput:
        args = invocation.command.arguments
        condition = require_json_string(args["condition"], "condition")
        value = require_json_string(args.get("value", ""), "value")
        expected = args.get("expected_document")
        timeout_ms = require_json_integer(args.get("timeout_ms", 5000), "timeout_ms")
        if not 100 <= timeout_ms <= 30000:
            raise ValueError("invalid_wait_deadline")
        baseline_text = require_json_string(args.get("baseline_text"), "baseline_text") if condition == "text_changed" else ""
        if condition in {"text_changed", "controls_changed"} and not isinstance(expected, dict):
            raise ValueError(condition + " requires its original document")
        controls = self._control_baseline(args) if condition == "controls_changed" else Counter[tuple[str, str]]()
        deadline = min(invocation.command.deadline, time.monotonic() + timeout_ms / 1000)
        while time.monotonic() < deadline:
            if condition in {"popup", "dialog", "download"}:
                matched = (bool(self._popups.get(state.binding.target_id)) if condition == "popup" else
                    state.binding.target_id in (self._dialogs if condition == "dialog" else self._downloads))
                if matched:
                    if condition == "popup":
                        await self._register_popups()
                    return BrowserToolOutput("Observed the requested page event.", True,
                        {"action": "wait", "condition": condition, "matched": True, "page_id": state.binding.target_id})
                await asyncio.sleep(0.1)
                continue
            async with self._frame(state) as context:
                same = expected is None or context.document == expected
                if not same and condition not in {"url_contains", "new_document"}:
                    raise RuntimeError("wait_document_changed")
                matched = False
                if condition in {"text", "text_absent"}:
                    body: object = await _evaluate(context.frame, "() => document.body?.innerText || ''")
                    matched = (value in str(body)) == (condition == "text")
                elif condition in {"text_changed", "controls_changed"}:
                    if condition == "controls_changed":
                        current = await collect_ax_tree(context)
                        available = Counter((str(row.get("role", "")), " ".join(str(row.get("name", "")).split())[:240])
                            for row in current.values() if row.get("role") in AX_CONTROL_ROLES and row.get("disabled") is not True)
                        matched = any(count > controls[key] for key, count in available.items())
                    else:
                        observed_text: object = await _evaluate(context.frame, "(" + VISIBLE_TEXT + ")(document.body)")
                        matched = require_json_string(observed_text, "visible text") != baseline_text
                    async with self._frame(state) as after:
                        if not same_document(context.document, after.document):
                            raise RuntimeError("wait_document_changed")
                elif condition in {"selector", "selector_hidden", "selector_detached"}:
                    handle = await context.frame.query_selector(value)
                    matched = (handle is not None and await handle.is_visible()) if condition == "selector" else (
                        handle is None or not await handle.is_visible() if condition == "selector_hidden" else handle is None)
                    if handle is not None:
                        await handle.dispose()
                elif condition == "url_contains":
                    matched = value in context.frame.url
                elif condition == "new_document":
                    matched = expected is not None and not same
                else:
                    raise ValueError("unsupported_wait_condition")
                if matched:
                    return BrowserToolOutput("Wait condition matched.", True, {"action": "wait", "condition": condition,
                        "matched": True, "page_url": context.frame.url, "text": "", "document": context.document})
            await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return BrowserToolOutput("Wait condition timed out.", False,
            {"action": "wait", "condition": condition, "matched": False, "reason": "timeout"})

    async def _query_baseline(self, context: FrameContext, record: ElementRecord[ElementHandle],
                              result_ref: str) -> JsonObject:
        target: JsonObject | None = None
        if result_ref:
            selected = await self._resolve(context, result_ref, purpose="scope")
            target = {"backend_node_id": selected.backend_node_id, "relation": "observed-scope"}
        else:
            target = await bind_query_target(context, record.handle)
            if target != record.query_target:
                raise ValueError("query_target_changed")
            explicit: object = await _evaluate(record.handle, "el => el.hasAttribute('aria-controls') || el.matches('summary')")
            if target is None and explicit is not True:
                candidates = await context.frame.query_selector_all("article,[role=status],[role=region],[aria-live]")
                visible = [node for node in candidates if await node.is_visible()]
                if len(visible) == 1:
                    target = {"backend_node_id": await backend_node(context, visible[0]), "relation": "observed-result-region"}
                for node in candidates:
                    await node.dispose()
        regions = await self._query_regions(context, target)
        baseline: JsonObject = {"document": context.document, "regions": regions,
            "control": {"backend_node_id": record.backend_node_id}, "context": record.context}
        if target is not None:
            baseline["target"] = target
        return baseline

    async def _query_regions(self, context: FrameContext, target: JsonObject | None) -> list[JsonValue]:
        node = (await resolve_backend(context, require_json_integer(target["backend_node_id"], "backend node"))
                if target is not None else await context.frame.query_selector("body"))
        if node is None:
            raise RuntimeError("query_target_unavailable")
        try:
            raw: object = await _evaluate(node, """el => ({text:(el.innerText||'').replace(/\\s+/g,' ').trim().slice(0,100001),
                connected:el.isConnected && el.ownerDocument === document,
                visible:!!el.getClientRects().length && getComputedStyle(el).visibility!=='hidden',
                busy:!!el.closest('[aria-busy=true]') || !!el.querySelector('[aria-busy=true]')})""")
            row = decode_json_object(raw)
            if row.pop("connected") is not True:
                raise ValueError("query_target_detached")
            if len(require_json_string(row["text"], "query text")) > 100000:
                raise ValueError("query_region_exceeds_observation_budget")
            return [{"key": str(target["backend_node_id"]) if target is not None else "document-body", **row}]
        finally:
            await node.dispose()

    async def _query(self, invocation: Invocation, context: FrameContext) -> JsonValue:
        action, args = invocation.command.action, invocation.command.arguments
        if action == BrowserAction.RECORDS:
            from codey.automation.browser_observation import _RECORDS

            observed = await context.frame.evaluate_handle(_RECORDS, require_json_strings(args["labels"], "labels"))
            rows_handle, targets = await observed.get_property("rows"), await observed.get_property("targets")
            try:
                read_rows: Callable[[], Awaitable[object]] = rows_handle.json_value
                raw: object = await read_rows()
                if not isinstance(raw, list):
                    raise ValueError("invalid observed record regions")
                rows: list[JsonValue] = []
                for value in raw:
                    row = decode_json_object(value)
                    candidates = row.get("targets")
                    linked: list[JsonValue] = []
                    for candidate in candidates if isinstance(candidates, list) else []:
                        region = decode_json_object(candidate)
                        handle = await targets.get_property(str(region.pop("index")))
                        try:
                            node = handle.as_element()
                            if node is None:
                                raise ValueError("observed region is not an element")
                            region["target"] = {"backend_node_id": await backend_node(context, node),
                                                "relation": region.pop("relation")}
                            linked.append(region)
                        finally:
                            await handle.dispose()
                    rows.append({**row, "targets": linked})
                return rows
            finally:
                await rows_handle.dispose()
                await targets.dispose()
                await observed.dispose()
        baseline = decode_json_object(args["baseline"])
        if action == BrowserAction.MATCH_QUERY:
            control = baseline.get("control")
            if not isinstance(control, dict):
                return True
            record = await self._resolve(context, require_json_string(args["ref"], "ref"))
            return control.get("backend_node_id") == record.backend_node_id
        if not same_document(baseline.get("document"), context.document):
            raise RuntimeError("query_document_changed")
        raw_target = baseline.get("target")
        target = decode_json_object(raw_target) if isinstance(raw_target, dict) else None
        deadline = min(invocation.command.deadline, time.monotonic() + 2.5)
        while True:
            current: JsonObject = {"document": context.document, "regions": await self._query_regions(context, target)}
            changed = changed_query_region(baseline, current) if target is not None else ""
            if changed:
                captured = await self._capture(context)
                if (changed in " ".join(str(captured["text"]).split())
                        and current["regions"] == await self._query_regions(context, target)):
                    captured["query_confirmed"] = True
                    captured["query_region"] = {"text": changed, "visible": True, "context": baseline.get("context"),
                        "relation": target["relation"] if target is not None else "observed-document-change"}
                    return captured
            if time.monotonic() >= deadline:
                captured = await self._capture(context)
                captured["query_confirmed"] = False
                captured["query_pending"] = "query_update_unconfirmed"
                return captured
            await asyncio.sleep(0.1)

    async def _file_snapshot(self, context: FrameContext) -> BrowserToolOutput:
        self._file_controls.clear()
        self._file_document = context.document
        handles = await context.frame.query_selector_all("input[type=file]")
        rows: list[JsonValue] = []
        for handle in handles:
            if len(rows) >= 80:
                break
            if not await handle.is_visible():
                continue
            metadata, owner = await element_metadata(handle)
            node = await backend_node(context, handle)
            ref = self._registry.observe_node(ElementRecord(handle, context.document, node,
                "file_input", " ".join(str(metadata.get("name") or "").split())[:240],
                None, owner, None, self._revision, semantic_fingerprint(metadata)))
            self._file_controls[ref] = {"id": ref, **await file_control_details(handle)}
            rows.append(self._file_controls[ref])
        truncated = len(handles) > 80
        self._file_controls_truncated = truncated
        value: JsonObject = {"page_id": context.document["target_id"], "page_url": context.frame.url,
            "generation": self._revision, "scope": "file_inputs", "complete": not truncated,
            "text_truncated": False, "elements_truncated": truncated, "elements": [], "contexts": [],
            "file_controls": rows, "text": json.dumps(rows, ensure_ascii=False)}
        return BrowserToolOutput(value["text"] if isinstance(value["text"], str) else "", True, value)

    @staticmethod
    async def _viewport(page: Page) -> JsonObject:
        raw: object = await _evaluate(page, """() => ({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,
            scale:visualViewport?.scale||1,offset_x:visualViewport?.offsetLeft||0,offset_y:visualViewport?.offsetTop||0})""")
        return decode_json_object(raw)

    @staticmethod
    async def _capture_png(page: Page, clip: JsonObject) -> bytes:
        cdp = await page.context.new_cdp_session(page)
        try:
            capture_args: JsonObject = {"format": "png", "fromSurface": True,
                "captureBeyondViewport": True, "clip": {**clip, "scale": 1}}
            raw: object = await _send(cdp, "Page.captureScreenshot", capture_args)
            data = decode_json_object(raw)
            return base64.b64decode(require_json_string(data["data"], "captured pixels"), validate=True)
        finally:
            await cdp.detach()

    async def _screenshot(self, invocation: Invocation, state: PageState, context: FrameContext) -> tuple[JsonValue, bytes]:
        args = invocation.command.arguments
        await _evaluate(state.page, "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(null))))")
        viewport = await self._viewport(state.page)
        full_page = args.get("full_page") is True
        clip: JsonObject = {"x": viewport["x"], "y": viewport["y"], "width": viewport["width"], "height": viewport["height"]}
        if context.frame is not state.page.main_frame:
            owner = await context.frame.frame_element()
            box = await owner.bounding_box()
            await owner.dispose()
            if box is None:
                raise ValueError("frame_not_visible")
            clip = decode_json_object(box)
            clip["x"] = image_number(clip["x"]) + image_number(viewport["x"])
            clip["y"] = image_number(clip["y"]) + image_number(viewport["y"])
            full_page = False
        elif full_page:
            raw_size: object = await _evaluate(context.frame, "() => ({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})")
            size = decode_json_object(raw_size)
            clip.update(x=0, y=0, width=size["width"], height=size["height"])
        if "clip" in args:
            clip = decode_json_object(args["clip"])
            if set(clip) != {"x", "y", "width", "height"}:
                raise ValueError("invalid screenshot clip")
            full_page = False
        width, height = image_number(clip["width"]), image_number(clip["height"])
        if min(width, height) <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("screenshot exceeds pixel budget; choose a smaller region")
        self._revision += 1
        observed = await collect_snapshot(context, self._registry, revision=self._revision)
        png = await self._capture_png(state.page, clip)
        if await self._viewport(state.page) != viewport:
            raise ValueError("screenshot_viewport_changed")
        async with self._frame(state) as after:
            if not same_document(context.document, after.document):
                raise ValueError("screenshot_document_changed")
        rendered, mapping, pixel_width, pixel_height = annotate_capture(png, observed, clip,
            annotate=args.get("annotate", True) is True, scroll_x=image_number(viewport["x"]), scroll_y=image_number(viewport["y"]))
        from codey.automation.browser_ipc import MAX_BLOB_BYTES

        if len(rendered) > MAX_BLOB_BYTES:
            raise ValueError("screenshot_exceeds_transfer_budget")
        identity = "visual-" + uuid4().hex
        visual: JsonObject = {"observation_ref": identity, "document": context.document, "viewport": viewport,
            "clip": clip, "full_page": full_page, "raw_sha256": pixel_digest(png),
            "sha256": hashlib.sha256(rendered).hexdigest(), "width": pixel_width, "height": pixel_height,
            "media_type": "image/png", "scale_x": pixel_width / width, "scale_y": pixel_height / height, "mapping": mapping}
        self._visual[identity] = visual
        while len(self._visual) > 4:
            del self._visual[next(iter(self._visual))]
        self._observations[self._revision] = observed
        while len(self._observations) > 4:
            del self._observations[next(iter(self._observations))]
        value = {**observed, "image": visual}
        return BrowserToolOutput("Captured the current browser image with observed control mappings.", True, value).payload(), rendered

    async def _visual_click(self, invocation: Invocation, state: PageState, context: FrameContext) -> BrowserToolOutput:
        args = invocation.command.arguments
        identity = require_json_string(args["observation_ref"], "observation_ref")
        visual = self._visual.get(identity)
        if visual is None:
            raise ValueError("visual_observation_unavailable")
        if visual["document"] != context.document or visual["viewport"] != await self._viewport(state.page):
            raise ValueError("visual_observation_stale")
        x, y = image_number(args["x"]), image_number(args["y"])
        if not 0 <= x < image_number(visual["width"]) or not 0 <= y < image_number(visual["height"]):
            raise ValueError("visual_point_outside_image")
        clip = decode_json_object(visual["clip"])
        current = await self._capture_png(state.page, clip)
        if pixel_digest(current) != visual["raw_sha256"]:
            raise ValueError("visual_pixels_changed")
        viewport = decode_json_object(visual["viewport"])
        x /= image_number(visual["scale_x"])
        y /= image_number(visual["scale_y"])
        offset_x = image_number(clip["x"]) - image_number(viewport["x"])
        offset_y = image_number(clip["y"]) - image_number(viewport["y"])
        if not 0 <= x + offset_x < image_number(viewport["width"]) or not 0 <= y + offset_y < image_number(viewport["height"]):
            raise ValueError("visual_point_outside_viewport; scroll and capture a fresh image")
        await self._mark(invocation, "visual_input_dispatch", dispatch="unknown")
        await state.page.mouse.click(x + offset_x, y + offset_y)
        await self._mark(invocation, "visual_input_complete", dispatch="confirmed")
        self._revision += 1
        return BrowserToolOutput("Delivered the click in the verified image space.", True,
            {"action": "visual_click", "observation_ref": identity, "delivery": "delivered", "effect_verified": False})

    async def _visual_or_file(self, invocation: Invocation, state: PageState, context: FrameContext) -> tuple[JsonValue, bytes]:
        action, args = invocation.command.action, invocation.command.arguments
        if action == BrowserAction.SCREENSHOT:
            return await self._screenshot(invocation, state, context)
        if action == BrowserAction.VISUAL_CLICK:
            return (await self._visual_click(invocation, state, context)).payload(), b""
        if action == BrowserAction.UPLOAD:
            return (await self._upload(invocation, context)).payload(), b""
        if action != BrowserAction.DOWNLOAD:
            raise ValueError("unsupported browser action")
        ref = require_json_string(args.get("ref", ""), "ref")
        record = await self._resolve(context, ref)
        await self._mark(invocation, "download_dispatch", dispatch="unknown")
        async with state.page.expect_download(timeout=15000) as pending:
            await record.handle.click(timeout=5000, no_wait_after=True)
            await self._mark(invocation, "download_click_complete", dispatch="confirmed")
        download = await pending.value
        path = await download.path()
        if path is None:
            raise ValueError("download_file_unavailable")
        from codey.automation.browser_ipc import MAX_BLOB_BYTES

        with path.open("rb") as stream:
            binary = stream.read(MAX_BLOB_BYTES + 1)
        if len(binary) > MAX_BLOB_BYTES:
            raise ValueError("download_exceeds_transfer_budget")
        await self._mark(invocation, "download_complete", dispatch="confirmed")
        value: JsonObject = {"action": "download", "ref": ref, "delivery": "delivered", "suggested_filename": download.suggested_filename,
                 "bytes": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}
        return BrowserToolOutput("Downloaded the selected file.", True, value).payload(), binary

    async def _file_control_ref(self, context: FrameContext, target: JsonObject) -> str:
        selector = "id" if "id" in target else "name"
        matches = [(ref, row) for ref, row in self._file_controls.items() if row[selector] == target[selector]]
        if len(matches) != 1 or (selector == "name" and self._file_controls_truncated):
            candidates = [row for _, row in matches] or list(self._file_controls.values())
            raise ValueError("Choose one file input using a current observed ID. Candidates: "
                             + json.dumps(candidates, ensure_ascii=False))
        ref, observed = matches[0]
        record = await self._resolve(context, ref)
        current = await file_control_details(record.handle)
        if current != {key: value for key, value in observed.items() if key != "id"}:
            raise RuntimeError("file_observation_expired")
        return ref

    async def _upload(self, invocation: Invocation, context: FrameContext) -> BrowserToolOutput:
        args = invocation.command.arguments
        ref = require_json_string(args.get("ref", ""), "ref")
        expected = args.get("expected_document")
        if expected is not None and expected != context.document:
            raise ValueError("file_observation_expired" if "target" in args else "upload_document_changed")
        if not ref:
            if self._file_document != context.document:
                raise ValueError("file_observation_expired")
            try:
                ref = await self._file_control_ref(context, decode_json_object(args["target"]))
            except ValueError as exc:
                return BrowserToolOutput("ERROR: " + str(exc), False,
                    {"action": "upload", "ref": "", "delivery": "not_delivered", "reason": "file_target_unavailable"})
        record = await self._resolve(context, ref)
        await self._mark(invocation, "file_selection_dispatch", dispatch="unknown")
        name, mime = require_json_string(args["name"], "name"), require_json_string(args["mime_type"], "mime_type")
        await record.handle.set_input_files({"name": name, "mimeType": mime, "buffer": invocation.binary}, timeout=5000)
        await self._mark(invocation, "file_selection_complete", dispatch="confirmed")
        raw: object = await _evaluate(record.handle, "el => Array.from(el.files||[],f=>({name:f.name,bytes:f.size,type:f.type}))")
        files = decode_json_object({"files": raw})["files"]
        verified = files == [{"name": name, "bytes": len(invocation.binary), "type": mime}]
        self._revision += 1
        # File selection readback proves the input state, not server acceptance.
        value: JsonObject = {"action": "upload", "ref": ref, "delivery": "verified" if verified else "unverified",
            "effect_verified": False, "name": name, "media_type": mime, "bytes": len(invocation.binary),
            "sha256": hashlib.sha256(invocation.binary).hexdigest()}
        return BrowserToolOutput("Selected and verified the imported attachment." if verified else "File readback differed.", verified, value)

    async def _resolve(self, context: FrameContext, ref: str, *, purpose: Literal["action", "scope", "inspect"] = "action") -> ElementRecord[ElementHandle]:
        record = self._registry.resolve(ref, context.document)
        connected: object = await _evaluate(record.handle, "el => el.isConnected && el.ownerDocument === document")
        if connected is not True:
            raise ValueError("stale_ref")
        metadata, owner = await element_metadata(record.handle)
        try:
            observed = parse_json_object(semantic_fingerprint(metadata))
            expected = parse_json_object(record.semantics_json)
            if purpose == "scope" and metadata.get("actionable") is False:
                observed.pop("name")
                expected.pop("name")
            if observed != expected:
                raise ValueError("stale_ref")
            if record.context_backend_node_id and (owner is None or await backend_node(context, owner) != record.context_backend_node_id):
                raise ValueError("stale_ref")
            if purpose == "action" and (metadata.get("disabled") is True or not await record.handle.is_enabled()):
                raise ValueError("disabled_control")
            if not await record.handle.is_visible():
                raise ValueError("control_not_visible")
            return record
        finally:
            if owner is not None:
                await owner.dispose()

    async def binding(self) -> JsonObject | None:
        if self._resume_binding is not None:
            return self._resume_binding.payload()
        if self._browser is None and not self._endpoint:
            return None
        # A lost target remains part of the recorded identity; recovery must reject
        # its loss rather than select another page or emit an invalid binding.
        pages = tuple(state.binding for state in self._pages.values())
        return BrowserBinding(self._port, browser_identity(self._endpoint), self._active, pages,
                              self._pages[self._active].selected_frame if self._active else "", self._owns_browser,
                              str(self._profile) if self._profile is not None else "").payload()

    async def close(self, *, release: bool = True) -> None:
        if release and self._browser is None and self._resume_binding is not None:
            with contextlib.suppress(Error, OSError, RuntimeError, ValueError):
                await self._connect()
        if self._browser is not None:
            await self._register_popups()
            for target, inputs in self._input_states.items():
                if inputs.keys and target in self._pages:
                    with contextlib.suppress(Error, TimeoutError):
                        await inputs.release(self._pages[target].page)
            for state in tuple(self._pages.values()):
                if release and state.binding.owned and not state.page.is_closed():
                    with contextlib.suppress(Error):
                        await state.page.close(run_before_unload=False)
            if release and self._owns_browser:
                with contextlib.suppress(Error):
                    cdp = await self._browser.new_browser_cdp_session()
                    await _send(cdp, "Browser.close")
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if release and self._process is not None and self._process.poll() is None:
            try:
                await asyncio.to_thread(self._process.wait, timeout=3)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                await asyncio.to_thread(self._process.wait, timeout=2)
        if release and self._profile is not None:
            deadline = time.monotonic() + 3
            while self._profile.exists():
                try:
                    await asyncio.to_thread(shutil.rmtree, self._profile)
                except FileNotFoundError:
                    # Chromium may remove its own lockfiles during recursive
                    # cleanup. Retry the remaining tree on Python 3.11/3.12.
                    if time.monotonic() >= deadline and self._profile.exists():
                        raise
                except OSError as exc:
                    # Browser.close acknowledges shutdown before Chromium's
                    # cache processes release handles and finish their writes.
                    if (not isinstance(exc, PermissionError) and exc.errno != errno.ENOTEMPTY) or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.1)
