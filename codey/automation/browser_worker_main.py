"""Child entry point. Only this process owns task Playwright objects."""
from __future__ import annotations

import asyncio
import sys
from io import BufferedIOBase, RawIOBase
from uuid import uuid4

from playwright.async_api import Error

from codey.automation.browser_binding import BrowserBinding
from codey.automation.browser_contracts import BrowserCommand, BrowserConfig
from codey.automation.browser_ipc import BrowserPacket, read_packet, write_packet
from codey.automation.browser_runtime import BrowserRuntime, NetworkPolicy, Progress
from codey.types.json_value import JsonObject, decode_json_object, require_json_boolean, require_json_string


async def close_runtime(runtime: BrowserRuntime) -> JsonObject:
    try:
        await runtime.close()
    except (Error, OSError, RuntimeError, ValueError) as exc:
        return {"type": "closed", "ok": False, "reason": str(exc)}
    return {"type": "closed", "ok": True, "reason": ""}


def initialize(first: BrowserPacket, policy: NetworkPolicy, progress: Progress) -> tuple[BrowserRuntime, str, str]:
    owner, resource = (require_json_string(first.message["owner_task_id"], "owner_task_id"),
                       require_json_string(first.message["resource_id"], "resource_id"))
    raw_binding = first.message.get("binding")
    return BrowserRuntime(BrowserConfig.read(first.message["config"]), network_policy=policy, progress=progress,
        inputs=decode_json_object(first.message["inputs"]),
        binding=BrowserBinding.read(raw_binding) if raw_binding is not None else None), owner, resource


def _ipc_streams() -> tuple[BufferedIOBase | RawIOBase, BufferedIOBase | RawIOBase]:
    stdin: object = sys.stdin
    stdout: object = sys.stdout
    source: object = getattr(stdin, "buffer", None)
    destination: object = getattr(stdout, "buffer", None)
    if not isinstance(source, (BufferedIOBase, RawIOBase)) or not isinstance(destination, (BufferedIOBase, RawIOBase)):
        raise RuntimeError("browser worker requires binary standard streams")
    return source, destination


async def serve() -> None:
    stdin, stdout = _ipc_streams()
    write_lock = asyncio.Lock()
    routes: dict[str, asyncio.Future[JsonObject]] = {}
    tasks: dict[str, asyncio.Task[None]] = {}
    acknowledgements: dict[str, asyncio.Future[None]] = {}

    async def send(packet: BrowserPacket) -> None:
        async with write_lock:
            await asyncio.to_thread(write_packet, stdout, packet)

    async def policy(request: JsonObject) -> JsonObject:
        identity = uuid4().hex
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        routes[identity] = future
        try:
            await send(BrowserPacket({"type": "route", "route_id": identity, "request": request}))
            async with asyncio.timeout(15):
                return await future
        finally:
            routes.pop(identity, None)

    async def progress(value: JsonObject) -> None:
        identity = uuid4().hex
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        acknowledgements[identity] = future
        try:
            await send(BrowserPacket({"type": "progress", "progress_id": identity, **value}))
            async with asyncio.timeout(10):
                await future
        finally:
            acknowledgements.pop(identity, None)

    first = await asyncio.to_thread(read_packet, stdin)
    runtime, owner, resource = initialize(first, policy, progress)

    async def execute(command: BrowserCommand, blob: bytes) -> None:
        try:
            await send(await runtime.dispatch(command, binary=blob))
        finally:
            tasks.pop(command.operation_id, None)

    release = False
    try:
        await send(BrowserPacket({"type": "ready"}))
        while True:
            packet = await asyncio.to_thread(read_packet, stdin)
            message = packet.message
            kind = message.get("type")
            if kind == "dispatch":
                command = BrowserCommand.read(message["command"])
                if command.owner_task_id != owner or command.resource_id != resource or command.operation_id in tasks:
                    raise ValueError("browser invocation owner or identity mismatch")
                tasks[command.operation_id] = asyncio.create_task(execute(command, packet.blob))
            elif kind == "cancel":
                task = tasks.get(require_json_string(message["operation_id"], "operation_id"))
                if task is not None:
                    task.cancel()
            elif kind == "progress_ack":
                acknowledgement = acknowledgements.get(require_json_string(message["progress_id"], "progress_id"))
                if acknowledgement is not None and not acknowledgement.done():
                    acknowledgement.set_result(None)
            elif kind == "route_result":
                future = routes.get(require_json_string(message["route_id"], "route_id"))
                if future is not None and not future.done():
                    future.set_result(decode_json_object(message["decision"]))
            elif kind == "health":
                await send(BrowserPacket({"type": "health", "active_operations": list(tasks)}))
            elif kind == "close":
                release = require_json_boolean(message.get("release", True), "release")
                break
            else:
                raise ValueError("invalid browser control message")
    except EOFError:
        pass
    finally:
        active = tuple(tasks.values())
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if release:
            await send(BrowserPacket(await close_runtime(runtime)))
        else:
            await runtime.close(release=False)
            await send(BrowserPacket({"type": "closed", "ok": True, "reason": ""}))


if __name__ == "__main__":
    asyncio.run(serve())
