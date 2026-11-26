"""Browser action values, shared observation state, and narrow runtime callbacks."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from playwright.async_api import CDPSession, ElementHandle, Frame, Page

from codey.automation.browser_binding import PageBinding
from codey.automation.browser_contracts import BrowserCommand
from codey.automation.browser_frames import FrameContext
from codey.automation.browser_native_input import NativeInput
from codey.automation.browser_refs import ElementRecord, ElementRegistry
from codey.types.json_value import JsonObject

Progress = Callable[[JsonObject], Awaitable[None]]


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


@dataclass
class ObservationState:
    registry: ElementRegistry[ElementHandle] = field(default_factory=ElementRegistry)
    revision: int = 0
    observations: dict[int, JsonObject] = field(default_factory=dict)


FrameAccess = Callable[[PageState], AbstractAsyncContextManager[FrameContext]]


class ResolveElement(Protocol):
    async def __call__(self, context: FrameContext, ref: str, *,
                       purpose: Literal["action", "scope", "inspect"] = "action") -> ElementRecord[ElementHandle]: ...


class MarkProgress(Protocol):
    async def __call__(self, invocation: Invocation, phase: str, *,
                       dispatch: Literal["not_sent", "unknown", "confirmed"] | None = None) -> None: ...
