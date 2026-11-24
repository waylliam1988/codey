"""Native operations, scoped observations and image coordinates on actual Chromium."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from codey.automation.browser_contracts import BrowserAction
from codey.automation.browser_query import QueryObservationError
from codey.automation.browser_session import BrowserSession
from codey.types.json_value import decode_json_object, parse_json_object, require_json_string
from tests.support.task_browser import browser_for, install

URL = "https://example.com/native-controls"


@pytest.fixture()
def browser() -> Iterator[BrowserSession]:
    resource = browser_for(lambda route: route.fulfill(content_type="text/html", body="<title>Native controls</title><main>Ready</main>"))
    try:
        assert resource.navigate(URL).ok
        yield resource
    finally:
        resource.close()


def ref(browser: BrowserSession, name: str) -> str:
    snapshot = browser.snapshot()
    assert snapshot.ok, snapshot.model_text
    values = snapshot.observation["elements"]
    assert isinstance(values, list)
    return next(require_json_string(row["ref"], "ref") for raw in values for row in (decode_json_object(raw),) if row["name"] == name)


def test_native_keys_selection_checkbox_and_hover(browser: BrowserSession) -> None:
    install(browser, '<input aria-label="City" onkeydown="if(event.key===\'Enter\') document.querySelector(\'output\').textContent=this.value">'
        '<select aria-label="Size"><option value=small>Small</option><option value=large>Large</option></select>'
        '<label><input type=checkbox>Verified</label><button onmouseenter="this.dataset.hovered=1">Hover</button><output></output>')
    city = ref(browser, "City")
    assert browser.fill(city, "Fuzhou").ok
    assert browser.action(BrowserAction.PRESS, {"ref": city, "key": "Enter"}).ok
    assert "Fuzhou" in str(browser.capture()["text"])
    size = browser.action(BrowserAction.SELECT, {"ref": ref(browser, "Size"), "values": ["large"]})
    assert size.ok and size.observation["values"] == ["large"]
    checked = browser.action(BrowserAction.CHECK, {"ref": ref(browser, "Verified"), "checked": True})
    assert checked.ok and checked.observation["checked"] is True
    assert browser.action(BrowserAction.HOVER, {"ref": ref(browser, "Hover")}).ok
    assert browser.evaluate("document.querySelector('button').dataset.hovered").observation["value"] == "1"


def test_popup_is_listed_without_switching_the_original_page_and_dialog_has_an_independent_exit(browser: BrowserSession) -> None:
    install(browser, '<button onclick="window.open(\'https://example.com/popup\')">Open popup</button>'
                     '<button onclick="alert(\'Confirm record\')">Ask</button>')
    original = browser.document_identity()
    assert browser.click(ref(browser, 'Open popup')).ok
    assert browser.wait('popup', '', 1000).ok
    tabs = browser.tab_list().observation['tabs']
    assert isinstance(tabs, list) and len(tabs) == 2
    assert browser.document_identity() == original
    assert browser.tab_switch(1).ok
    assert 'popup' in str(browser.document_identity())
    assert browser.tab_switch(0).ok
    # A dialog can suspend the caller's native input acknowledgement. It must
    # remain observable/respondable through the separate control command path.
    from concurrent.futures import ThreadPoolExecutor

    button = ref(browser, 'Ask')
    with ThreadPoolExecutor(max_workers=1) as executor:
        clicking = executor.submit(browser.click, button)
        assert browser.wait('dialog', '', 3000).ok
        assert browser.action(BrowserAction.DIALOG).observation['message'] == 'Confirm record'
        assert browser.action(BrowserAction.DIALOG_RESPOND, {'accept': False}).ok
        assert clicking.result(timeout=5).ok
    assert browser.snapshot().ok


def test_delta_display_retains_a_full_receipt_and_requires_identical_scope(browser: BrowserSession) -> None:
    install(browser, '<section><input aria-label="City"></section><aside>Other region</aside>')
    original = browser.snapshot(selector="section")
    assert original.ok
    revision = original.observation["revision"]
    assert isinstance(revision, int)
    delta = browser.snapshot(selector="section", base_revision=revision)
    assert delta.ok
    view = parse_json_object(delta.model_text)
    assert view["kind"] == "delta" and view["added"] == [] and view["changed"] == []
    assert "elements" not in view and "text" not in view
    assert delta.observation["elements"] == original.observation["elements"]
    changed_scope = browser.snapshot(selector="aside", base_revision=revision)
    assert changed_scope.ok and parse_json_object(changed_scope.model_text)["kind"] == "full"
    input_only = browser.snapshot(selector="input")
    rows = input_only.observation["elements"]
    assert input_only.ok and isinstance(rows, list) and len(rows) == 1


def test_delta_displays_reordered_controls_in_current_order(browser: BrowserSession) -> None:
    install(browser, '<section><button>First</button><button>Second</button></section>')
    original = browser.snapshot(selector="section")
    assert original.ok
    revision = original.observation["revision"]
    assert isinstance(revision, int)
    assert browser.evaluate("document.querySelector('section').prepend(document.querySelector('button:last-child'))").ok
    observed = browser.snapshot(selector="section", base_revision=revision)
    assert observed.ok
    view = parse_json_object(observed.model_text)
    assert view["kind"] == "full"
    before, after = original.observation["elements"], view["elements"]
    assert isinstance(before, list) and isinstance(after, list)
    assert [decode_json_object(row)["ref"] for row in after] == [decode_json_object(row)["ref"] for row in reversed(before)]
    assert [decode_json_object(row)["name"] for row in after] == ["Second", "First"]


def test_div_query_uses_an_explicit_observed_scope_and_never_confirms_an_unchanged_result(browser: BrowserSession) -> None:
    install(browser, '<button onclick="document.querySelector(\'#result\').textContent=\'173 QUARTZ_851\'">Query</button>'
                     '<div id=result>Waiting</div>')
    region = browser.action(BrowserAction.LOCATE, {"selector": "#result"})
    candidates = region.observation["candidates"]
    assert isinstance(candidates, list) and len(candidates) == 1
    result_ref = require_json_string(decode_json_object(candidates[0])["ref"], "result ref")
    query_ref = ref(browser, "Query")
    clicked = browser.click(query_ref, query=True, result_ref=result_ref)
    assert clicked.ok, clicked.model_text
    baseline = decode_json_object(clicked.observation["query_baseline"])
    assert browser.capture_query_result(baseline, pending="pending")["query_confirmed"] is True
    same = browser.click(query_ref, query=True, result_ref=result_ref)
    assert same.ok
    with pytest.raises(QueryObservationError) as unchanged:
        browser.capture_query_result(decode_json_object(same.observation["query_baseline"]), pending="pending")
    assert unchanged.value.reason == "query_update_unconfirmed"
    assert browser.evaluate("document.querySelector('#result').outerHTML='<div id=result>Replacement</div>'").ok
    with pytest.raises(QueryObservationError):
        browser.capture_query_result(baseline, pending="pending")


def test_scrolled_image_mapping_and_pixel_validation(browser: BrowserSession) -> None:
    install(browser, '<div style="height:900px"></div><button onclick="this.textContent=\'Clicked\'">Click here</button><div style="height:900px"></div>')
    assert browser.evaluate("window.scrollTo(0,800)").ok
    image = browser.action(BrowserAction.SCREENSHOT, {"annotate": True})
    assert image.ok and image.binary.startswith(b"\x89PNG")
    visual = decode_json_object(image.observation["image"])
    mapping = visual["mapping"]
    assert isinstance(mapping, list) and len(mapping) == 1
    box = decode_json_object(decode_json_object(mapping[0])["box"])
    x, y = box["x"], box["y"]
    assert isinstance(x, int | float) and isinstance(y, int | float)
    clicked = browser.action(BrowserAction.VISUAL_CLICK, {"observation_ref": visual["observation_ref"], "x": x + 5, "y": y + 5})
    assert clicked.ok, clicked.model_text
    stale = browser.action(BrowserAction.VISUAL_CLICK, {"observation_ref": visual["observation_ref"], "x": x + 5, "y": y + 5})
    assert not stale.ok and stale.observation["delivery"] == "not_delivered"


def test_binary_file_selection_and_download(browser: BrowserSession) -> None:
    install(browser, '<label>Attachment<input type=file></label><a download="fixture.txt" href="data:text/plain;base64,aGVsbG8=">Download</a>')
    selected = browser.upload(ref(browser, "Attachment"), {"name": "upload.txt", "mimeType": "text/plain", "buffer": b"uploaded"})
    assert selected.ok and selected.observation["delivery"] == "verified"
    assert selected.observation["effect_verified"] is False
    downloaded = browser.download(ref(browser, "Download"))
    assert downloaded.ok, downloaded.model_text
    assert downloaded.binary == b"hello" and downloaded.observation["suggested_filename"] == "fixture.txt"
