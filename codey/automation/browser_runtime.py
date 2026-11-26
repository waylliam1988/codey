"""The task browser's async Playwright owner, hosted only in its worker process."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from playwright.async_api import Browser, Dialog, Download, ElementHandle, Error, Page, Playwright, async_playwright

from codey.automation.browser import CONTROLLED_BROWSER_FLAGS
from codey.automation.browser_binding import BrowserBinding, PageBinding, browser_endpoint, browser_identity
from codey.automation.browser_cleanup import remove_profile, stop_process
from codey.automation.browser_contracts import (
    BrowserAction,
    BrowserCommand,
    BrowserCompletion,
    BrowserConfig,
    BrowserToolOutput,
)
from codey.automation.browser_document import same_document
from codey.automation.browser_frames import FrameContext, backend_node, frame_context
from codey.automation.browser_input import _HIT_TEST
from codey.automation.browser_ipc import BrowserPacket
from codey.automation.browser_native_input import NativeInput
from codey.automation.browser_network import BrowserNetwork, NetworkPolicy
from codey.automation.browser_observation import VISIBLE_TEXT
from codey.automation.browser_query import QueryObservationError
from codey.automation.browser_query_async import bind_query_target
from codey.automation.browser_refs import ElementRecord
from codey.automation.browser_runtime_media import BrowserMedia
from codey.automation.browser_runtime_query import BrowserQueries
from codey.automation.browser_runtime_state import Invocation, ObservationState, PageState, Progress, _evaluate, _send
from codey.automation.browser_snapshot import (
    collect_snapshot,
    diff_snapshot,
    element_metadata,
    element_role,
    semantic_fingerprint,
    snapshot_display,
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

_PROPERTY_RETURN = re.compile(r"\s*return[ \t]+(window\.[A-Za-z_$][\w$]*)(?:\s*;)?\s*", re.ASCII)


class BrowserRuntime:
    def __init__(self, config: BrowserConfig, *, network_policy: NetworkPolicy | None = None,
                 progress: Progress | None = None, binding: BrowserBinding | None = None,
                 inputs: JsonObject | None = None) -> None:
        self.config, self.progress = config, progress
        self._network = BrowserNetwork(network_policy)
        self._resume_binding = binding
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._profile: Path | None = None
        self._endpoint = ""
        self._port = config.cdp_port
        self._pages: dict[str, PageState] = {}
        self._active = ""
        self._observation = ObservationState()
        self._dialogs: dict[str, Dialog] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._owns_browser = False
        self._connection_lock = asyncio.Lock()
        self._popups: dict[str, list[Page]] = {}
        self._downloads: dict[str, Download] = {}
        self._input_states = {target: NativeInput.read(value) for target, value in (inputs or {}).items()}
        self._documents: dict[tuple[str, str], JsonObject] = {}
        self._media = BrowserMedia(self._observation, frame=self._frame, resolve=self._resolve, mark=self._mark)
        self._queries = BrowserQueries(frame=self._frame, resolve=self._resolve, capture=self._capture,
            popups=self._popups, dialogs=self._dialogs, downloads=self._downloads,
            register_popups=self._register_popups)

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
            await self._network.guard(context)
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
        args = [executable, *CONTROLLED_BROWSER_FLAGS, "--disable-background-networking",
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
        self._network.watch(page, binding.target_id)
        return state

    async def _register_popups(self) -> None:
        while True:
            registered = {state.page for state in self._pages.values()}
            pending = [page for pages in self._popups.values() for page in pages
                       if page not in registered and not page.is_closed()]
            if not pending:
                return
            for page in pending:
                if not page.is_closed():
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
        await self._network.guard(context)
        seed = next((page for page in context.pages if page.url == "about:blank"), None) if self._owns_browser else None
        page = seed if seed is not None else await context.new_page()
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
                "completion": BrowserCompletion.COMPLETED.value, "generation": self._observation.revision, "binding": await self.binding()}, blob)
        except (asyncio.CancelledError, TimeoutError, Error, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
            reason = (exc.reason if isinstance(exc, QueryObservationError) else "cancelled"
                      if isinstance(exc, asyncio.CancelledError) else "deadline" if isinstance(exc, TimeoutError) else str(exc))
            delivery = {"not_sent": "not_delivered", "unknown": "uncertain", "confirmed": "delivered"}[invocation.dispatch_state]
            output = BrowserToolOutput("ERROR: " + (str(exc) if isinstance(exc, QueryObservationError) else reason), False,
                {"action": command.action.value,
                "ref": command.arguments.get("ref", ""), "reason": reason, "phase": invocation.phase, "delivery": delivery,
                "document": invocation.document, "query_baseline": invocation.query_baseline,
                "input_state": invocation.input_state.payload() if invocation.input_state is not None else None})
            if reason == "browser_not_bound":
                output = BrowserToolOutput("ERROR: An HTTP-first source read does not bind the browser. "
                    "Use browser_navigate to explicitly open the authorized page, then observe it.", False, output.observation)
            return BrowserPacket({"type": "result", "operation_id": command.operation_id, "value": output.payload(),
                "completion": BrowserCompletion.FAILED.value, "generation": self._observation.revision, "binding": await self.binding()})
        finally:
            for handle in self._observation.registry.retired_handles():
                with contextlib.suppress(Error):
                    await handle.dispose()

    async def _state(self, invocation: Invocation, *, create: bool = False) -> PageState:
        command = invocation.command
        if command.target_id:
            await self._connect(invocation)
            state = self._pages.get(command.target_id)
            if state is None or state.page.is_closed():
                raise RuntimeError("original_browser_target_lost")
            return PageState(state.page, state.binding,
                "" if command.arguments.get("main_frame") is True else command.frame_id or state.selected_frame)
        state = await self._current(invocation, create=create)
        return PageState(state.page, state.binding) if command.arguments.get("main_frame") is True else state

    async def _execute(self, invocation: Invocation) -> tuple[JsonValue, bytes]:
        action = invocation.command.action
        if action == BrowserAction.BINDING:
            return await self.binding(), b""
        if action == BrowserAction.RESET_REFS:
            self._observation.registry.invalidate_all()
            self._media.reset_refs()
            self._observation.observations.clear()
            self._observation.revision += 1
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
        if action == BrowserAction.DIAGNOSTICS:
            return self._network.diagnostics(state.binding.target_id,
                clear=invocation.command.arguments.get("clear") is True), b""
        if action == BrowserAction.SUBMIT_FORM:
            return (await self._submit_form(invocation, state)).payload(), b""
        if action == BrowserAction.WAIT:
            expected = invocation.command.arguments.get("expected_document")
            if isinstance(expected, dict) and isinstance(expected.get("frame_id"), str):
                state = PageState(state.page, state.binding, str(expected["frame_id"]))
            return (await self._queries._wait(invocation, state)).payload(), b""
        async with self._observed_frame(state, invocation) as context:
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
                return await self._queries._query(invocation, context), b""
            return await self._media._visual_or_file(invocation, state, context)

    @asynccontextmanager
    async def _observed_frame(self, state: PageState, invocation: Invocation) -> AsyncGenerator[FrameContext, None]:
        ref = invocation.command.arguments.get("ref")
        async with self._frame(state) as context:
            parent = next(reversed(self._observation.observations.values()), None)
            record = self._observation.registry.records.get(ref) if isinstance(ref, str) else None
            if (record is None or parent is None or parent.get("document") != context.document
                    or same_document(context.document, record.document)):
                yield context
                return
            frames = parent.get("frames")
            if isinstance(frames, list):
                for raw in frames:
                    row = decode_json_object(raw)
                    if row.get("document") != record.document or not isinstance(elements := row.get("elements"), list):
                        continue
                    if any(isinstance(element, dict) and element.get("ref") == ref for element in elements):
                        frame_id = require_json_string(row["frame_id"], "frame id")
                        selected = PageState(state.page, state.binding, frame_id)
                        async with self._frame(selected) as child:
                            self._observation.registry.resolve(require_json_string(ref, "ref"), child.document)
                            yield child
                            if invocation.dispatch_state == "confirmed":
                                state.selected_frame = frame_id
                            return
            yield context

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
            self._observation.revision += 1
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
                self._observation.revision += 1
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
        if self.config.bring_to_front:
            await state.page.bring_to_front()
        self._network.clear(state.binding.target_id)
        await self._mark(invocation, "navigation_dispatch", dispatch="unknown")
        response = await state.page.goto(url, wait_until="domcontentloaded",
            timeout=max(1, min(20_000, int((invocation.command.deadline - time.monotonic()) * 1000))))
        await self._mark(invocation, "navigation_complete", dispatch="confirmed")
        self._pages[state.binding.target_id].selected_frame = ""
        self._observation.revision += 1
        headers = await response.all_headers() if response is not None else {}
        facts: JsonObject = {"http_status": response.status if response is not None else None,
                             "content_type": headers.get("content-type", "")}
        return BrowserToolOutput("Opened " + state.page.url, True, {"action": action.value, "url": state.page.url,
            "previous_url": previous, "page_id": state.binding.target_id, "generation": self._observation.revision,
            **facts, "delivery": "delivered"})

    async def _observe(self, invocation: Invocation, context: FrameContext) -> BrowserToolOutput:
        action, args = invocation.command.action, invocation.command.arguments
        self._observation.revision += 1
        if action == BrowserAction.SNAPSHOT and args.get("file_inputs_only") is True:
            return await self._media._file_snapshot(context)
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
                ref = self._observation.registry.observe_node(ElementRecord(handle, context.document, await backend_node(context, handle),
                    role, name, decode_json_object(metadata["context"]) if isinstance(metadata.get("context"), dict) else None,
                    owner, await bind_query_target(context, handle) if metadata.get("query_relation") else None,
                    self._observation.revision, semantic_fingerprint(metadata), await backend_node(context, owner) if owner is not None else 0))
                rows.append({"ref": ref, "role": role, "name": name, "text": str(text)[:500], "placeholder": placeholder})
            return BrowserToolOutput(json.dumps(rows, ensure_ascii=False), True,
                {"action": "locate", "candidates": rows, "unique": len(rows) == 1, "document": context.document})
        scope_ref = require_json_string(args.get("scope_ref", ""), "scope_ref")
        if scope_ref:
            scope_ref = await self._snapshot_scope_ref(context, scope_ref)
            await self._resolve(context, scope_ref, purpose="scope")
        selector = require_json_string(args.get("selector", ""), "selector")
        current = await collect_snapshot(context, self._observation.registry, revision=self._observation.revision,
            max_elements=require_json_integer(args.get("max_elements", 80), "max_elements"),
            max_text=require_json_integer(args.get("max_text", 12000), "max_text"),
            scope_ref=scope_ref, selector=selector)
        if not (scope_ref or selector) and context.frame.child_frames:
            current["frames"] = await self._snapshot_child_frames(context, current)
        base = args.get("base_revision")
        value = diff_snapshot(self._observation.observations.get(base) if type(base) is int and isinstance(base, int) else None, current)
        self._observation.observations[self._observation.revision] = current
        while len(self._observation.observations) > 4:
            del self._observation.observations[next(iter(self._observation.observations))]
        # The receipt retains the full observation; the model sees its derived delta.
        receipt = {**current, **{key: value[key] for key in ("kind", "base_revision", "added", "changed", "removed") if key in value}}
        hint = ("\nControls are truncated. Inspect a listed container with browser_snapshot(scope_ref=ref) "
                "or use a unique selector to narrow the scope." if current["elements_truncated"] is True else "")
        display = json.dumps(snapshot_display(value), ensure_ascii=False, separators=(",", ":"))
        return BrowserToolOutput(display + hint, True, receipt)

    async def _snapshot_child_frames(self, context: FrameContext, parent: JsonObject) -> list[JsonValue]:
        scope = decode_json_object(parent["scope"])
        remaining_elements = require_json_integer(scope["max_elements"], "snapshot control limit")
        remaining_text = require_json_integer(scope["max_text"], "snapshot text limit")
        rows = parent["elements"]
        assert isinstance(rows, list)
        remaining_elements -= len(rows)
        remaining_text -= len(require_json_string(parent["text"], "snapshot text"))
        frames: list[JsonValue] = []
        page = context.frame.page
        target = require_json_string(context.document["target_id"], "snapshot target")
        for child in context.frame.child_frames[:4]:
            try:
                async with frame_context(page, child, target) as frame:
                    row: JsonObject = {"frame_id": frame.document["frame_id"], "name": child.name,
                                       "document": frame.document}
                    if remaining_elements > 0 and remaining_text > 0:
                        preview = await collect_snapshot(frame, self._observation.registry, revision=self._observation.revision,
                            max_elements=min(remaining_elements, 8), max_text=min(remaining_text, 1200))
                        elements, body = preview["elements"], require_json_string(preview["text"], "frame text")
                        assert isinstance(elements, list)
                        remaining_elements -= len(elements)
                        remaining_text -= len(body)
                        row.update(elements=elements, text=body, complete=preview["complete"])
                    frames.append(row)
            except (Error, RuntimeError, ValueError):
                if not child.is_detached():
                    raise
        return frames

    async def _snapshot_scope_ref(self, context: FrameContext, ref: str) -> str:
        observed = self._observation.registry.observed_context(ref, context.document) if ref.startswith("region-") else None
        if observed is None or (handle := observed.context_node) is None:
            return ref
        metadata, owner = await element_metadata(handle)
        if owner is not None:
            await owner.dispose()
        return self._observation.registry.observe_node(ElementRecord(
            handle, context.document, observed.context_backend_node_id, element_role(metadata),
            str((observed.context or {}).get("label", "")), None, None, None, self._observation.revision,
            semantic_fingerprint(metadata), scope_only=True,
        ))

    async def _capture(self, context: FrameContext) -> JsonObject:
        raw: object = await _evaluate(context.frame, """() => ({url:location.href,title:document.title,
            text:(document.body?.innerText||'').slice(0,100001),content_type:document.contentType,
            http_status:performance.getEntriesByType('navigation')[0]?.responseStatus||null,
            password_visible:Array.from(document.querySelectorAll('input[type=password]')).some(el=>el.getClientRects().length),
            render_pending:Array.from(document.querySelectorAll('app-root,#root,#app,#__next')).some(el=>
                !el.innerText.trim() || /^(loading|please wait|加载中|正在加载)[. 。…!]*$/i.test(el.innerText.trim()))})""")
        data = decode_json_object(raw)
        visible_text: object = await _evaluate(context.frame, "(" + VISIBLE_TEXT + ")(document.body, true)")
        data["text"] = require_json_string(visible_text, "visible text")
        text = require_json_string(data["text"], "text")
        target = require_json_string(context.document["target_id"], "target id")
        facts = self._network.response_facts(target, require_json_string(data["url"], "capture URL"))
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
        self._observation.revision += 1
        return BrowserToolOutput("Scrolled the observed container." if ref else "Scrolled the page.", True,
            {"action": "scroll", "ref": ref, "delivery": "delivered", "effect_verified": False,
             "page_url": context.document["url"], "text": ""})

    async def _submit_form(self, invocation: Invocation, state: PageState) -> BrowserToolOutput:
        args = invocation.command.arguments
        async with self._frame(PageState(state.page, state.binding)) as observed:
            expected = decode_json_object(args["expected_document"])
            if not same_document(observed.document, expected):
                raise ValueError("search_document_changed")
            invocation.document = observed.document
        timeout = max(1, int((invocation.command.deadline - time.monotonic()) * 1000))
        query = state.page.locator(require_json_string(args["query_selector"], "query selector")).first
        submit_selector = require_json_string(args["submit_selector"], "submit selector")
        submit = state.page.locator(":is(" + submit_selector + "):visible").first
        await self._mark(invocation, "form_fill", dispatch="unknown")
        await query.fill(require_json_string(args["value"], "query"), timeout=timeout)
        # Input handlers may navigate. Do not submit a different document.
        async with self._frame(PageState(state.page, state.binding)) as observed:
            if not same_document(observed.document, expected):
                raise ValueError("search_document_changed")
        await self._mark(invocation, "form_submit", dispatch="unknown")
        await submit.click(timeout=max(1, min(5000, int((invocation.command.deadline - time.monotonic()) * 1000))))
        await self._mark(invocation, "form_submitted", dispatch="confirmed")
        state.selected_frame = ""
        self._observation.revision += 1
        return BrowserToolOutput("Submitted the observed form.", True, {"delivery": "delivered"})

    async def _input(self, invocation: Invocation, state: PageState, context: FrameContext) -> BrowserToolOutput:
        action, args = invocation.command.action, invocation.command.arguments
        if action == BrowserAction.EVALUATE:
            await self._mark(invocation, "script_dispatch", dispatch="unknown")
            code = require_json_string(args["code"], "code")
            property_return = _PROPERTY_RETURN.fullmatch(code)
            raw: object = await _evaluate(context.frame, property_return[1] if property_return else code)
            await self._mark(invocation, "script_complete", dispatch="confirmed")
            value = decode_json_object({"value": raw})["value"]
            self._observation.revision += 1
            return BrowserToolOutput(json.dumps(value, ensure_ascii=False), True,
                {"action": "eval", "value": value, "truncated": False, "delivery": "delivered",
                 "document": context.document})
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
                {"action": action.value, "ref": ref, "delivery": "not_delivered", "document": context.document,
                 "hit_frame_depth": 0, "hit_frame_url": context.frame.url, **ready})
        ancestor = context.frame
        depth = 0
        while (parent := ancestor.parent_frame) is not None:
            depth += 1
            owner = await ancestor.frame_element()
            try:
                outer: object = await _evaluate(owner, _HIT_TEST)
                readiness = decode_json_object(outer)
                if readiness.get("ready") is not True:
                    return BrowserToolOutput("Input not dispatched: enclosing frame is occluded.", False,
                        {"action": action.value, "ref": ref, "delivery": "not_delivered", "document": context.document,
                         "hit_frame_depth": depth, "hit_frame_url": parent.url, **readiness})
            finally:
                await owner.dispose()
            ancestor = parent
        baseline: JsonObject | None = None
        if action == BrowserAction.CLICK and args.get("query") is True:
            baseline = await self._queries._query_baseline(context, record, require_json_string(args.get("result_ref", ""), "result_ref"))
            invocation.query_baseline = baseline
        previous_popups = len(self._popups.get(state.binding.target_id, ()))
        await self._mark(invocation, "input_dispatch", dispatch="unknown")
        observed: JsonObject = {"action": action.value, "ref": ref, "page_url": context.document["url"]}
        if action == BrowserAction.CLICK:
            observed.update(await self._click_control(record, ready, baseline))
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
        self._observation.revision += 1
        observed["delivery"] = "delivered"
        observed["effect_verified"] = False
        observed["input_state"] = input_state.payload()
        observed["text"] = ""
        if action in {BrowserAction.CLICK, BrowserAction.PRESS}:
            popups = self._popups.get(state.binding.target_id, [])[previous_popups:previous_popups + 8]
            if popups:
                observed["opened_surfaces"] = [{"kind": "popup"} for popup in popups if not popup.is_closed()]
        if action == BrowserAction.FILL:
            actual: object = await _evaluate(record.handle, "el => 'value' in el ? el.value : el.textContent || ''")
            verified = actual == args["value"]
            observed.update(value_length=len(require_json_string(args["value"], "value")),
                            delivery="verified" if verified else "unverified", effect_verified=verified)
            return BrowserToolOutput("Filled and verified " + ref if verified else "Fill readback differed.", verified, observed)
        if action == BrowserAction.CHECK:
            verified = observed["checked"] == args["checked"]
            observed.update(delivery="verified" if verified else "unverified", effect_verified=verified)
            return BrowserToolOutput("Checked state verified " + ref if verified else "Check readback differed.", verified, observed)
        surface_hint = " A popup opened. Use browser_tab_list to find it." if observed.get("opened_surfaces") else ""
        return BrowserToolOutput("Delivered " + action.value + " to " + ref + surface_hint, True, observed)

    async def _click_control(self, record: ElementRecord[ElementHandle], ready: JsonObject,
                             baseline: JsonObject | None) -> JsonObject:
        position = decode_json_object(ready["position"])
        x, y = position["x"], position["y"]
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("invalid verified click position")
        navigation: object = False
        if record.role == "link":
            navigation = await _evaluate(record.handle, """el => {
                if (!el.matches('a[href]:not([download])')) return false;
                const target = (el.target || document.querySelector('base[target]')?.target || '_self').toLowerCase();
                const url = new URL(el.href, document.baseURI);
                const current = new URL(document.URL);
                url.hash = current.hash = '';
                return target === '_self' && /^https?:$/.test(url.protocol) && url.href !== current.href;
            }""")
        await record.handle.click(timeout=5000, no_wait_after=True, position={"x": x, "y": y})
        observed: JsonObject = {"query_baseline": baseline} if baseline is not None else {}
        if navigation is True:
            observed["link_origin"] = record.document
        return observed

    async def _resolve(self, context: FrameContext, ref: str, *, purpose: Literal["action", "scope", "inspect"] = "action") -> ElementRecord[ElementHandle]:
        record = self._observation.registry.resolve(ref, context.document)
        if record.scope_only and purpose == "action":
            raise ValueError("scope_ref_is_observation_only")
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
        async with contextlib.AsyncExitStack() as cleanup:
            if release:
                cleanup.push_async_callback(remove_profile, self._profile)
                cleanup.push_async_callback(stop_process, self._process)
            if self._playwright is not None:
                cleanup.push_async_callback(self._playwright.stop)
            if self._browser is not None:
                cleanup.push_async_callback(self._browser.close)
                try:
                    await self._network.close()
                finally:
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
