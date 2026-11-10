"""CDP resource identity carried by ordinary tool receipts, never a second store."""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.sync_api import Page, sync_playwright

from codey.automation.browser import Session
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)


@dataclass(frozen=True)
class PageBinding:
    target_id: str
    context_id: str
    owned: bool

    def payload(self) -> JsonObject:
        return {"target_id": self.target_id, "context_id": self.context_id, "owned": self.owned}

    @classmethod
    def read(cls, value: object) -> PageBinding:
        row = decode_json_object(value)
        if set(row) != {"target_id", "context_id", "owned"}:
            raise ValueError("invalid browser page binding")
        target = require_json_string(row["target_id"], "target_id")
        if not target:
            raise ValueError("browser target_id must not be empty")
        return cls(target, require_json_string(row["context_id"], "context_id"),
                   require_json_boolean(row["owned"], "owned"))


@dataclass(frozen=True)
class BrowserBinding:
    port: int
    browser_id: str
    active_target: str
    pages: tuple[PageBinding, ...]

    def payload(self) -> JsonObject:
        pages: list[JsonValue] = [page.payload() for page in self.pages]
        return {"version": 1, "port": self.port, "browser_id": self.browser_id,
                "active_target": self.active_target, "pages": pages}

    @classmethod
    def read(cls, value: object) -> BrowserBinding:
        row = decode_json_object(value)
        if (set(row) != {"version", "port", "browser_id", "active_target", "pages"}
                or require_json_integer(row["version"], "version") != 1):
            raise ValueError("invalid browser resource binding")
        port = require_json_integer(row["port"], "port")
        identity = require_json_string(row["browser_id"], "browser_id")
        active = require_json_string(row["active_target"], "active_target")
        raw = row["pages"]
        if not 0 < port < 65536 or not identity or not isinstance(raw, list):
            raise ValueError("invalid browser resource identity")
        pages = tuple(PageBinding.read(page) for page in raw)
        targets = {page.target_id for page in pages}
        if len(targets) != len(pages) or active not in targets:
            raise ValueError("invalid active browser target")
        return cls(port, identity, active, pages)


def browser_endpoint(port: int) -> str:
    """Discover only the recorded loopback port; its browser UUID must also match."""
    with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
        version = decode_json_object(json.loads(response.read()))
    endpoint = require_json_string(version.get("webSocketDebuggerUrl"), "browser endpoint")
    parsed = urlparse(endpoint)
    if (parsed.scheme != "ws" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.port != port or not parsed.path.startswith("/devtools/browser/")):
        raise ValueError("invalid loopback browser endpoint")
    return endpoint


def browser_identity(endpoint: str) -> str:
    return urlparse(endpoint).path.removeprefix("/devtools/browser/")


def page_binding(page: Page, *, owned: bool) -> PageBinding:
    cdp = page.context.new_cdp_session(page)
    try:
        result = decode_json_object(cdp.send("Target.getTargetInfo"))
        info = decode_json_object(result["targetInfo"])
        return PageBinding(require_json_string(info.get("targetId"), "targetId"),
                           require_json_string(info.get("browserContextId", ""), "browserContextId"), owned)
    finally:
        cdp.detach()


def describe_binding(session: Session, current: Page, owned: Sequence[Page], borrowed: Sequence[Page],
                     *, browser_id: str) -> BrowserBinding:
    identity = browser_id or browser_identity(browser_endpoint(session.cdp_port))
    owned_ids = {id(page) for page in owned}
    pages = tuple(page_binding(page, owned=id(page) in owned_ids)
                  for page in (*owned, *borrowed) if not page.is_closed())
    active = page_binding(current, owned=id(current) in owned_ids)
    if active.target_id not in {page.target_id for page in pages}:
        pages = (*pages, active)
    return BrowserBinding(session.cdp_port, identity, active.target_id, pages)


def connect_binding(binding: BrowserBinding, *, timeout_ms: int) -> tuple[Session, tuple[tuple[PageBinding, Page], ...]]:
    """Connect the exact original browser and targets, without navigation or creation."""
    endpoint = browser_endpoint(binding.port)
    if browser_identity(endpoint) != binding.browser_id:
        raise RuntimeError("browser recovery failed: original browser instance is gone")
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
        expected = {page.target_id: page for page in binding.pages}
        matched: list[tuple[PageBinding, Page]] = []
        active: Page | None = None
        for context in browser.contexts:
            for page in context.pages:
                observed = page_binding(page, owned=False)
                prior = expected.get(observed.target_id)
                if prior is not None and observed.context_id == prior.context_id:
                    matched.append((prior, page))
                    if observed.target_id == binding.active_target:
                        active = page
        if active is None:
            raise RuntimeError("browser recovery failed: original tab is gone; no replacement was selected")
        return Session(pw, browser, active, cdp_port=binding.port), tuple(matched)
    except BaseException:
        pw.stop()
        raise
