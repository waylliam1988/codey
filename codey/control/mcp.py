"""Official SDK stdio adapter with the same schemas and application client."""

from __future__ import annotations

import sys
from typing import Any

from codey.control.client import ControlClient
from codey.control.protocol import COMMANDS, PROTOCOL_VERSION, ControlError


def build_server(client: ControlClient) -> Any:
    from anyio import CapacityLimiter
    from anyio.to_thread import run_sync
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

    limiter: CapacityLimiter | None = None

    async def list_tools(_ctx: Any, _params: Any) -> Any:
        return ListToolsResult(tools=[Tool(
            name=name, description=command.description, input_schema=command.schema(),
            output_schema={"type": "object"},
            annotations=ToolAnnotations(read_only_hint=command.readonly, destructive_hint=not command.readonly,
                                        idempotent_hint=True, open_world_hint=not command.readonly),
        ) for name, command in COMMANDS.items()])

    async def call_tool(_ctx: Any, params: Any) -> Any:
        nonlocal limiter
        if limiter is None:
            limiter = CapacityLimiter(16)
        try:
            result = await run_sync(client.call, params.name, params.arguments or {}, abandon_on_cancel=True, limiter=limiter)
            state = result.get("reason") or result.get("execution") or result.get("phase") or result.get("status") or "ok"
            return CallToolResult(content=[TextContent(type="text", text=f"Codey: {state}. Structured result attached.")],
                                  structured_content=result)
        except ControlError as exc:
            return CallToolResult(content=[TextContent(type="text", text=f"{exc.code}: {exc}")],
                                  structured_content={"error": exc.payload()}, is_error=True)

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
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(serve)
    return 0
