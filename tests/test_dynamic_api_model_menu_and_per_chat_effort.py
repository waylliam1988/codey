"""API directory updates and per-chat effort use the generic composer path."""
from playwright.sync_api import expect

from tests.test_ui_workflow import (  # noqa: F401 -- shared real HTTP/browser fixtures
    page,
    select_connection_models,
    ui_browser,
)


def test_dynamic_api_menu_selects_responses_model_without_local_headers_or_ui_branch(page):  # noqa: F811 -- imported pytest fixture
    page.evaluate("""() => CodeyProviderUI.applyConfig({default:'deepseek',providers:[
      {id:'deepseek',label:'DeepSeek'},{id:'local',label:'Local'},{id:'zen',label:'OpenCode Zen'}]})""")
    payload = {"id": "zen", "label": "OpenCode Zen", "models": [
        {"id": "muse-fixture", "name": "Muse Fixture", "protocol": "openai-responses", "efforts": ["low", "high", "xhigh"]},
        {"id": "chat-fixture", "name": "Chat Fixture", "protocol": "openai-completions", "efforts": []},
    ]}
    page.route("**/api/api_models", lambda route: route.fulfill(json={"connections": [payload]}))
    select_connection_models(page, payload)
    page.locator("#provider-button").click()
    page.locator('[data-model="muse-fixture"]').click()
    expect(page.locator("#provider-name")).to_have_text("Muse Fixture")
    page.locator("#effort-button").click()
    page.get_by_role("button", name="Thinking effort: XHigh", exact=True).click()
    selection = page.evaluate("CodeyProviderUI.runSelection(CodeyUiState.current().sessions[0], 'zen')")
    assert selection == {"model_selection": {"connection_id": "zen", "base_url": "", "model": "muse-fixture", "effort": "xhigh"}}
    page.evaluate("switchSession('b')")
    expect(page.locator("#effort-chooser")).to_be_hidden()
    page.evaluate("switchSession('a')")
    expect(page.locator("#effort-name")).to_have_text("XHigh")
    payload["models"] = [{"id": "new-free", "name": "New Free", "efforts": []}]
    page.evaluate("CodeyProviderUI.applyApiModels", [payload])
    page.locator("#provider-button").click()
    expect(page.locator('[data-model="muse-fixture"]')).to_have_count(0)
    expect(page.locator('[data-model="new-free"]')).to_have_count(0)
    # Discovery cannot grant consent. An explicit Settings choice reveals it.
    select_connection_models(page, payload, ["new-free"])
    expect(page.locator('[data-model="new-free"]')).to_be_visible()
    # Removal cannot reinterpret this chat as a different model.
    assert page.evaluate("CodeyUiState.current().sessions[0].modelSelection.model") == "muse-fixture"


def test_removed_connection_stays_readable_and_cannot_silently_send_to_default(page):  # noqa: F811 -- imported pytest fixture
    state = {"active_id": "a", "projects": [], "sessions": [{"id": "a", "provider": "retired-api", "title": "Old chat",
             "messages": [{"type": "asst", "text": "Stored answer"}],
             "modelSelection": {"connection_id": "retired-api", "model": "old-model", "efforts": {}}}]}
    page.route("**/api/ui_state", lambda route: route.fulfill(json={"state": state}))
    requests = []
    page.route("**/api/run", lambda route: (requests.append(route.request.post_data_json), route.fulfill(status=400, json={"error": "unsupported provider"})))
    page.reload()
    expect(page.locator(".msg.asst")).to_contain_text("Stored answer")
    page.locator("#task").fill("continue")
    expect(page.locator("#send")).to_be_disabled()
    page.locator("#task").press("Enter")
    assert requests == []
    expect(page.locator("#task")).to_have_value("continue")
    assert page.evaluate("CodeyUiState.current().sessions.find(s=>s.id==='a').provider") == "retired-api"


def test_original_chat_send_does_not_take_effort_from_currently_visible_chat(page):  # noqa: F811 -- imported pytest fixture
    select_connection_models(page, {"id": "zen", "label": "OpenCode Zen", "models": [
        {"id": "model", "name": "Model", "efforts": ["low", "high"]},
    ]})
    page.evaluate("""() => {
      CodeyProviderUI.applyConfig({default:'deepseek',providers:[{id:'deepseek',label:'DeepSeek'},{id:'zen',label:'OpenCode Zen'}]});
      CodeyProviderUI.applyApiModels([{id:'zen',models:[{id:'model',name:'Model',efforts:['low','high']}]}]);
      const sessions = CodeyUiState.current().sessions;
      sessions[0].provider='zen'; sessions[0].modelSelection={connection_id:'zen',model:'model',effort:'low'};
      sessions[1].provider='zen'; sessions[1].modelSelection={connection_id:'zen',model:'model',effort:'high'};
      switchSession('b');
    }""")
    expect(page.locator('#effort-name')).to_have_text('High')
    selected = page.evaluate("CodeyProviderUI.runSelection(CodeyUiState.current().sessions.find(s => s.id === 'a'), 'zen')")
    assert selected['model_selection']['effort'] == 'low'


def test_explicit_selection_fixture_keeps_availability_catalog_consistent(page):  # noqa: F811
    select_connection_models(page, {"id": "optional", "label": "Optional", "models": [
        {"id": "selected", "name": "Selected", "efforts": []},
    ]})
    status = page.evaluate("async () => (await fetch('/api/providers')).json()")
    assert "optional" in [item["id"] for item in status["providers"]]
    page.evaluate("CodeyProviderUI.refreshStatus(true)")
    page.wait_for_load_state("networkidle")
    page.locator("#provider-button").click()
    expect(page.locator('[data-model="selected"]')).to_be_visible()
