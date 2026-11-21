"""Retained node identities survive observations, never document or record replacement."""
from __future__ import annotations

from dataclasses import replace

import pytest

from codey.automation.browser_refs import ElementRecord, ElementRegistry
from codey.automation.browser_session import BrowserSession
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.types.json_value import JsonObject, require_json_string
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser

DOCUMENT: JsonObject = {"target_id": "page", "loader_id": "document", "url": URL}


def test_registry_reuses_only_the_same_document_node_and_semantics() -> None:
    registry = ElementRegistry[str](capacity=2)
    original = ElementRecord("handle", DOCUMENT, 7, "button", "Load", None, None, None, 1, "record-A")
    ref = registry.observe_node(original)
    assert registry.observe_node(replace(original, handle="fresh-handle", revision=2)) == ref
    assert registry.resolve(ref, DOCUMENT).handle == "fresh-handle"
    changed = registry.observe_node(replace(original, semantics_json="record-B"))
    assert changed != ref
    with pytest.raises(ValueError, match="stale_ref"):
        registry.resolve(ref, {**DOCUMENT, "loader_id": "reloaded"})
    registry.observe_node(replace(original, backend_node_id=8))
    with pytest.raises(ValueError, match="unknown_ref"):
        registry.resolve(ref, DOCUMENT)
    assert len(registry.records) == 2
    registry.invalidate_document(DOCUMENT)
    assert not registry.records


def test_live_refs_survive_fill_and_reobservation(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    browser = production_browser[2]
    assert browser.navigate(URL).ok
    install(browser, '<input aria-label="City"><button onclick="this.dataset.clicked=1">Load</button>')
    first = elements(browser.snapshot().observation)
    fill = require_json_string(first[0]["ref"], "ref")
    click = require_json_string(first[1]["ref"], "ref")
    assert browser.fill(fill, "福州").ok
    second = elements(browser.snapshot().observation)
    assert [row["ref"] for row in first] == [row["ref"] for row in second]
    assert browser.click(click).ok
    assert browser.snapshot().ok
    assert browser.fill(fill, "上海").ok


def test_same_node_representing_another_record_rejects_the_old_ref(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    browser = production_browser[2]
    assert browser.navigate(URL).ok
    install(browser, '<section data-key="record-A"><button>Details</button></section>')
    original = elements(browser.snapshot().observation)[0]
    ref = require_json_string(original["ref"], "ref")
    assert browser.evaluate("document.querySelector('section').dataset.key='record-B'").ok
    rejected = browser.click(ref)
    assert not rejected.ok and rejected.observation["delivery"] == "not_delivered"
    assert rejected.observation["reason"] == "stale_ref"
    current = elements(browser.snapshot().observation)[0]
    assert current["ref"] != ref
