"""Official SDK stdio adapter with the same schemas and application client."""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from codey.control.client import ControlClient
from codey.control.protocol import COMMANDS, PROTOCOL_VERSION, ControlError

if TYPE_CHECKING:
    from mcp.server import Server
    from mcp.shared._stream_protocols import ReadStream, WriteStream
    from mcp.shared.message import SessionMessage
    from mcp.types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams

    _StdioStreams = tuple[ReadStream[SessionMessage | Exception], WriteStream[SessionMessage]]


def build_server(client: ControlClient) -> Server[dict[str, object]]:
    from anyio import CapacityLimiter
    from anyio.to_thread import run_sync
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

    limiter: CapacityLimiter | None = None

    async def list_tools(
        _ctx: object, _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        tools: list[Tool] = []
        for name, command in COMMANDS.items():
            schema: dict[str, object] = command.schema()
            output_schema: dict[str, object] = {"type": "object"}
            tools.append(Tool(
                name=name, description=command.description, input_schema=schema, output_schema=output_schema,
                annotations=ToolAnnotations(read_only_hint=command.readonly, destructive_hint=not command.readonly,
                                            idempotent_hint=True, open_world_hint=not command.readonly),
            ))
        return ListToolsResult(tools=tools)

    async def call_tool(_ctx: object, params: CallToolRequestParams) -> CallToolResult:
        nonlocal limiter
        if limiter is None:
            limiter = CapacityLimiter(16)
        try:
            arguments: object = params.arguments
            if arguments is None:
                arguments = dict[str, object]()
            result = await run_sync(client.call, params.name, arguments, abandon_on_cancel=True, limiter=limiter)
            state = result.get("reason") or result.get("execution") or result.get("phase") or result.get("status") or "ok"
            return CallToolResult(content=[TextContent(type="text", text=f"Codey: {state}. Structured result attached.")],
                                  structured_content=result)
        except ControlError as exc:
            error: dict[str, object] = exc.payload()
            structured_error: dict[str, object] = {"error": error}
            return CallToolResult(content=[TextContent(type="text", text=f"{exc.code}: {exc}")],
                                  structured_content=structured_error, is_error=True)

    return Server("Codey", version=str(PROTOCOL_VERSION),
                  instructions="Use stable request_id values for mutations. Admission is not completion. Wait with terminal_or_attention. "
                               "Inspect actual evidence before recording a review. Cancelling a tool only cancels observation; stop_run cancels work.",
                  on_list_tools=list_tools, on_call_tool=call_tool)


def run(client: ControlClient) -> int:
    try:
        import anyio
        from mcp.server.stdio import stdio_server
    except ImportError:
        print("MCP adapter requires the optional codey[mcp] dependencies.", file=sys.stderr)
        return 2

    async def serve() -> None:
        server = build_server(client)
        open_stdio: Callable[[], AbstractAsyncContextManager[_StdioStreams]] = stdio_server
        async with open_stdio() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(serve)
    return 0
