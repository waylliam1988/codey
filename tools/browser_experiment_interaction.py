"""Bounded semantic browser observations and actions on one retained page."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from playwright.sync_api import ElementHandle, Error, Page

from codey.research.browser_search import _search_browser_call
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.source_acquisition import with_attempt
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools.browser_experiment_binding import BrowserTargetBinding, assert_bound, bind_page
from tools.browser_experiment_sources import ResumableBrowserSearchProvider

MAX_SNAPSHOT_ELEMENTS = 80
MAX_SNAPSHOT_TEXT = 12_000
MAX_ACTION_TEXT = 2_000


@dataclass(frozen=True)
class BrowserToolOutput:
    model_text: str
    ok: bool
    observation: JsonObject


@dataclass
class _ElementRef:
    handle: ElementHandle
    page_token: int
    generation: int
    url: str


class BrowserInteraction:
    """One semantic action surface over the provider's retained fetch page."""

    def __init__(self, provider: ResumableBrowserSearchProvider) -> None:
        self.provider = provider
        self._binding: BrowserTargetBinding | None = None
        self._reference_scope = uuid4().hex
        self._generation = 0
        self._refs: dict[str, _ElementRef] = {}

    @classmethod
    def from_provider(cls, provider: object) -> BrowserInteraction | None:
        base = provider.base_provider if isinstance(provider, ConnectorAwareSearchProvider) else provider
        return cls(base) if isinstance(base, ResumableBrowserSearchProvider) else None

    def snapshot(self, *, max_elements: int = MAX_SNAPSHOT_ELEMENTS,
                 max_text: int = MAX_SNAPSHOT_TEXT) -> BrowserToolOutput:
        return _search_browser_call(self._snapshot_on_browser_thread, max_elements, max_text)

    def click(self, ref: str) -> BrowserToolOutput:
        return _search_browser_call(self._click_on_browser_thread, ref)

    def fill(self, ref: str, value: str) -> BrowserToolOutput:
        return _search_browser_call(self._fill_on_browser_thread, ref, value)

    def scroll(self, direction: str, amount: int) -> BrowserToolOutput:
        return _search_browser_call(self._scroll_on_browser_thread, direction, amount)

    def wait(self, condition: str, value: str, timeout_ms: int) -> BrowserToolOutput:
        return _search_browser_call(self._wait_on_browser_thread, condition, value, timeout_ms)

    def capture(self) -> dict[str, object]:
        """Capture this bound page; source admission and passages remain gateway-owned."""
        return _search_browser_call(self._capture_on_browser_thread)

    def _capture_on_browser_thread(self) -> dict[str, object]:
        cancellation.check()
        page = self._page_on_browser_thread()
        result = self.provider._resume_fetch_on_browser_thread()
        return with_attempt(result, "browser", page.url)

    def _page_on_browser_thread(self) -> Page:
        session = self.provider._session
        page = self.provider._fetch_page
        if session is None or page is None:
            raise RuntimeError("no retained browser page; open a source first")
        if self._binding is None:
            self._binding = bind_page(session, page)
        else:
            assert_bound(session, self._binding, page)
        return page

    def _snapshot_on_browser_thread(self, max_elements: int, max_text: int) -> BrowserToolOutput:
        page = self._page_on_browser_thread()
        self._generation += 1
        self._refs.clear()
        full_text = page.locator("body").inner_text(timeout=2_000)
        body = full_text[:max(1, min(max_text, MAX_SNAPSHOT_TEXT))]
        handles = page.query_selector_all(
            "button,a,input,textarea,select,[role=button],[role=link],[role=textbox],[role=combobox]"
        )
        elements: list[JsonObject] = []
        elements_truncated = False
        for handle in handles:
            if not handle.is_visible():
                continue
            metadata = decode_json_object(handle.evaluate("""el => ({
                tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
                name: el.getAttribute('aria-label') || el.getAttribute('title') ||
                      Array.from(el.labels || [], label => label.innerText).join(' ') ||
                      el.getAttribute('placeholder') || el.innerText || '',
                type: el.getAttribute('type') || '',
                value: el.type === 'password' ? null : ('value' in el ? el.value : null),
                disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
                checked: 'checked' in el ? el.checked : null
            })"""))
            role = _element_role(metadata)
            name = " ".join(str(metadata.get("name") or "").split())[:240]
            if not name and role not in {"textbox", "combobox"}:
                continue
            if len(elements) >= max(1, min(max_elements, MAX_SNAPSHOT_ELEMENTS)):
                elements_truncated = True
                break
            ref = f"b{self._reference_scope}g{self._generation}e{len(elements) + 1}"
            self._refs[ref] = _ElementRef(handle, id(page), self._generation, page.url)
            elements.append({"ref": ref, "role": role, "name": name,
                             **{key: metadata[key] for key in ("value", "disabled", "checked")}})
        rows = [f"[{row['ref']}] {row['role']} \"{row['name']}\""
                + (f" value={row['value']!r}" if row["value"] is not None else "")
                + (" disabled" if row["disabled"] else "") for row in elements]
        text = (f"Page URL: {page.url}\nObservation generation: {self._generation}\n"
                f"Visible text:\n{body}\nInteractive elements:\n" + "\n".join(rows))
        elements_value: list[JsonValue] = [cast(JsonValue, row) for row in elements]
        return BrowserToolOutput(text, True, {
            "page_url": page.url,
            "generation": self._generation,
            "complete": len(body) == len(full_text) and not elements_truncated,
            "scope": "main_frame_visible_text_and_named_controls",
            "text_truncated": len(body) < len(full_text),
            "elements_truncated": elements_truncated,
            "text": body,
            "elements": elements_value,
        })

    def _click_on_browser_thread(self, ref: str) -> BrowserToolOutput:
        cancellation.check()
        try:
            page, handle = self._resolve_ref(ref)
        except RuntimeError as exc:
            return self._stale_ref_output("click", ref, exc)
        try:
            handle.click(timeout=5_000, no_wait_after=True)
        except Exception as exc:
            self._invalidate_refs()
            return BrowserToolOutput(f"ERROR: click delivery is uncertain: {exc}", False,
                                     {"action": "click", "ref": ref, "delivery": "uncertain"})
        self._invalidate_refs()
        return self._action_observation(page, "click", ref)

    def _fill_on_browser_thread(self, ref: str, value: str) -> BrowserToolOutput:
        cancellation.check()
        try:
            page, handle = self._resolve_ref(ref)
        except RuntimeError as exc:
            return self._stale_ref_output("fill", ref, exc)
        try:
            handle.fill(value, timeout=5_000)
            actual = str(handle.evaluate("el => 'value' in el ? el.value : el.textContent || ''"))
        except Exception as exc:
            self._invalidate_refs()
            return BrowserToolOutput(f"ERROR: fill delivery is uncertain: {exc}", False,
                                     {"action": "fill", "ref": ref, "delivery": "uncertain"})
        self._invalidate_refs()
        verified = actual == value
        return BrowserToolOutput(
            ("Filled and verified " + ref) if verified else "ERROR: fill was delivered but readback differed",
            verified,
            {"action": "fill", "ref": ref, "delivery": "verified" if verified else "unverified",
             "value_length": len(value)},
        )

    def _scroll_on_browser_thread(self, direction: str, amount: int) -> BrowserToolOutput:
        cancellation.check()
        page = self._page_on_browser_thread()
        normalized = direction.strip().lower()
        if normalized not in {"up", "down", "left", "right"}:
            return BrowserToolOutput("ERROR: scroll direction must be up, down, left, or right", False,
                                     {"action": "scroll", "delivery": "not_delivered"})
        distance = max(1, min(abs(amount), 20_000))
        dx = distance if normalized == "right" else -distance if normalized == "left" else 0
        dy = distance if normalized == "down" else -distance if normalized == "up" else 0
        page.evaluate("([x, y]) => window.scrollBy(x, y)", [dx, dy])
        self._invalidate_refs()
        return self._action_observation(page, "scroll", normalized)

    def _wait_on_browser_thread(self, condition: str, value: str, timeout_ms: int) -> BrowserToolOutput:
        page = self._page_on_browser_thread()
        if condition not in {"text", "selector", "url_contains"} or not value:
            return BrowserToolOutput("ERROR: wait requires a supported condition and non-empty value", False,
                                     {"action": "wait", "matched": False, "reason": "invalid_condition"})
        timeout = max(100, min(timeout_ms, 30_000))
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            cancellation.check()
            matched = (
                any(item.is_visible() for item in page.get_by_text(value, exact=False).all()) if condition == "text" else
                any(item.is_visible() for item in page.locator(value).all()) if condition == "selector" else
                value in page.url if condition == "url_contains" else False
            )
            if matched:
                return BrowserToolOutput(f"Wait condition satisfied: {condition}", True,
                                         {"action": "wait", "condition": condition, "matched": True,
                                          "page_url": page.url})
            page.wait_for_timeout(100)
        return BrowserToolOutput(f"ERROR: wait timed out: {condition}", False,
                                 {"action": "wait", "condition": condition, "matched": False,
                                  "page_url": page.url})

    def _resolve_ref(self, ref: str) -> tuple[Page, ElementHandle]:
        page = self._page_on_browser_thread()
        entry = self._refs.get(ref.strip())
        if entry is None or entry.page_token != id(page) or entry.generation != self._generation or entry.url != page.url:
            raise RuntimeError("stale browser element reference; take a fresh browser_snapshot")
        try:
            connected = entry.handle.evaluate("el => el.isConnected && el.ownerDocument === document")
        except Error as exc:
            raise RuntimeError("stale browser element reference; document is no longer available") from exc
        if connected is not True:
            raise RuntimeError("stale browser element reference; original node is detached")
        return page, entry.handle

    def _invalidate_refs(self) -> None:
        self._generation += 1
        self._refs.clear()

    @staticmethod
    def _stale_ref_output(action: str, ref: str, error: RuntimeError) -> BrowserToolOutput:
        return BrowserToolOutput(
            f"ERROR: {error}",
            False,
            {"action": action, "ref": ref, "delivery": "not_delivered", "reason": "stale_ref"},
        )

    @staticmethod
    def _action_observation(page: Page, action: str, ref: str) -> BrowserToolOutput:
        try:
            text = page.locator("body").inner_text(timeout=2_000)[:MAX_ACTION_TEXT]
        except Error as exc:
            return BrowserToolOutput(f"ERROR: action delivered; page observation failed: {exc}", False,
                                     {"action": action, "ref": ref, "delivery": "delivered",
                                      "observation": "unavailable", "effect_verified": False})
        return BrowserToolOutput(f"Action delivered and page observed: {action} {ref}\n{text}", True,
                                 {"action": action, "ref": ref, "delivery": "delivered",
                                  "observation": "observed", "effect_verified": False,
                                  "page_url": page.url, "text": text})


def _element_role(metadata: JsonObject) -> str:
    role = str(metadata.get("role") or "").strip().lower()
    if role:
        return role
    tag = str(metadata.get("tag") or "").lower()
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag in {"textarea", "input"}:
        input_type = str(metadata.get("type") or "").lower()
        return "checkbox" if input_type == "checkbox" else "textbox"
    if tag == "select":
        return "combobox"
    return "generic"


__all__ = ["BrowserInteraction", "BrowserToolOutput"]
