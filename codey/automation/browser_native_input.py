"""Native key ownership and bounded release, including interrupted chords."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from playwright.async_api import ElementHandle, Error, Page

from codey.automation.browser_contracts import InputRelease
from codey.types.json_value import JsonObject, decode_json_object, require_json_string, require_json_strings


@dataclass
class NativeInput:
    keys: list[str] = field(default_factory=list)
    release_state: InputRelease = InputRelease.RELEASED

    def payload(self) -> JsonObject:
        return {"keys": list(self.keys), "release": self.release_state.value}

    @classmethod
    def read(cls, value: object) -> NativeInput:
        row = decode_json_object(value)
        if set(row) != {"keys", "release"}:
            raise ValueError("invalid native input state")
        return cls(require_json_strings(row["keys"], "pressed keys"), InputRelease(require_json_string(row["release"], "release")))

    async def release(self, page: Page) -> None:
        async with asyncio.timeout(1):
            for key in tuple(reversed(self.keys)):
                await page.keyboard.up(key)
                self.keys.remove(key)
        self.release_state = InputRelease.RELEASED

    async def press(self, page: Page, handle: ElementHandle, chord: str, progress: Callable[[], Awaitable[None]]) -> None:
        if self.keys:
            await self.release(page)
        keys = chord.split("+") if chord != "+" else ["+"]
        if chord.endswith("++"):
            keys[-2:] = ["+"]
        if any(not key for key in keys):
            raise ValueError("invalid_key_chord")
        await handle.focus()
        try:
            for key in keys:
                # A failed await can have sent the key. Keep it until keyup is
                # acknowledged; cancellation never means it was not pressed.
                self.keys.append(key)
                self.release_state = InputRelease.UNKNOWN
                await progress()
                await page.keyboard.down(key)
            self.release_state = InputRelease.PENDING
        finally:
            try:
                await self.release(page)
                await progress()
            except (Error, TimeoutError):
                self.release_state = InputRelease.UNKNOWN
                raise RuntimeError("input_release_pending") from None
