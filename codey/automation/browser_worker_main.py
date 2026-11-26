"""Child entry point. Only this process owns task Playwright objects."""
from __future__ import annotations

import asyncio
import sys
from io import BufferedIOBase, RawIOBase
from uuid import uuid4

from playwright.async_api import Error

from codey.automation.browser_binding import BrowserBinding
from codey.automation.browser_contracts import BrowserCommand, BrowserCompletion, BrowserConfig, BrowserToolOutput
from codey.automation.browser_ipc import BrowserPacket, read_packet, write_packet
from codey.automation.browser_network import NetworkPolicy
from codey.automation.browser_runtime import BrowserRuntime, Progress
from codey.types.json_value import JsonObject, decode_json_object, require_json_boolean, require_json_string


async def close_runtime(runtime: BrowserRuntime) -> JsonObject:
    try:
        await runtime.close()
    except (Error, OSError, RuntimeError, ValueError) as exc:
        return {"type": "closed", "ok": False, "reason": str(exc) or type(exc).__name__}
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


class BrowserServer:
    """Keep the control pipe alive until network draining and cleanup are acknowledged."""

    def __init__(self, first: BrowserPacket, stdin: BufferedIOBase | RawIOBase,
                 stdout: BufferedIOBase | RawIOBase) -> None:
        self.stdin, self.stdout = stdin, stdout
        self.write_lock = asyncio.Lock()
        self.routes: dict[str, asyncio.Future[JsonObject]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.acknowledgements: dict[str, asyncio.Future[None]] = {}
        self.closing: asyncio.Task[None] | None = None
        self.runtime, self.owner, self.resource = initialize(first, self.policy, self.progress)

    async def send(self, packet: BrowserPacket) -> None:
        async with self.write_lock:
            await asyncio.to_thread(write_packet, self.stdout, packet)

    async def policy(self, request: JsonObject) -> JsonObject:
        identity = uuid4().hex
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self.routes[identity] = future
        try:
            await self.send(BrowserPacket({"type": "route", "route_id": identity, "request": request}))
            async with asyncio.timeout(15):
                return await future
        finally:
            self.routes.pop(identity, None)

    async def progress(self, value: JsonObject) -> None:
        identity = uuid4().hex
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.acknowledgements[identity] = future
        try:
            await self.send(BrowserPacket({"type": "progress", "progress_id": identity, **value}))
            async with asyncio.timeout(10):
                await future
        finally:
            self.acknowledgements.pop(identity, None)

    async def execute(self, command: BrowserCommand, blob: bytes) -> None:
        try:
            await self.send(await self.runtime.dispatch(command, binary=blob))
        finally:
            self.tasks.pop(command.operation_id, None)

    async def finish(self, release: bool) -> None:
        active = tuple(self.tasks.values())
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        if release:
            await self.send(BrowserPacket(await close_runtime(self.runtime)))
        else:
            await self.runtime.close(release=False)
            await self.send(BrowserPacket({"type": "closed", "ok": True, "reason": ""}))

    async def accept(self, packet: BrowserPacket) -> bool:
        message = packet.message
        kind = message.get("type")
        if kind == "dispatch":
            command = BrowserCommand.read(message["command"])
            if command.owner_task_id != self.owner or command.resource_id != self.resource or command.operation_id in self.tasks:
                raise ValueError("browser invocation owner or identity mismatch")
            if self.closing is not None:
                output = BrowserToolOutput("Browser is closing.", False,
                    {"reason": "browser_closing", "delivery": "not_delivered"})
                await self.send(BrowserPacket({"type": "result", "operation_id": command.operation_id,
                    "value": output.payload(), "completion": BrowserCompletion.FAILED.value, "generation": 0, "binding": None}))
            else:
                self.tasks[command.operation_id] = asyncio.create_task(self.execute(command, packet.blob))
        elif kind == "cancel":
            task = self.tasks.get(require_json_string(message["operation_id"], "operation_id"))
            if task is not None:
                task.cancel()
        elif kind == "progress_ack":
            acknowledgement = self.acknowledgements.get(require_json_string(message["progress_id"], "progress_id"))
            if acknowledgement is not None and not acknowledgement.done():
                acknowledgement.set_result(None)
        elif kind == "route_result":
            future = self.routes.get(require_json_string(message["route_id"], "route_id"))
            if future is not None and not future.done():
                future.set_result(decode_json_object(message["decision"]))
        elif kind == "health":
            await self.send(BrowserPacket({"type": "health", "active_operations": list(self.tasks)}))
        elif kind == "close":
            if self.closing is None:
                self.closing = asyncio.create_task(self.finish(require_json_boolean(message.get("release", True), "release")))
        elif kind == "close_ack":
            if self.closing is None:
                raise ValueError("browser close acknowledgement without cleanup")
            await self.closing
            return False
        else:
            raise ValueError("invalid browser control message")
        return True

    async def run(self) -> None:
        try:
            await self.send(BrowserPacket({"type": "ready"}))
            while await self.accept(await asyncio.to_thread(read_packet, self.stdin)):
                pass
        except EOFError:
            pass
        finally:
            if self.closing is not None:
                await self.closing
            else:
                await self.finish(False)


async def serve() -> None:
    stdin, stdout = _ipc_streams()
    first = await asyncio.to_thread(read_packet, stdin)
    await BrowserServer(first, stdin, stdout).run()


if __name__ == "__main__":
    asyncio.run(serve())
