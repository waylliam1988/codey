"""Identify Playwright frames through their live Chromium execution worlds."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from uuid import uuid4

from playwright.async_api import CDPSession, ElementHandle, Error, Frame, Page

from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string


@dataclass(frozen=True)
class FrameContext:
    frame: Frame
    cdp: CDPSession
    context_id: int
    document: JsonObject

    async def send(self, method: str, arguments: JsonObject | None = None) -> JsonObject:
        raw: object = await self.cdp.send(method, arguments)
        return decode_json_object(raw)


def _find_frame(tree: JsonObject, frame_id: str) -> JsonObject | None:
    frame = decode_json_object(tree["frame"])
    if frame.get("id") == frame_id:
        return frame
    children = tree.get("childFrames", [])
    if not isinstance(children, list):
        raise ValueError("invalid Chromium frame tree")
    for child in children:
        found = _find_frame(decode_json_object(child), frame_id)
        if found is not None:
            return found
    return None


@asynccontextmanager
async def frame_context(page: Page, frame: Frame, target_id: str) -> AsyncGenerator[FrameContext, None]:
    if frame.is_detached():
        raise RuntimeError("frame_detached")
    try:
        cdp = await page.context.new_cdp_session(frame)
    except Error:
        if frame.is_detached():
            raise RuntimeError("frame_detached") from None
        cdp = await page.context.new_cdp_session(page)
    contexts: dict[int, JsonObject] = {}

    def created(raw: object) -> None:
        event = decode_json_object(raw)
        context = decode_json_object(event["context"])
        auxiliary = context.get("auxData")
        if isinstance(auxiliary, dict) and auxiliary.get("isDefault") is True:
            contexts[require_json_integer(context["id"], "execution context")] = auxiliary

    cdp.on("Runtime.executionContextCreated", created)
    key = "__codey_frame_" + uuid4().hex
    marker = uuid4().hex
    try:
        await cdp.send("Runtime.enable")
        await frame.evaluate("([key,value]) => Object.defineProperty(globalThis,key,{value,configurable:true})", [key, marker])
        selected: tuple[int, str] | None = None
        expression = "globalThis[" + json.dumps(key) + "] === " + json.dumps(marker)
        for context_id, auxiliary in tuple(contexts.items()):
            raw: object = await cdp.send("Runtime.evaluate", {"expression": expression, "contextId": context_id,
                                                            "returnByValue": True})
            value = decode_json_object(decode_json_object(raw)["result"]).get("value")
            if value is True:
                if selected is not None:
                    raise RuntimeError("ambiguous_frame_world")
                selected = context_id, require_json_string(auxiliary.get("frameId"), "frame id")
        if selected is None:
            raise RuntimeError("frame_world_unavailable")
        raw_tree: object = await cdp.send("Page.getFrameTree")
        tree = decode_json_object(decode_json_object(raw_tree)["frameTree"])
        observed = _find_frame(tree, selected[1])
        if observed is None:
            raise RuntimeError("frame_document_unavailable")
        loader = require_json_string(observed.get("loaderId"), "frame loader")
        if not loader:
            raise RuntimeError("frame_document_unknown")
        yield FrameContext(frame, cdp, selected[0], {
            "target_id": target_id, "frame_id": selected[1], "loader_id": loader,
            "url": require_json_string(observed.get("url"), "frame URL")})
    finally:
        with suppress(Error, TimeoutError):
            async with asyncio.timeout(0.5):
                await frame.evaluate("key => {delete globalThis[key]}", key)
        await cdp.detach()


async def backend_node(context: FrameContext, node: ElementHandle) -> int:
    key = "__codey_node_" + uuid4().hex
    group = "codey-node-" + uuid4().hex
    try:
        await node.evaluate("(node,key) => Object.defineProperty(globalThis,key,{value:node,configurable:true})", key)
        remote = await context.send("Runtime.evaluate", {
            "expression": "globalThis[" + json.dumps(key) + "]", "contextId": context.context_id, "objectGroup": group})
        object_id = require_json_string(decode_json_object(remote["result"]).get("objectId"), "node object")
        observed = await context.send("DOM.describeNode", {"objectId": object_id})
        return require_json_integer(decode_json_object(observed["node"])["backendNodeId"], "backend node")
    finally:
        with suppress(Error):
            await context.frame.evaluate("key => {delete globalThis[key]}", key)
        await context.send("Runtime.releaseObjectGroup", {"objectGroup": group})


async def resolve_backend(context: FrameContext, backend_node_id: int) -> ElementHandle:
    key = "__codey_resolved_" + uuid4().hex
    group = "codey-resolve-" + uuid4().hex
    try:
        raw = await context.send("DOM.resolveNode", {"backendNodeId": backend_node_id,
                                                   "executionContextId": context.context_id, "objectGroup": group})
        object_id = require_json_string(decode_json_object(raw["object"])["objectId"], "node object")
        await context.send("Runtime.callFunctionOn", {"objectId": object_id,
            "functionDeclaration": "function(key){Object.defineProperty(globalThis,key,{value:this,configurable:true})}",
            "arguments": [{"value": key}]})
        handle = await context.frame.evaluate_handle("key => {const node=globalThis[key];delete globalThis[key];return node}", key)
        element = handle.as_element()
        if element is None:
            await handle.dispose()
            raise RuntimeError("node_unavailable")
        return element
    finally:
        await context.send("Runtime.releaseObjectGroup", {"objectGroup": group})
