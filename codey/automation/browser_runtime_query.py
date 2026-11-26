"""BrowserQueries: browser actions using explicit runtime state and callbacks."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from playwright.async_api import Dialog, Download, ElementHandle, Error, Page

from codey.automation.browser_contracts import BrowserAction, BrowserToolOutput
from codey.automation.browser_document import same_document
from codey.automation.browser_frames import FrameContext, backend_node, resolve_backend
from codey.automation.browser_observation import VISIBLE_TEXT
from codey.automation.browser_query import QueryObservationError, changed_query_region
from codey.automation.browser_query_async import bind_query_target
from codey.automation.browser_refs import ElementRecord
from codey.automation.browser_runtime_state import FrameAccess, Invocation, PageState, ResolveElement, _evaluate
from codey.automation.browser_snapshot import available_control_counts, sample_available_controls
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)


class BrowserQueries:

    def __init__(self, *, frame: FrameAccess, resolve: ResolveElement,
                 capture: Callable[[FrameContext], Awaitable[JsonObject]],
                 popups: dict[str, list[Page]], dialogs: dict[str, Dialog],
                 downloads: dict[str, Download], register_popups: Callable[[], Awaitable[None]]) -> None:
        self._frame = frame
        self._resolve = resolve
        self._capture = capture
        self._popups = popups
        self._dialogs = dialogs
        self._downloads = downloads
        self._register_popups = register_popups

    def _control_baseline(self, args: JsonObject) -> Counter[tuple[str, str, str]]:
        baseline = args.get("baseline_controls")
        if not isinstance(baseline, list):
            raise ValueError("controls_changed requires its original observed controls")
        return available_control_counts(decode_json_object(raw) for raw in baseline)

    @asynccontextmanager
    async def _wait_frame(self, state: PageState, expected: JsonValue) -> AsyncGenerator[FrameContext, None]:
        try:
            async with self._frame(state) as context:
                yield context
        except Error as error:
            # Navigation can destroy the execution context between the two
            # document checks. Confirm the new document before classifying it.
            if isinstance(expected, dict):
                with contextlib.suppress(Error):
                    async with self._frame(state) as current:
                        if not same_document(expected, current.document):
                            raise RuntimeError("wait_document_changed") from error
            raise

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
        controls = self._control_baseline(args) if condition == "controls_changed" else Counter[tuple[str, str, str]]()
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
            unchanged_document = None if condition in {"url_contains", "new_document"} else expected
            async with self._wait_frame(state, unchanged_document) as context:
                same = expected is None or context.document == expected
                if not same and condition not in {"url_contains", "new_document"}:
                    raise RuntimeError("wait_document_changed")
                matched = False
                if condition in {"text", "text_absent"}:
                    body: object = await _evaluate(context.frame, "() => document.body?.innerText || ''")
                    matched = (value in str(body)) == (condition == "text")
                elif condition in {"text_changed", "controls_changed"}:
                    if condition == "controls_changed":
                        available = await sample_available_controls(context)
                        matched = available is not None and any(count > controls[key] for key, count in available.items())
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
                if matched and time.monotonic() < deadline:
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
                candidates = await context.frame.query_selector_all("article,output,[role=status],[role=region],[aria-live]")
                visible = [node for node in candidates if await node.is_visible()]
                if len(visible) == 1:
                    target = {"backend_node_id": await backend_node(context, visible[0]), "relation": "observed-result-region"}
                for node in candidates:
                    await node.dispose()
        regions = await self._query_regions(context, target) if target is not None else []
        baseline: JsonObject = {"document": context.document, "regions": regions,
            "control": {"backend_node_id": record.backend_node_id}, "context": record.context}
        if target is not None:
            baseline["target"] = target
        return baseline

    async def _query_regions(self, context: FrameContext, target: JsonObject) -> list[JsonValue]:
        backend_node_id = require_json_integer(target["backend_node_id"], "backend node")
        node = await resolve_backend(context, backend_node_id)
        if node is None:
            raise RuntimeError("query_target_unavailable")
        try:
            raw: object = await _evaluate(node, """el => ({text:(""" + VISIBLE_TEXT + """)(el, true).slice(0,100001),
                connected:el.isConnected && el.ownerDocument === document,
                visible:!!el.getClientRects().length && getComputedStyle(el).visibility!=='hidden',
                busy:!!el.closest('[aria-busy=true]') || !!el.querySelector('[aria-busy=true]')})""")
            row = decode_json_object(raw)
            if row.pop("connected") is not True:
                raise ValueError("query_target_detached")
            if len(require_json_string(row["text"], "query text")) > 100000:
                raise ValueError("query_region_exceeds_observation_budget")
            return [{"key": str(backend_node_id), **row}]
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
            raise QueryObservationError("query_document_changed", "The click belongs to a previous document.")
        raw_target = baseline.get("target")
        if not isinstance(raw_target, dict):
            raise QueryObservationError("query_target_unbound",
                "The click was delivered, but no result region was bound before it. Observe the page without repeating the click.")
        target = decode_json_object(raw_target)
        deadline = min(invocation.command.deadline, time.monotonic() + 2.5)
        while True:
            current: JsonObject = {"document": context.document, "regions": await self._query_regions(context, target)}
            changed = changed_query_region(baseline, current)
            if changed:
                captured = await self._capture(context)
                if (" ".join(changed.split()) in " ".join(str(captured["text"]).split())
                        and current["regions"] == await self._query_regions(context, target)):
                    captured["query_confirmed"] = True
                    captured["query_region"] = {"text": changed, "visible": True, "context": baseline.get("context"),
                        "relation": target["relation"]}
                    return captured
            if time.monotonic() >= deadline:
                raise QueryObservationError("query_update_unconfirmed", "The bound result region has not shown a confirmed update.")
            await asyncio.sleep(0.1)
