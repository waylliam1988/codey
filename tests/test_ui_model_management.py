"""The shipped UI manages sources with one shared, provider-neutral view."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.fixture
def models(page):
    state = {
        "ok": True,
        "preferences": {
            "revision": 0,
            "sources": {
                "websites": {"enabled": True, "models": ["deepseek", "qwen"]},
                "local": {"enabled": True, "models": ["actual-local-id"]},
                "partner": {"enabled": True, "models": ["chosen-model"]},
            },
        },
        "sources": [
            {
                "id": "websites",
                "label": "Websites",
                "models": [
                    {"id": "deepseek", "name": "DeepSeek"},
                    {"id": "qwen", "name": "Qwen"},
                    {"id": "mimo", "name": "MiMo"},
                ],
            },
            {
                "id": "partner",
                "label": "Optional connection",
                "discoverable": True,
                "models": [
                    {"id": "chosen-model", "name": "Chosen model"},
                    {"id": "new-model", "name": "Unselected model"},
                ],
            },
            {
                "id": "local",
                "label": "Local",
                "connection_editor": True,
                "discoverable": True,
                "models": [{"id": "actual-local-id", "name": "Actual server title"}],
            },
        ],
        "in_use": [],
    }
    posts = []

    def settings(route):
        if route.request.method == "POST":
            posts.append(route.request.post_data_json)
            state["preferences"]["sources"] = copy.deepcopy(posts[-1]["sources"])
            state["preferences"]["revision"] += 1
        route.fulfill(json=state)

    page.route("**/api/model_settings", settings)
    page.route(
        "**/api/api_models",
        lambda route: route.fulfill(
            json={
                "connections": [
                    {"id": "local", "base_url": "http://fixture/v1", "models": state["sources"][2]["models"]},
                    {"id": "partner", "models": state["sources"][1]["models"]},
                ]
            }
        ),
    )
    page.route(
        "**/api/provider_catalog",
        lambda route: route.fulfill(
            json={
                "default": "deepseek",
                "providers": [
                    {"id": "deepseek", "label": "DeepSeek"},
                    {"id": "qwen", "label": "Qwen"},
                    {"id": "mimo", "label": "MiMo"},
                    {"id": "partner", "label": "Optional connection"},
                    {"id": "local", "label": "Local"},
                ],
            }
        ),
    )
    page.route(
        "**/api/providers",
        lambda route: route.fulfill(
            json={
                "default": "deepseek",
                "providers": [
                    {"id": "deepseek", "label": "DeepSeek"},
                    {"id": "qwen", "label": "Qwen"},
                    {"id": "mimo", "label": "MiMo"},
                    {"id": "partner", "label": "Optional connection"},
                    {"id": "local", "label": "Local"},
                ],
            }
        ),
    )
    page.reload()
    page.wait_for_load_state("networkidle")
    page.wait_for_function("CodeyUiState.current().active_id === 'a'")
    return state, posts


def test_uniform_source_switches_keep_choices_and_apply_only_after_save(page, models):
    state, posts = models
    page.locator("#task").fill("保留草稿")
    page.locator("#btn-settings").click()
    expect(page.locator(".model-source-toggle")).to_have_count(3, timeout=1500)
    websites = page.locator('.model-source[data-source-id="websites"]')
    websites.locator("summary").click()
    expect(websites.get_by_role("checkbox", name="Use DeepSeek", exact=True)).to_be_checked()
    websites.get_by_role("checkbox", name="Enable Websites", exact=True).uncheck()
    assert posts == []
    expect(page.locator("#provider-name")).to_have_text("DeepSeek")
    page.locator("#model-settings-save").click()
    expect(page.locator("#local-config-pop")).to_be_hidden()
    expect(page.locator("#provider-name")).to_have_text("DeepSeek · Disabled")
    expect(page.locator("#send")).to_be_disabled()
    expect(page.locator("#task")).to_have_value("保留草稿")
    assert state["preferences"]["sources"]["websites"]["models"] == ["deepseek", "qwen"]
    page.locator("#btn-settings").click()
    page.get_by_role("checkbox", name="Enable Websites", exact=True).check()
    page.locator("#model-settings-save").click()
    page.locator("#provider-button").click()
    expect(page.locator('.provider-item[data-provider="mimo"]')).to_have_count(0)
    expect(page.locator('.provider-item[data-provider="deepseek"]')).to_be_visible()
    expect(page.locator("#send")).to_be_enabled()


def test_actual_model_names_and_new_catalog_entries_do_not_change_allowlist(page, models):
    page.locator("#provider-button").click()
    expect(page.locator('[data-model="actual-local-id"]')).to_have_text("Actual server title", timeout=1500)
    expect(page.locator('[data-model="new-model"]')).to_have_count(0)
    page.evaluate("CodeyProviderUI.applyApiModels([{id:'partner',models:[{id:'new-model',name:'New arrival'}]}])")
    expect(page.locator('[data-model="new-model"]')).to_have_count(0)
    assert models[0]["preferences"]["sources"]["partner"]["models"] == ["chosen-model"]


def test_removing_optional_source_removes_ui_without_rewriting_old_chat(page, models):
    page.evaluate("""() => {
      const s=CodeyUiState.current().sessions.find(s=>s.id==='a');
      s.provider='partner';s.modelSelection={connection_id:'partner',model:'chosen-model',name:'Chosen model',efforts:{}};
      CodeyComposer.syncSession();CodeyProviderUI.sync('partner');
    }""")
    page.locator("#task").fill("old draft")
    state, _ = models
    state["sources"] = [s for s in state["sources"] if s["id"] != "partner"]
    del state["preferences"]["sources"]["partner"]
    state["preferences"]["revision"] += 1
    page.evaluate("CodeyModels.applySettings", state)
    expect(page.locator("#provider-name")).to_have_text("Chosen model · Unavailable", timeout=1500)
    expect(page.locator("#send")).to_be_disabled()
    expect(page.locator("#task")).to_have_value("old draft")
    page.locator("#btn-settings").click()
    expect(page.locator('.model-source[data-source-id="partner"]')).to_have_count(0)
    assert page.evaluate("CodeyUiState.current().sessions.find(s=>s.id==='a').modelSelection.model") == "chosen-model"


def test_shared_frontend_contains_no_optional_vendor_logic():
    root = Path(__file__).resolve().parents[1] / "codey" / "web"
    for file in root.glob("assets/*.js"):
        assert "opencode" not in file.read_text(encoding="utf-8").lower(), file.name
    assert "opencode" not in (root / "index.html").read_text(encoding="utf-8").lower()


def test_all_sources_off_is_quiet_and_picker_never_sends_or_discovers(page, models):
    calls = []
    page.on("request", lambda request: calls.append(request.url))
    page.locator("#btn-settings").click()
    for label in ("Websites", "Optional connection", "Local"):
        page.get_by_role("checkbox", name="Enable " + label, exact=True).uncheck()
    page.locator("#model-settings-save").click()
    expect(page.locator("#local-config-pop")).to_be_hidden()
    page.locator("#task").fill("keep me")
    expect(page.locator("#model-notice")).to_contain_text("No models enabled")
    page.locator("#provider-button").click()
    expect(page.locator(".provider-item")).to_have_count(0)
    expect(page.locator("#task")).to_have_value("keep me")
    page.keyboard.press("Escape")
    expect(page.locator("#task")).to_have_value("keep me")
    page.locator("#task").focus()
    page.keyboard.press("Enter")
    assert not any(
        url.endswith(("/api/run", "/api/model_catalog", "/api/local_provider", "/api/api_models")) for url in calls
    )
    expect(page.locator("#task")).to_have_value("keep me")


def test_cancel_discards_draft_choices_and_keeps_focus(page, models):
    page.locator("#btn-settings").click()
    page.get_by_role("checkbox", name="Enable Websites", exact=True).uncheck()
    page.locator("#model-settings-cancel").click()
    expect(page.locator("#btn-settings")).to_be_focused()
    assert models[1] == []
    page.locator("#btn-settings").click()
    expect(page.get_by_role("checkbox", name="Enable Websites", exact=True)).to_be_checked()


def test_stale_settings_reply_cannot_reenable_a_source(page, models):
    stale = copy.deepcopy(models[0])
    current = copy.deepcopy(stale)
    current["preferences"]["revision"] += 1
    current["preferences"]["sources"]["websites"]["enabled"] = False
    page.evaluate("CodeyModels.applySettings", current)
    page.evaluate("CodeyModels.applySettings", stale)
    expect(page.locator("#provider-name")).to_have_text("DeepSeek · Disabled")


def test_used_model_cannot_be_cleared_but_unused_sources_stay_editable(page, models):
    models[0]["in_use"] = [{"provider": "qwen", "model": ""}]
    page.locator("#btn-settings").click()
    websites = page.locator('.model-source[data-source-id="websites"]')
    websites.locator("summary").click()
    expect(websites.get_by_role("checkbox", name="Enable Websites", exact=True)).to_be_disabled()
    expect(page.get_by_role("checkbox", name="Enable Local", exact=True)).to_be_enabled()
    websites.get_by_role("button", name="Clear", exact=True).click()
    expect(websites.get_by_role("checkbox", name="Use Qwen", exact=True)).to_be_checked()
    expect(websites.get_by_role("checkbox", name="Use DeepSeek", exact=True)).not_to_be_checked()


def test_refresh_catalog_keeps_staged_choices_and_never_selects_new_models(page, models):
    page.route(
        "**/api/model_catalog",
        lambda route: route.fulfill(
            json={
                "ok": True,
                "source": {
                    **models[0]["sources"][1],
                    "models": [*models[0]["sources"][1]["models"], {"id": "late", "name": "New arrival"}],
                },
            }
        ),
    )
    page.locator("#btn-settings").click()
    source = page.locator('.model-source[data-source-id="partner"]')
    source.locator("summary").click()
    source.get_by_role("checkbox", name="Use Chosen model", exact=True).uncheck()
    source.get_by_role("button", name="Refresh models", exact=True).click()
    expect(source.get_by_role("checkbox", name="Use New arrival", exact=True)).not_to_be_checked()
    expect(source.get_by_role("checkbox", name="Use Chosen model", exact=True)).not_to_be_checked()
    page.locator("#model-settings-save").click()
    assert models[1][0]["sources"]["partner"]["models"] == []


@pytest.mark.parametrize("source_id", ["websites", "partner", "local"])
@pytest.mark.parametrize("action", ["uncheck", "clear"])
def test_empty_selection_turns_source_off_and_explicit_selection_turns_it_on(page, models, source_id, action):
    page.locator("#btn-settings").click()
    source = page.locator(f'.model-source[data-source-id="{source_id}"]')
    source.locator("summary").first.click()
    toggle = source.locator(".model-source-toggle")
    if action == "clear":
        source.get_by_role("button", name="Clear", exact=True).click()
    else:
        for item in source.locator(".model-choice input").all():
            item.uncheck()
    expect(source.locator(".model-source-count")).to_have_text("0 selected")
    expect(toggle).not_to_be_checked()
    expect(toggle).to_be_disabled()
    assert models[1] == []
    page.locator("#model-settings-save").click()
    expect(page.locator("#local-config-pop")).to_be_hidden()
    assert models[0]["preferences"]["sources"][source_id] == {"enabled": False, "models": []}
    page.locator("#btn-settings").click()
    expect(source.locator(".model-source-count")).to_have_text("0 selected")
    if source.locator("details").first.get_attribute("open") is None:
        source.locator("summary").first.click()
    expect(toggle).not_to_be_checked()
    source.locator(".model-choice input").first.check()
    expect(toggle).to_be_checked()
    expect(toggle).to_be_enabled()
    page.locator("#model-settings-save").click()
    expect(page.locator("#local-config-pop")).to_be_hidden()
    assert models[0]["preferences"]["sources"][source_id]["enabled"] is True


def test_select_all_reenables_empty_source_without_restoring_other_sources(page, models):
    previous = copy.deepcopy(models[0]["preferences"]["sources"])
    page.locator("#btn-settings").click()
    source = page.locator('.model-source[data-source-id="websites"]')
    source.locator("summary").click()
    source.get_by_role("button", name="Clear", exact=True).click()
    source.get_by_role("button", name="Select all", exact=True).click()
    expect(source.locator(".model-source-toggle")).to_be_checked()
    expect(source.locator(".model-source-count")).to_have_text("3 selected")
    page.locator("#model-settings-save").click()
    expect(page.locator("#local-config-pop")).to_be_hidden()
    for source_id in ("local", "partner"):
        assert models[0]["preferences"]["sources"][source_id] == previous[source_id]
