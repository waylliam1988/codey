"""Bind query regions to actual nodes observed in the selected frame."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from playwright.async_api import ElementHandle

from codey.automation.browser_frames import FrameContext, backend_node
from codey.automation.browser_query import QUERY_TARGET
from codey.types.json_value import JsonObject, require_json_string


async def bind_query_target(context: FrameContext, handle: ElementHandle) -> JsonObject | None:
    observed = await handle.evaluate_handle(QUERY_TARGET)
    node = await observed.get_property("node")
    relation = await observed.get_property("relation")
    try:
        element = node.as_element()
        if element is None:
            return None
        read_relation: Callable[[], Awaitable[object]] = relation.json_value
        raw_relation = await read_relation()
        return {"backend_node_id": await backend_node(context, element),
                "relation": require_json_string(raw_relation, "query relation")}
    finally:
        await node.dispose()
        await relation.dispose()
        await observed.dispose()
