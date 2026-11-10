"""Task-scoped browser actions over the existing Research browser worker.

This module owns page interaction state only. It does not run a model, write a
Research ledger, or publish events; callers turn its bounded observations into
the ordinary kernel ``ToolResult`` receipt.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar, cast
from uuid import uuid4

from playwright.sync_api import ElementHandle, Error, FilePayload, Page

from codey.policies.network import check_fetch_url
from codey.research.browser_search import (
    BrowserSearchProvider,
    _fetch_page_content_after_settle,
    _search_browser_call,
    _validate_page_content_type,
    _validate_page_observation,
    _validate_page_status,
)
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.extract import _MAX_PAGE_CHARS, extract_title
from codey.research.source_acquisition import with_attempt
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_value

MAX_SNAPSHOT_ELEMENTS = 80
MAX_SNAPSHOT_TEXT = 12_000
MAX_ACTION_TEXT = 2_000
T = TypeVar("T")


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


def _browser_call(fn: Callable[..., T], *args: object) -> T:
    """Keep the browser worker boundary typed when its runtime is imported lazily."""
    return cast(T, _search_browser_call(fn, *args))


def _browser_attempt(page: dict[str, object], url: str) -> dict[str, object]:
    return cast(dict[str, object], with_attempt(page, "browser", url))


def _base_provider(provider: object) -> BrowserSearchProvider:
    base = provider.base_provider if isinstance(provider, ConnectorAwareSearchProvider) else provider
    if isinstance(base, WebSourceProvider):
        base = base.browser_source
    if not isinstance(base, BrowserSearchProvider):
        raise TypeError("browser interaction requires the production BrowserSearchProvider")
    return base


class BrowserSession:
    """One task-owned page binding using the Research browser worker."""

    def __init__(self, provider: object) -> None:
        self.provider = _base_provider(provider)
        self._reference_scope = uuid4().hex
        self._generation = 0
        self._refs: dict[str, _ElementRef] = {}
        self._page_ids: dict[Page, str] = {}
        self._navigation_facts: dict[int, tuple[int | None, str]] = {}

    @classmethod
    def from_provider(cls, provider: object) -> BrowserSession:
        return cls(provider)

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self, *, max_elements: int = MAX_SNAPSHOT_ELEMENTS,
                 max_text: int = MAX_SNAPSHOT_TEXT) -> BrowserToolOutput:
        return _browser_call(self._snapshot_on_browser_thread, max_elements, max_text)

    def click(self, ref: str) -> BrowserToolOutput:
        return _browser_call(self._click_on_browser_thread, ref)

    def fill(self, ref: str, value: str) -> BrowserToolOutput:
        return _browser_call(self._fill_on_browser_thread, ref, value)

    def scroll(self, direction: str, amount: int) -> BrowserToolOutput:
        return _browser_call(self._scroll_on_browser_thread, direction, amount)

    def wait(self, condition: str, value: str, timeout_ms: int) -> BrowserToolOutput:
        return _browser_call(self._wait_on_browser_thread, condition, value, timeout_ms)

    def navigate(self, url: str) -> BrowserToolOutput:
        return _browser_call(self._navigate_on_browser_thread, url)

    def evaluate(self, code: str) -> BrowserToolOutput:
        return _browser_call(self._evaluate_on_browser_thread, code)

    def tab_list(self) -> BrowserToolOutput:
        return _browser_call(self._tab_list_on_browser_thread)

    def tab_new(self, url: str) -> BrowserToolOutput:
        return _browser_call(self._tab_new_on_browser_thread, url)

    def tab_switch(self, index: int) -> BrowserToolOutput:
        return _browser_call(self._tab_switch_on_browser_thread, index)

    def upload(self, ref: str, file: FilePayload) -> BrowserToolOutput:
        return _browser_call(self._upload_on_browser_thread, ref, file)

    def download(self, ref: str) -> BrowserToolOutput:
        return _browser_call(self._download_on_browser_thread, ref)

    def capture(self) -> dict[str, object]:
        return _browser_call(self._capture_on_browser_thread)

    def _page_on_browser_thread(self) -> Page:
        page = self.provider._fetch_page
        if page is None or page.is_closed():
            raise RuntimeError(
                "no retained browser page; call browser_navigate with the source URL "
                "to take over an HTTP-first source before browser actions"
            )
        return page

    def _page_id(self, page: Page) -> str:
        if page not in self._page_ids:
            self._page_ids[page] = uuid4().hex
        return self._page_ids[page]

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
            metadata = canonical_json_object(handle.evaluate("""el => ({
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
            "page_id": self._page_id(page), "page_url": page.url, "generation": self._generation,
            "complete": len(body) == len(full_text) and not elements_truncated,
            "scope": "main_frame_visible_text_and_named_controls",
            "text_truncated": len(body) < len(full_text),
            "elements_truncated": elements_truncated, "text": body,
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
            verified, {"action": "fill", "ref": ref,
                       "delivery": "verified" if verified else "unverified", "value_length": len(value)},
        )

    def _scroll_on_browser_thread(self, direction: str, amount: int) -> BrowserToolOutput:
        page = self._page_on_browser_thread()
        normalized = direction.strip().lower()
        if normalized not in {"up", "down", "left", "right"}:
            return BrowserToolOutput("ERROR: invalid scroll direction", False,
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
                value in page.url
            )
            if matched:
                return BrowserToolOutput(f"Wait condition satisfied: {condition}", True,
                                         {"action": "wait", "condition": condition, "matched": True,
                                          "page_url": page.url})
            page.wait_for_timeout(100)
        return BrowserToolOutput(f"ERROR: wait timed out: {condition}", False,
                                 {"action": "wait", "condition": condition, "matched": False,
                                  "page_url": page.url})

    def _navigate_on_browser_thread(self, url: str) -> BrowserToolOutput:
        cancellation.check()
        target = url.strip()
        if not target:
            return BrowserToolOutput("ERROR: browser_navigate needs a URL", False,
                                     {"action": "navigate", "delivery": "not_delivered"})
        policy_reason = check_fetch_url(target, resolve=False)
        if policy_reason:
            return BrowserToolOutput(f"ERROR: navigation denied: {policy_reason}", False,
                                     {"action": "navigate", "url": target,
                                      "delivery": "not_delivered", "reason": "policy_denied"})
        page = self.provider._ensure_fetch_page_on_browser_thread(target)
        previous_url = page.url
        try:
            response = page.goto(target, wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            self._invalidate_refs()
            return BrowserToolOutput(f"ERROR: navigation failed: {exc}", False,
                                     {"action": "navigate", "url": target, "delivery": "uncertain"})
        final_url = page.url or target
        final_policy_reason = check_fetch_url(final_url, resolve=False)
        if final_policy_reason:
            self._navigation_facts[id(page)] = (None, "")
            self._invalidate_refs()
            return BrowserToolOutput(f"ERROR: navigation denied after redirect: {final_policy_reason}", False,
                                     {"action": "navigate", "url": final_url,
                                      "delivery": "confirmed", "reason": "policy_denied"})
        status = response.status if response is not None else None
        content_type = response.headers.get("content-type", "") if response is not None else ""
        self._navigation_facts[id(page)] = (status, content_type)
        self._invalidate_refs()
        return BrowserToolOutput(f"Navigated to {final_url}", True,
                                 {"action": "navigate", "url": final_url, "previous_url": previous_url,
                                  "page_id": self._page_id(page), "generation": self._generation,
                                  "delivery": "delivered", "http_status": status,
                                  "content_type": content_type})

    def _evaluate_on_browser_thread(self, code: str) -> BrowserToolOutput:
        page = self._page_on_browser_thread()
        if not code.strip():
            return BrowserToolOutput("ERROR: browser_eval needs code", False,
                                     {"action": "eval", "delivery": "not_delivered"})
        try:
            value = page.evaluate(code)
            canonical = decode_json_value(value)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return BrowserToolOutput(f"ERROR: eval failed: {exc}", False,
                                     {"action": "eval", "delivery": "uncertain"})
        finally:
            # Scripts can replace the document even when evaluation throws.
            self._invalidate_refs()
        rendered = json.dumps(canonical, ensure_ascii=False)
        truncated = len(rendered) > MAX_ACTION_TEXT
        bounded: JsonValue = rendered[:MAX_ACTION_TEXT] if truncated else canonical
        return BrowserToolOutput("Evaluation completed: " + rendered[:MAX_ACTION_TEXT], True,
                                 {"action": "eval", "delivery": "delivered", "value": bounded,
                                  "truncated": truncated})

    def _tab_list_on_browser_thread(self) -> BrowserToolOutput:
        session = self.provider._session
        if session is None:
            raise RuntimeError("no browser session")
        pages: list[JsonValue] = []
        current = self.provider._fetch_page
        observation: JsonObject = {"tabs": pages, "page_url": current.url if current is not None else ""}
        context = session.browser.contexts[0] if session.browser.contexts else None
        if context is None:
            return BrowserToolOutput("No browser tabs", True, observation)
        for index, page in enumerate(context.pages):
            pages.append({"index": index, "page_id": self._page_id(page),
                          "url": page.url, "title": str(page.title() or "")[:240]})
        return BrowserToolOutput("Current browser tabs: " + json.dumps(observation, ensure_ascii=False), True, observation)

    def _tab_new_on_browser_thread(self, url: str) -> BrowserToolOutput:
        cancellation.check()
        target = url.strip()
        policy_reason = check_fetch_url(target, resolve=False)
        if policy_reason:
            return BrowserToolOutput(f"ERROR: tab URL denied: {policy_reason}", False,
                                     {"action": "tab_new", "url": target,
                                      "delivery": "not_delivered", "reason": "policy_denied"})
        session = self.provider._ensure_session_on_browser_thread()
        context = session.browser.contexts[0] if session.browser.contexts else session.page.context
        page = self.provider._prepare_page_on_browser_thread(context.new_page())
        self.provider._register_owned_page_on_browser_thread(page)
        self.provider._fetch_page = page
        self._invalidate_refs()
        response = page.goto(target, wait_until="domcontentloaded", timeout=20_000)
        status = response.status if response is not None else None
        content_type = response.headers.get("content-type", "") if response is not None else ""
        self._navigation_facts[id(page)] = (status, content_type)
        return BrowserToolOutput(f"Opened tab at {page.url}", True,
                                 {"action": "tab_new", "url": page.url, "page_id": self._page_id(page),
                                  "generation": self._generation, "delivery": "delivered",
                                  "http_status": status, "content_type": content_type})

    def _tab_switch_on_browser_thread(self, index: int) -> BrowserToolOutput:
        session = self.provider._session
        if session is None:
            raise RuntimeError("no browser session")
        context = session.browser.contexts[0] if session.browser.contexts else session.page.context
        pages = context.pages
        if index < 0 or index >= len(pages):
            return BrowserToolOutput("ERROR: tab index is unavailable", False,
                                     {"action": "tab_switch", "index": index, "delivery": "not_delivered"})
        page = self.provider._prepare_page_on_browser_thread(pages[index])
        self.provider._register_borrowed_page_on_browser_thread(page)
        self.provider._fetch_page = page
        self._invalidate_refs()
        return BrowserToolOutput(f"Switched to tab {index}", True,
                                 {"action": "tab_switch", "index": index, "url": pages[index].url,
                                  "delivery": "delivered"})

    def _upload_on_browser_thread(self, ref: str, file: FilePayload) -> BrowserToolOutput:
        cancellation.check()
        try:
            _, handle = self._resolve_ref(ref)
        except RuntimeError as exc:
            return self._stale_ref_output("upload", ref, exc)
        try:
            handle.set_input_files(file)
            selected = handle.evaluate("el => Array.from(el.files, f => ({name:f.name,size:f.size,type:f.type}))")
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            self._invalidate_refs()
            return BrowserToolOutput(f"ERROR: upload failed: {exc}", False,
                                     {"action": "upload", "ref": ref, "delivery": "uncertain"})
        self._invalidate_refs()
        data = file["buffer"]
        verified = selected == [{"name": file["name"], "size": len(data), "type": file["mimeType"]}]
        return BrowserToolOutput("File input selection " + ("verified" if verified else "unverified"), verified,
                                 {"action": "upload", "ref": ref,
                                  "delivery": "verified" if verified else "unverified", "effect_verified": False,
                                  "name": file["name"], "media_type": file["mimeType"],
                                  "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    def _download_on_browser_thread(self, ref: str) -> BrowserToolOutput:
        cancellation.check()
        try:
            page, handle = self._resolve_ref(ref)
        except RuntimeError as exc:
            return self._stale_ref_output("download", ref, exc)
        try:
            with page.expect_download(timeout=20_000) as pending:
                handle.click(timeout=5_000, no_wait_after=True)
            download = pending.value
            path = download.path()
            if path is None:
                raise RuntimeError("download path unavailable")
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            self._invalidate_refs()
            return BrowserToolOutput(f"ERROR: download failed: {exc}", False,
                                     {"action": "download", "ref": ref, "delivery": "uncertain"})
        self._invalidate_refs()
        return BrowserToolOutput("Download completed", True,
                                 {"action": "download", "ref": ref, "delivery": "verified",
                                  "suggested_filename": download.suggested_filename, "path": str(path)})

    def _capture_on_browser_thread(self) -> dict[str, object]:
        page = self._page_on_browser_thread()
        response_status, content_type = self._navigation_facts.get(id(page), (None, ""))
        invalid = (_validate_page_status(page.url, response_status)
                   or _validate_page_content_type(page.url, content_type))
        if invalid is not None:
            return _browser_attempt(invalid, page.url)
        html, text = _fetch_page_content_after_settle(page)
        invalid = _validate_page_observation(page, page.url, html, text)
        if invalid is not None:
            return _browser_attempt(invalid, page.url)
        truncated = len(text) > _MAX_PAGE_CHARS
        if truncated:
            text = text[:_MAX_PAGE_CHARS]
        result: dict[str, object] = {"status": "ok", "url": page.url, "title": extract_title(html),
                                     "text": text, "truncated": truncated, "observation": "current_page"}
        return _browser_attempt(result, page.url)

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
            f"ERROR: {error}", False,
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
        return "checkbox" if str(metadata.get("type") or "").lower() == "checkbox" else "textbox"
    if tag == "select":
        return "combobox"
    return "generic"


__all__ = ["BrowserSession", "BrowserToolOutput"]
