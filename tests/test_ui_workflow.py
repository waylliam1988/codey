"""User workflows for the quiet composer, reading, and inspection surfaces."""
from __future__ import annotations

import threading
from io import BytesIO
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.http_plumbing import resolve_web_asset, send_index

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def ui_browser(tmp_path_factory):
    app_context = server.AppContext(tmp_path_factory.mktemp("ui-workflow") / "state")
    original = server.STATE
    server.STATE = app_context
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            yield browser, httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/")
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        app_context.close()
        server.STATE = original


@pytest.fixture
def page(ui_browser):
    browser, url = ui_browser
    app_context = server.STATE
    assert app_context is not None
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    boot_errors = []
    page.on("pageerror", lambda error: boot_errors.append(str(error)))
    page.on("requestfailed", lambda request: boot_errors.append(f"{urlsplit(request.url).path}: {request.failure}"))
    page.add_init_script("window.__TEST_BRIDGE__ = true; window.EventSource = class { static OPEN = 1; readyState = 1; close() {} };")

    document = mock.Mock(headers={}, wfile=BytesIO())
    send_index(document)
    index_body = document.wfile.getvalue()
    page.route(lambda url: urlsplit(url).path in {"/", "/index.html"},
               lambda route: route.fulfill(body=index_body, content_type="text/html; charset=utf-8"))

    def asset(route):
        resolved = resolve_web_asset(urlsplit(route.request.url).path)
        assert resolved is not None, f"Missing shipped asset: {route.request.url}"
        path, content_type = resolved
        route.fulfill(body=path.read_bytes(), content_type=content_type)

    page.route("**/assets/**", asset)
    catalog = [{"id": key, "label": label, "available": True} for key, label in
               [("deepseek", "DeepSeek"), ("mimo", "MiMo"), ("stepfun", "StepFun"),
                ("qwen", "Qwen"), ("glm", "GLM"), ("local", "Local")]]
    ui_state = {"active_id": "a", "sessions": [
        {"id": "a", "title": "Alpha", "provider": "deepseek", "messages": []},
        {"id": "b", "title": "Beta", "provider": "mimo", "messages": []}], "projects": []}

    def api(route):
        parsed = urlsplit(route.request.url)
        path = parsed.path.split("/api/")[-1]
        data = {"ok": True}
        if path in {"provider_catalog", "providers"}:
            data = {"providers": catalog, "default": "deepseek"}
        elif path == "model_settings":
            data = model_management_payload()
        elif path == "ui_state" and route.request.method == "GET":
            data = {"state": ui_state}
        elif path.startswith("sessions/") and path.endswith("/messages"):
            session_id = path.split("/")[1]
            query = parse_qs(parsed.query)
            try:
                data = app_context.durable.history_page(
                    session_id,
                    before=(query.get("before") or [None])[0],
                    limit=int((query.get("limit") or ["100"])[0]),
                )
            except (KeyError, ValueError):
                data = {
                    "schema_version": 1,
                    "session_id": session_id,
                    "epoch": app_context.durable.database_id,
                    "snapshot_revision": 0,
                    "items": [],
                    "before_cursor": None,
                    "has_more": False,
                }
        elif path == "pick_folder":
            data = {"ok": True, "path": "E:/project-alpha", "name": "Project Alpha"}
        route.fulfill(json=data)

    page.route("**/api/**", api)
    try:
        # State restoration is asynchronous; frame delivery is not its clock.
        try:
            page.goto(url)
            expect(page.locator("#provider-button")).to_be_enabled(timeout=15000)
            page.wait_for_function("UiState.current().active_id === 'a'", polling=100)
        except (AssertionError, PlaywrightTimeoutError) as exc:
            status = page.locator(".status-row").all_text_contents()
            boot_state = page.evaluate("() => ({active_id: UiState?.current().active_id, "
                                  "session_ids: UiState?.current().sessions.map(s => s.id), "
                                  "visibility: document.visibilityState})")
            raise AssertionError(f"UI boot failed: {boot_errors}; status: {status}; state: {boot_state}") from exc
        page.wait_for_load_state("networkidle")
        page.codey_context = app_context
        yield page
    finally:
        page.close()


def test_long_answer_is_visible_and_pointer_selection_survives_tool_update(page):
    text = "可选择的正文 Reading code safely.\n" * 25 + "FINAL CONCLUSION"
    page.evaluate("text => { addToSession('a', {type:'asst', text}); }", text)
    body = page.locator(".msg.asst .body")
    expect(body).to_contain_text("FINAL CONCLUSION")
    assert "collapsed" not in body.get_attribute("class")
    page.locator("#chat-area").evaluate("e => e.scrollTop = 0")
    first = body.locator("p").first.bounding_box()
    page.mouse.move(first["x"] + 2, first["y"] + 8)
    page.mouse.down()
    page.mouse.move(first["x"] + 200, first["y"] + 8, steps=15)
    page.mouse.up()
    selected = page.evaluate("getSelection().toString()")
    assert selected
    page.evaluate("addToSession('a', {type:'tool',kind:'read',path:'a.py',result:'2 lines'})")
    assert page.evaluate("getSelection().toString()") == selected
    expect(page.get_by_role("button", name="Collapse", exact=True)).to_have_count(0)


def api_connection_payload(local):
    model = local.get("model", "")
    return {"id": "local", "base_url": local.get("base_url", ""), "default_model": model,
            "models": [{"id": name, "name": local.get("display_name") or name,
                        "efforts": local.get("thinking_options", []) if name == model else []}
                       for name in dict.fromkeys([*local.get("models", []), *([model] if model else [])])]}


def model_management_payload(connection=None):
    websites = [{"id": key, "name": name} for key, name in [("deepseek", "DeepSeek"), ("mimo", "MiMo"), ("stepfun", "StepFun"), ("qwen", "Qwen"), ("glm", "GLM")]]
    local = {"id": "local", "label": "Local", "connection_editor": True, "discoverable": True, "models": [], **(connection or {})}
    return {"ok": True, "preferences": {"revision": 0, "sources": {
        "websites": {"enabled": True, "models": [m["id"] for m in websites]},
        "local": {"enabled": bool(connection), "models": [m["id"] for m in local["models"]]},
    }}, "sources": [{"id": "websites", "label": "Websites", "models": websites}, local], "in_use": []}


def open_connection_settings(page):
    page.locator("#btn-settings").click()
    source = page.locator('.model-source[data-source-id="local"] > details')
    expect(source).to_be_visible()
    if source.get_attribute("open") is None:
        source.locator("summary").first.click()
    with page.expect_request("**/api/local_provider"):
        page.locator('#local-connection-editor > summary').click()
    expect(page.locator("#local-config-pop")).to_have_attribute("aria-busy", "false")


def apply_model_metadata(page, local):
    payload = api_connection_payload(local)
    page.evaluate("Models.applySettings", model_management_payload(payload))
    page.evaluate("ProviderUI.applyApiModels", [payload])
    page.evaluate("ProviderUI.applyLocalMetadata", local)


def select_connection_models(page, connection, selected=None):
    """Explicit Settings consent, separate from discovery in composer tests."""
    settings = page.evaluate("Models.snapshot()")
    settings["sources"] = [source for source in settings["sources"] if source["id"] != connection["id"]]
    settings["sources"].append(connection)
    settings["preferences"]["revision"] += 1
    settings["preferences"]["sources"][connection["id"]] = {
        "enabled": True, "models": selected if selected is not None else [model["id"] for model in connection["models"]],
    }
    providers = []
    for source in settings["sources"]:
        if source["id"] == "websites":
            providers.extend({"id": model["id"], "label": model["name"], "available": True} for model in source["models"])
        else:
            providers.append({"id": source["id"], "label": source["label"], "available": True})
    catalog = {"providers": providers, "default": "deepseek"}
    for endpoint in ("provider_catalog", "providers"):
        page.route("**/api/" + endpoint, lambda route: route.fulfill(json=catalog))
    page.evaluate("ProviderUI.applyConfig", catalog)
    page.evaluate("Models.applySettings", settings)
    page.evaluate("ProviderUI.applyApiModels", [connection])


def model_settings_route(page, *, thinking=True):
    local = {"connected": True, "base_url": "http://127.0.0.1:5001/v1", "model": "koboldcpp/Gemma4-12B-Q4",
             "display_name": "Gemma4 12B", "models": ["koboldcpp/Gemma4-12B-Q4", "second-model"],
             "context": {"context_window_tokens": 262144}, "native_tools_mode": "auto", "has_api_key": True,
             "thinking_options": ["off", "minimal", "low", "medium", "high"] if thinking else []}
    page.route("**/api/local_provider", lambda route: route.fulfill(json={"ok": True, "local": local}))
    page.route("**/api/api_models", lambda route: route.fulfill(json={"ok": True, "connections": [api_connection_payload(local)]}))
    settings = model_management_payload(api_connection_payload(local))
    page.route("**/api/model_settings", lambda route: route.fulfill(json=settings))
    page.evaluate("Models.applySettings", settings)
    return local


@pytest.mark.parametrize(('remembered', 'display_name', 'expected'), [
    (True, '', 'Gemma4 12B'), (True, 'My Gemma', 'My Gemma'), (False, '', 'Gemma4 12B'),
])
def test_local_model_title_and_connection_dot_survive_online_and_offline_reload(
    page, monkeypatch, remembered, display_name, expected,
):
    from codey.app import api
    from codey.env_names import LOCAL_OPENAI_API_KEY_ENV, LOCAL_OPENAI_BASE_URL_ENV, LOCAL_OPENAI_MODEL_ENV
    from codey.providers.local_config import LocalProviderConfig

    for key in (LOCAL_OPENAI_API_KEY_ENV, LOCAL_OPENAI_BASE_URL_ENV, LOCAL_OPENAI_MODEL_ENV):
        monkeypatch.delenv(key, raising=False)
    model = 'koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M'
    base_url = 'http://127.0.0.1:5001/v1'
    selection = {'connection_id': 'local', 'base_url': base_url, 'model': model, 'effort': 'low', 'efforts': {model: 'low'}}
    state = {'active_id': 'a', 'sessions': [
        {'id': 'a', 'title': 'Local chat', 'provider': 'local', 'messages': [], 'modelSelection': selection},
    ], 'projects': []}
    saved = LocalProviderConfig(base_url=base_url, model=model, display_name=display_name) if remembered else LocalProviderConfig()
    local = {'connected': True, 'base_url': base_url, 'model': model, 'models': [model]}
    page.route('**/api/model_settings', lambda route: route.fulfill(json=model_management_payload(api_connection_payload({'model': model, 'base_url': base_url}))))
    page.route('**/api/ui_state', lambda route: route.fulfill(json={'ok': True, 'state': state}))
    page.route('**/api/local_provider', lambda route: route.fulfill(json=api.local_provider_response()[1]))
    page.route('**/api/api_models', lambda route: route.fulfill(json={'connections': [api_connection_payload(api.local_provider_response()[1]['local'])]}))
    # Prevent unrelated availability refreshes from claiming the offline model is up.
    page.route('**/api/providers', lambda route: route.fulfill(json={
        'default': 'deepseek', 'providers': [{'id': 'local', 'label': 'Local', 'available': local['connected']}],
    }))
    with (
        mock.patch.object(api, 'load_local_config', return_value=saved),
        mock.patch.object(api, 'local_bootstrap_payload', side_effect=lambda **_: dict(local)),
        mock.patch('codey.providers.local_selection._metadata_json', return_value={}),
    ):
        for connected in (True, False, True):
            local.update(connected=connected, model=model if connected or remembered else '',
                         base_url=base_url if connected or remembered else '', models=[model] if connected else [])
            page.reload()
            expect(page.locator('#provider-button')).to_be_enabled(timeout=15000)
            expect(page.locator('#provider-name')).to_have_text(expected)
            expect(page.locator('#provider-dot')).to_have_class('dot ok' if connected else 'dot ')
            assert page.evaluate("UiState.current().sessions[0].modelSelection") == selection
            expect(page.locator('#effort-chooser')).to_be_hidden()


def test_settings_is_quiet_modal_with_advanced_dark_controls_and_focus_return(page):
    model_settings_route(page)
    open_connection_settings(page)
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_visible()
    expect(page.locator("#local-base-url")).to_have_value("http://127.0.0.1:5001/v1")
    expect(page.locator("#local-native-tools-mode")).to_be_hidden()
    expect(page.locator("#local-config-save")).to_have_text("Save connection")
    page.get_by_text("Advanced", exact=True).click()
    for selector in ("#local-context-preset-button", "#local-native-tools-mode-button"):
        control = page.locator(selector)
        expect(control).to_be_visible()
        assert control.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(28, 28, 28)"
    expect(page.locator("#local-context-window")).to_be_hidden()
    page.locator("#local-context-preset-button").click()
    selected = page.locator('#local-context-preset-menu [aria-selected="true"]')
    assert selected.evaluate("e => getComputedStyle(e).backgroundColor") in {"rgb(47, 47, 47)", "rgb(42, 42, 42)"}
    page.get_by_role("option", name="Custom…", exact=True).click()
    expect(page.locator("#local-context-window")).to_have_value("262144")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_hidden()
    expect(page.locator("#btn-settings")).to_be_focused()
    page.keyboard.press("Control+,")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_be_visible()
    page.set_viewport_size({"width": 640, "height": 480})
    box = page.get_by_role("dialog", name="Settings", exact=True).bounding_box()
    assert box["y"] >= 12 and box["y"] + box["height"] <= 468


def test_actual_model_and_supported_thinking_are_per_chat_and_sent_to_runtime(page):
    local = model_settings_route(page)
    apply_model_metadata(page, local)
    page.locator("#provider-button").click()
    page.locator('.provider-item[data-provider="local"]').first.click()
    expect(page.locator("#provider-name")).to_have_text("Gemma4 12B")
    expect(page.locator("#effort-name")).to_have_text("High")
    for selector in ("#provider-button", "#effort-button", "#provider-menu .provider-item"):
        assert page.locator(selector).evaluate_all("rows => rows.every(row => !row.hasAttribute('title'))")
    page.locator("#effort-button").click()
    assert page.locator('#effort-menu button').evaluate_all("rows => rows.every(row => !row.hasAttribute('title'))")
    expect(page.locator("#provider-menu")).to_be_hidden()
    page.get_by_role("button", name="Thinking effort: Low", exact=True).click()
    expect(page.locator("#effort-name")).to_have_text("Low")
    expect(page.locator("#effort-button")).to_be_focused()
    page.locator("#provider-button").click()
    expect(page.locator("#effort-menu")).to_be_hidden()
    expect(page.locator("#provider-menu .provider-action")).to_have_count(0)
    page.locator('.provider-item[data-model="second-model"]').click()
    expect(page.locator("#effort-button")).to_be_hidden()
    page.locator("#provider-button").click()
    page.locator('.provider-item[data-model="koboldcpp/Gemma4-12B-Q4"]').click()
    expect(page.locator("#effort-name")).to_have_text("Low")
    page.evaluate("switchSession('b')")
    expect(page.locator("#effort-button")).to_be_hidden()
    page.evaluate("switchSession('a')")
    page.locator("#effort-button").click()
    page.evaluate("ProviderUI.applyStatus([{id:'local',available:true}])")
    expect(page.locator('#effort-menu [data-effort="low"]')).to_be_focused()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("#effort-name")).to_have_text("Med")
    page.locator("#effort-button").click()
    page.keyboard.press("Escape")
    expect(page.locator("#effort-button")).to_be_focused()
    requests = []
    page.route("**/api/run", lambda route: (requests.append(route.request.post_data_json), route.fulfill(json={"ok":True,"run_id":"settings-qa"})))
    page.locator("#task").fill("Say hello")
    with page.expect_request("**/api/run"):
        page.locator("#send").click()
    # The request event fires before the Playwright route callback necessarily
    # appends its decoded body. Wait for the composer's request lifecycle to
    # settle before asserting the captured payload.
    page.wait_for_function("Composer.isSending() === false")
    assert requests[0]["model_selection"] == {"connection_id":"local","base_url":local["base_url"],"model":local["model"],"effort":"medium"}


@pytest.mark.parametrize(('old', 'expected'), [('off','Off'),('high','High'),(None,'High')])
def test_saved_effort_selection_remains_per_chat(page, old, expected):
    local = model_settings_route(page)
    apply_model_metadata(page, local)
    page.evaluate("""([local, old]) => {
        const s=sessions.find(s=>s.id==='a');
        s.provider='local'; s.modelSelection={connection_id:'local',base_url:local.base_url,model:local.model,effort:old,efforts:{[local.model]:old}};
        setActiveProvider('local');
        ProviderUI.sync('local');
    }""", [local,old])
    expect(page.locator("#effort-name")).to_have_text(expected)


@pytest.mark.parametrize('size', [(1280,800),(720,560),(480,420)])
def test_supported_effort_controls_and_menu_fit_short_windows(page, size):
    local = model_settings_route(page)
    apply_model_metadata(page, local)
    page.locator('#provider-button').click()
    page.locator('.provider-item[data-provider="local"]').first.click()
    page.set_viewport_size({'width':size[0], 'height':size[1]})
    page.locator('#task').fill('Long input\n' * 25)
    for selector in ['#provider-button','#effort-button','#send']:
        box = page.locator(selector).bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= size[0]
        assert box['y'] >= 0 and box['y'] + box['height'] <= size[1]
    page.locator('#provider-button').click()
    model_menu = page.locator('#provider-menu').bounding_box()
    assert model_menu['width'] == 240
    assert model_menu['x'] >= 0 and model_menu['x'] + model_menu['width'] <= size[0]
    page.locator('#effort-button').click()
    menu = page.locator('#effort-menu').bounding_box()
    assert menu['x'] >= 0 and menu['x'] + menu['width'] <= size[0]
    assert menu['y'] >= 0 and menu['y'] + menu['height'] <= size[1]
    page.keyboard.press('Home')
    page.keyboard.press('Enter')
    expect(page.locator('#effort-name')).to_have_text('Off')
    # Effort/name changes must not change the menu width; resizing keeps it on-screen.
    for effort in ('minimal', 'medium'):
        page.locator('#effort-button').click()
        page.locator(f'#effort-menu [data-effort="{effort}"]').click()
        page.locator('#provider-button').click()
        page.wait_for_function("""() => {
            const menu = document.getElementById('provider-menu').getBoundingClientRect();
            return menu.width === 240 && menu.left >= 0 && menu.right <= innerWidth;
        }""")
        page.keyboard.press('Escape')
    local['display_name'] = 'A much longer model name'
    apply_model_metadata(page, local)
    page.locator('#provider-button').click()
    page.set_viewport_size({'width':size[0] - 40, 'height':size[1]})
    page.wait_for_function("""() => {
        const menu = document.getElementById('provider-menu').getBoundingClientRect();
        return menu.width === 240 && menu.left >= 0 && menu.right <= innerWidth;
    }""")


def test_unknown_or_web_model_has_no_fake_thinking_control(page):
    local = model_settings_route(page, thinking=False)
    apply_model_metadata(page, local)
    page.locator("#provider-button").click()
    expect(page.locator("#effort-button")).to_be_hidden()
    page.locator('.provider-item[data-provider="local"]').first.click()
    page.locator("#provider-button").click()
    expect(page.locator("#effort-button")).to_be_hidden()
    expect(page.locator("#effort-separator")).to_have_count(0)
    expect(page.locator("#provider-menu input")).to_have_count(0)


def test_chat_drafts_and_caret_are_isolated(page):
    task = page.locator("#task")
    task.fill("alpha draft")
    page.evaluate("document.getElementById('task').setSelectionRange(2, 5)")
    page.evaluate("switchSession('b')")
    expect(task).to_have_value("")
    task.fill("beta draft")
    page.evaluate("switchSession('a')")
    expect(task).to_have_value("alpha draft")
    assert page.evaluate("[document.getElementById('task').selectionStart, document.getElementById('task').selectionEnd]") == [2, 5]
    page.evaluate("switchSession('b')")
    expect(task).to_have_value("beta draft")


def test_send_failure_keeps_draft_and_blocks_double_submission(page):
    page.evaluate("""() => {
        window.runCalls = 0;
        const realFetch = fetch;
        window.fetch = (url, options) => url === '/api/run'
            ? (runCalls++, new Promise(resolve => window.finishSend = () => resolve(new Response('{}', {status:409}))))
            : realFetch(url, options);
    }""")
    page.locator("#task").fill("keep this task")
    page.locator("#send").click()
    # Admission owns the submitted text immediately; a rejected response
    # restores it after the request finishes.
    expect(page.locator("#task")).to_have_value("")
    expect(page.locator("#send")).to_be_disabled()
    page.locator("#task").press("Enter")
    assert page.evaluate("runCalls") == 1
    page.evaluate("finishSend()")
    expect(page.locator("#send")).to_be_enabled()
    expect(page.locator("#task")).to_have_value("keep this task")
    expect(page.locator(".status-row.err")).to_contain_text("Could not send")


def test_preflight_rejection_restores_draft_after_admission_clear(page):
    page.locator("#task").fill("restore after a changed model")
    page.evaluate("Models.allows = () => false")
    page.locator("#send").click()
    expect(page.locator("#task")).to_have_value("restore after a changed model")


def test_accepted_snapshot_does_not_erase_later_input_or_other_chat(page):
    page.evaluate("""() => {
        const realFetch = fetch;
        window.fetch = (url, options) => url === '/api/run'
            ? new Promise(resolve => window.finishSend = () => resolve(new Response('{"run_id":"run-a"}')))
            : realFetch(url, options);
    }""")
    page.locator("#task").fill("first task")
    page.locator("#send").click()
    page.locator("#task").fill("later edit")
    page.evaluate("switchSession('b')")
    page.locator("#task").fill("beta task")
    page.evaluate("finishSend()")
    page.wait_for_function("sessions.find(s => s.id === 'a').messages.some(m => m.type === 'user')")
    expect(page.locator("#task")).to_have_value("beta task")
    page.evaluate("switchSession('a')")
    expect(page.locator("#task")).to_have_value("later edit")


def test_choose_folder_never_sends_draft(page):
    calls = []
    page.on("request", lambda request: calls.append(request.url) if request.url.endswith("/api/run") else None)
    page.locator("#task").fill("do not run yet")
    page.locator("#ctx-folder").click()
    expect(page.locator("#ctx-folder")).to_have_text("Project Alpha")
    expect(page.locator("#task")).to_have_value("do not run yet")
    assert calls == []


def test_success_consumes_only_submitted_chat_draft(page):
    page.route("**/api/run", lambda route: route.fulfill(json={"run_id": "accepted"}))
    page.locator("#task").fill("accepted task")
    page.locator("#send").click()
    expect(page.locator("#task")).to_have_value("")
    page.evaluate("switchSession('b')")
    page.locator("#task").fill("independent")
    page.evaluate("switchSession('a')")
    expect(page.locator("#task")).to_have_value("")


def test_retry_preserves_new_draft_and_uses_original_failed_submission(page):
    submissions = []

    def run(route):
        submissions.append(route.request.post_data_json)
        route.fulfill(status=409, json={"error": "busy"})

    page.route("**/api/run", run)
    page.locator("#task").fill("original task")
    page.locator("#send").click()
    expect(page.locator(".status-row.err")).to_be_visible()
    page.locator("#task").fill("later draft")
    page.get_by_role("button", name="Retry", exact=True).click()
    expect(page.locator("#send")).to_be_enabled()
    assert len(submissions) == 2
    assert submissions[1]["task"] == "original task"
    expect(page.locator("#task")).to_have_value("later draft")


def test_model_menu_has_no_search_and_small_catalog_keyboard(page):
    page.locator("#provider-button").click()
    expect(page.locator("#provider-menu input")).to_have_count(0)
    expect(page.locator(".provider-item:visible")).to_have_count(5)
    page.keyboard.press("Enter")
    expect(page.locator("#provider-name")).to_have_text("DeepSeek")
    catalog = {"providers": [{"id": "deepseek", "label": "DeepSeek"}, {"id": "mimo", "label": "MiMo"}], "default": "mimo"}
    for endpoint in ('providers', 'provider_catalog'):
        page.route('**/api/' + endpoint, lambda route: route.fulfill(json=catalog))
    page.evaluate("ProviderUI.applyConfig", catalog)
    page.locator("#provider-button").click()
    expect(page.locator("#provider-menu input")).to_have_count(0)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("#provider-name")).to_have_text("MiMo")
    assert page.evaluate("UiState.DEFAULT_PROVIDER") == "mimo"


def test_late_changes_response_cannot_replace_new_project_or_reopen_drawer(page):
    page.evaluate("""() => {
        const realFetch = fetch;
        window.fetch = (url, options) => {
            if (url !== '/api/changes') return realFetch(url, options);
            const project = JSON.parse(options.body).project;
            if (project === 'E:/old') return new Promise(resolve => window.finishChanges = () => resolve(new Response(JSON.stringify({ok:true,mode:'git',files:[{path:'old.py'}],diff:''}))));
            return Promise.resolve(new Response(JSON.stringify({ok:true,mode:'git',files:[{path:'new.py'}],diff:''})));
        };
        openChangesDrawer('E:/old');
    }""")
    page.evaluate("openChangesDrawer('E:/new')")
    expect(page.locator("#changes-body")).to_contain_text("new.py")
    page.evaluate("finishChanges()")
    expect(page.locator("#changes-body")).not_to_contain_text("old.py")
    page.evaluate("switchSession('b')")
    expect(page.locator("#changes-drawer")).to_have_attribute("aria-hidden", "true")


def test_change_file_link_expands_exact_file_and_escape_returns_focus(page):
    page.route("**/api/changes", lambda route: route.fulfill(json={"ok": True, "mode": "snapshot", "files": [
        {"path": "one.py"}, {"path": "two.py"}], "diff": "diff --git a/two.py b/two.py\n@@ -1 +1 @@\n-old\n+new"}))
    page.evaluate("addToSession('a',{type:'changes',project:'E:/project-alpha',files:[{path:'two.py'}]})")
    link = page.get_by_role("button", name="two.py", exact=True)
    link.click()
    expect(page.locator('.change-file[data-path="two.py"] pre')).to_be_visible()
    expect(page.locator('.change-file[data-path="one.py"] pre')).to_be_hidden()
    page.keyboard.press("Escape")
    expect(link).to_be_focused()


def test_hybrid_task_done_research_payload_creates_graph_entry(page):
    page.route("**/api/research/graph**", lambda route: route.fulfill(json={
        "ok": True,
        "graph": {"nodes": [{"id": "note-1", "label": "Research note", "kind": "note"}], "edges": []},
    }))
    page.evaluate("""() => handleServerEvent({
        type: 'task_done', run_id: 'hybrid-run', session_id: 'a', summary: 'done',
        stop_reason: 'done', turns: 1, mode: 'hybrid', receipt: {display: {summary: 'done'}},
        research: {notes_created: ['note-1'], notes_updated: [], source_urls: [], sources_read: 0,
          queries: ['graph'], search_results: [], opened_sources: [], coverage: {},
          evidence_items: [], citation_map: [], counterpoints: [], quality_warnings: []}
    })""")
    page.evaluate("ResearchDrawer.open('a', 'hybrid-run')")
    expect(page.locator("#research-drawer")).to_be_visible()
    expect(page.get_by_role("button", name="Graph", exact=True)).to_be_visible()
    page.get_by_role("button", name="Graph", exact=True).click()
    expect(page.locator("#research-drawer .research-graph-stage canvas")).to_be_visible()


def test_invalid_terminal_event_does_not_consume_run_and_valid_replay_finishes_once(page):
    result = page.evaluate("""() => {
        const invalid = {
            type: 'task_done', run_id: 'replay-run', session_id: 'a',
            summary: 'FINAL REPLY', stop_reason: 'done', mode: 'chat',
            research: {notes_created: ['note-1', 42]},
        };
        let error = '';
        try { handleServerEvent(invalid); } catch (exc) { error = String(exc.message || exc); }
        const valid = {...invalid, research: {notes_created: []}};
        handleServerEvent(valid);
        handleServerEvent(valid);
        return {error};
    }""")
    assert 'research notes_created is invalid' in result['error']
    expect(page.locator('.msg.asst').filter(has_text='FINAL REPLY')).to_have_count(1)


def test_truncated_changes_loads_large_file_preview_on_demand(page):
    page.route("**/api/changes**", lambda route: route.fulfill(json={
        "ok": True, "mode": "git", "truncated": True,
        "files": [{"path": "bundle.js", "status": "??"}], "diff": "",
    }))
    page.route("**/api/changes/file", lambda route: route.fulfill(json={
        "ok": True, "path": "bundle.js", "status": "too_large", "size": 184324,
    }))
    page.evaluate("openChangesDrawer('E:/project-alpha')")
    expect(page.locator("#changes-subtitle")).to_contain_text("Diff list truncated")
    expect(page.locator(".change-file[data-path='bundle.js']")).to_have_count(1)
    with page.expect_request("**/api/changes/file"):
        page.locator(".change-file[data-path='bundle.js'] button").click()
    expect(page.locator(".change-file[data-path='bundle.js'] pre")).to_contain_text("too large")


def test_refresh_reloads_complete_preview_for_expanded_file_and_focus_keeps_it_open(page):
    file_calls = []
    preview_texts = iter(['complete-v1', 'complete-v2', 'complete-v3'])
    page.route("**/api/changes**", lambda route: route.fulfill(json={
        "ok": True, "mode": "git", "truncated": True,
        "files": [{"path": "bundle.js", "status": "M"}],
        "diff": "diff --git a/bundle.js b/bundle.js\n@@ -1 +1 @@\n-old\n+partial\n",
    }))

    def file_preview(route):
        file_calls.append(route.request.url)
        text = next(preview_texts)
        route.fulfill(json={
            "ok": True, "path": "bundle.js", "status": "text",
            "diff": f"diff --git a/bundle.js b/bundle.js\n@@ -1 +1 @@\n-old\n+{text}\n",
        })

    page.route("**/api/changes/file", file_preview)
    page.evaluate("() => openChangesDrawer('E:/project-alpha')")
    expect(page.locator(".change-file[data-path='bundle.js']")).to_have_count(1)
    page.locator(".change-file[data-path='bundle.js'] button").click()
    expect(page.locator(".change-file[data-path='bundle.js'] pre")).to_contain_text("complete-v1")
    assert len(file_calls) == 1
    page.wait_for_function("!document.querySelector('.change-file[data-path=\\\"bundle.js\\\"]').dataset.previewLoading")
    expect(page.locator('#changes-refresh')).to_be_enabled()
    with page.expect_request("**/api/changes/file"):
        page.locator('#changes-refresh').click()
    expect(page.locator(".change-file[data-path='bundle.js'] pre")).to_contain_text("complete-v2")
    assert len(file_calls) == 2
    page.evaluate("() => openChangesDrawer('E:/project-alpha', 'bundle.js')")
    expect(page.locator(".change-file[data-path='bundle.js'] pre")).to_be_visible()


def test_late_restore_failure_stays_with_original_project(page):
    page.route("**/api/changes", lambda route: route.fulfill(json={"ok": True, "mode": "snapshot", "files": [{"path": "one.py"}], "diff": ""}))
    page.evaluate("""() => {
        const realFetch = fetch;
        window.fetch = (url, options) => url === '/api/changes/restore'
            ? new Promise(resolve => window.finishRestore = () => resolve(new Response('{"ok":false,"error":"OLD RESTORE ERROR"}')))
            : realFetch(url, options);
        openChangesDrawer('E:/old');
    }""")
    expect(page.locator("#changes-restore")).to_be_enabled()
    page.locator("#changes-restore").click()
    page.evaluate("openChangesDrawer('E:/new')")
    expect(page.locator("#changes-scope")).to_have_text("new")
    page.evaluate("finishRestore()")
    page.wait_for_function("document.getElementById('changes-restore').textContent !== 'Restoring'")
    expect(page.locator("#changes-subtitle")).not_to_contain_text("OLD RESTORE ERROR")


def test_narrow_window_controls_can_be_clicked_and_keyboard_sidebar_menu_closes(page):
    page.set_viewport_size({"width": 480, "height": 420})
    page.locator("#provider-button").click()
    expect(page.locator('.provider-item[data-provider="deepseek"]')).to_be_focused()
    page.keyboard.press("Escape")
    page.locator("#show-sidebar").click()
    title = page.locator(".session-title").first
    title.focus()
    page.keyboard.press("Tab")
    expect(page.locator(".session-more").first).to_be_focused()
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Escape")
    expect(page.locator(".session-more").first).to_be_focused()


def test_full_refresh_preserves_selection_and_full_answer(page):
    page.evaluate("""() => {
        addToSession('a', {type:'asst',text:'Full answer\\n'.repeat(30)});
        const range = document.createRange(); range.selectNodeContents(document.querySelector('.msg.asst .body'));
        getSelection().removeAllRanges(); getSelection().addRange(range);
        window.selectedAnswer = getSelection().toString();
        renderChat();
    }""")
    assert page.evaluate("getSelection().toString() === selectedAnswer")
    page.evaluate("switchSession('b'); switchSession('a')")
    expect(page.locator(".msg.asst .body")).to_contain_text("Full answer")
    expect(page.get_by_role("button", name="Expand", exact=True)).to_have_count(0)


def test_model_menu_keyboard_and_focus_return(page):
    trigger = page.locator("#provider-button")
    trigger.click()
    expect(trigger).to_have_attribute("aria-expanded", "true")
    expect(page.locator('.provider-item[data-provider="deepseek"]')).to_be_focused()
    for _ in range(3):
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(page.locator("#provider-name")).to_have_text("Qwen")
    expect(trigger).to_be_focused()
    trigger.click()
    page.keyboard.press("Escape")
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(trigger).to_be_focused()


def test_model_menu_width_does_not_follow_selected_name(page):
    connection = {"id": "zen", "label": "OpenCode Zen", "models": [
        {"id": "pickle", "name": "Big Pickle", "efforts": []},
        {"id": "spark", "name": "Muse Spark 1.3 Free", "efforts": ["low", "high"]},
    ]}
    select_connection_models(page, connection)
    widths = []
    for provider, model in [("mimo", ""), ("zen", "pickle"), ("zen", "spark")]:
        page.locator('#provider-button').click()
        selector = f'.provider-item[data-provider="{provider}"]'
        if model:
            selector += f'[data-model="{model}"]'
        page.locator(selector).click()
        page.locator('#provider-button').click()
        menu = page.locator('#provider-menu')
        bounds = menu.bounding_box()
        widths.append(bounds['width'])
        check = menu.locator('.provider-item.active .check').bounding_box()
        assert bounds['x'] + bounds['width'] - check['x'] - check['width'] >= 10
        assert menu.evaluate('el => el.scrollWidth <= el.clientWidth')
        page.keyboard.press('Escape')
    assert max(widths) - min(widths) <= 1
    assert min(widths) >= 220


def test_running_owner_and_approval_are_discoverable_without_enter_approving(page):
    approvals = []
    page.on("request", lambda request: approvals.append(request.url) if "/api/shell_approval" in request.url else None)
    page.evaluate("""() => {
        applyRunState({busy:true,run_id:'run-a',session_id:'a'});
        addToSession('a', {type:'shell_request',id:'approval-a',command:'pytest -q',cwd:'E:/project-alpha'});
        switchSession('b');
    }""")
    expect(page.locator("#composer-notice")).to_have_count(0)
    expect(page.locator("#status")).to_contain_text("Alpha")
    expect(page.locator("#stop")).to_have_attribute("aria-label", "Stop Alpha")
    page.locator("#task").fill("another draft")
    page.locator("#task").press("Enter")
    assert approvals == []
    page.locator('#status').get_by_role("button", name="Open", exact=True).click()
    expect(page.locator("#sess-title")).to_contain_text("Alpha")
    expect(page.locator(".shell-command")).to_contain_text("pytest -q")
    expect(page.get_by_text("Run this command?", exact=True)).to_have_count(1)


def test_new_chat_keeps_composer_usable_while_another_session_runs(page):
    page.evaluate("applyRunState({busy:true,run_id:'run-a',session_id:'a',run_status:'running'})")
    page.evaluate("switchSession('b')")
    page.locator('#task').fill('independent topic')
    expect(page.locator('#send')).to_be_enabled()
    expect(page.locator('#provider-button')).to_be_enabled()
    expect(page.locator('#stop')).to_be_hidden()


def test_reading_position_and_back_to_latest(page):
    page.evaluate("""() => {
        for (let i=0; i<40; i++) addToSession('a',{type:'asst',text:'Reading paragraph '+i});
        document.getElementById('chat-area').dispatchEvent(new WheelEvent('wheel',{deltaY:-200}));
        document.getElementById('chat-area').scrollTop = 200;
        document.getElementById('chat-area').dispatchEvent(new Event('scroll'));
        switchSession('b'); switchSession('a');
    }""")
    assert abs(page.locator("#chat-area").evaluate("e => e.scrollTop") - 200) < 4
    page.evaluate("addToSession('a', {type:'asst',text:'New output'})")
    expect(page.get_by_role("button", name="Back to latest", exact=True)).to_be_visible()
    page.get_by_role("button", name="Back to latest", exact=True).click()
    assert page.locator("#chat-area").evaluate("e => e.scrollHeight - e.scrollTop - e.clientHeight") < 4


def test_search_and_inspection_scope_close_on_chat_switch(page):
    trigger = page.get_by_role("button", name="Search chats and projects", exact=True)
    search = page.get_by_role("searchbox", name="Search chats and projects")
    expect(search).to_be_hidden()


    before = trigger.bounding_box()
    new_chat = page.locator("#btn-new-chat").bounding_box()
    add_project = page.locator("#btn-add-project").bounding_box()
    first_gap = add_project["y"] - new_chat["y"] - new_chat["height"]
    second_gap = before["y"] - add_project["y"] - add_project["height"]
    assert abs(first_gap - second_gap) < 1
    trigger.click()
    expect(search).to_be_focused()
    after = search.bounding_box()
    assert abs(before["height"] - after["height"]) < 1
    assert abs(before["y"] - after["y"]) < 1
    search.fill("beta")
    expect(page.locator(".session-title")).to_have_text("Beta")
    search.press("Escape")
    expect(search).to_be_hidden()
    expect(trigger).to_be_focused()
    expect(page.locator(".session-title")).to_have_count(2)
    page.route("**/api/changes", lambda route: route.fulfill(json={"ok": True, "mode": "git", "files": [
        {"path": "module.py", "status": "M", "additions": 1}],
        "diff": "diff --git a/module.py b/module.py\n@@ -1 +1 @@\n-old\n+new"}))
    page.evaluate("openChangesDrawer('E:/project-alpha')")
    expect(page.locator("#changes-subtitle")).to_contain_text("Working tree")
    expect(page.locator("#changes-drawer")).to_contain_text("project-alpha")
    page.evaluate("switchSession('b')")
    expect(page.locator("#changes-drawer")).to_have_attribute("aria-hidden", "true")
    assert page.locator("#changes-drawer").evaluate("e => e.inert")


def test_new_chat_can_start_while_another_session_is_running(page):
    """A global execution slot must not disable a new session's admission."""
    started = []
    page.route("**/api/run", lambda route: (started.append(route.request.post_data_json), route.fulfill(json={"ok": True, "run_id": "run-b"})))
    page.evaluate(
        "applyRunState",
        {"busy": True, "run_id": "run_old", "session_id": "a", "run_status": "running"},
    )
    page.locator("#btn-new-chat").click()
    expect(page.locator("#task")).to_be_enabled()
    page.locator("#task").fill("new chat request")
    expect(page.locator("#send")).to_be_enabled()
    page.locator("#send").click()
    expect(page.locator(".pending-inputs")).to_be_hidden()
    assert len(started) == 1
    assert started[0]["session_id"] != "a"


def test_three_chats_can_admit_inputs_while_first_chat_is_running(page):
    """Two background chats can start independently while chat A runs."""
    started = []
    page.route("**/api/run", lambda route: (started.append(route.request.post_data_json), route.fulfill(json={"ok": True, "run_id": "run-new"})))
    page.evaluate("applyRunState", {"busy": True, "run_id": "run_a", "session_id": "a", "run_status": "running"})

    page.evaluate("switchSession('b')")
    page.locator("#task").fill("topic B")
    page.locator("#send").click()
    expect(page.locator(".pending-inputs")).to_be_hidden()

    page.locator("#btn-new-chat").click()
    page.locator("#task").fill("topic C")
    page.locator("#send").click()
    expect(page.locator(".pending-inputs")).to_be_hidden()
    assert {item["session_id"] for item in started} == {"b", page.evaluate("activeId")}


def test_stale_frontend_run_state_recovers_busy_response_into_new_run(page):
    """A disconnected SSE state must not turn a valid new-chat send into an error."""
    started = []
    page.route(
        "**/api/state",
        lambda route: route.fulfill(json={"busy": True, "run_id": "run_a", "session_id": "a", "run_status": "running"}),
    )

    page.route("**/api/run", lambda route: (started.append(route.request.post_data_json), route.fulfill(json={"ok": True, "run_id": "run-b"})))
    page.evaluate("switchSession('b'); runningSessionId=null; runningRunId=null")
    page.locator("#task").fill("stale state topic")
    page.locator("#send").click()
    page.wait_for_timeout(250)
    expect(page.locator(".pending-inputs")).to_be_hidden()
    assert started and started[0]["session_id"] == "b"


@pytest.mark.parametrize("size", [(1280, 800), (720, 560), (480, 420)])
def test_composer_controls_share_frame_and_remain_reachable(page, size):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.locator("#task").fill("long input\n" * 25)
    box = page.locator(".composer-box").bounding_box()
    for control in [page.locator("#provider-button"), page.locator("#send")]:
        rect = control.bounding_box()
        assert rect["y"] >= box["y"] and rect["y"] + rect["height"] <= box["y"] + box["height"] + 1
        assert rect["x"] >= 0 and rect["x"] + rect["width"] <= size[0]
        assert rect["y"] + rect["height"] <= size[1]
    assert page.locator("#send").bounding_box()["width"] >= 32
    hint = page.locator("#send-hint").bounding_box()
    send = page.locator("#send").bounding_box()
    assert 0 <= send["x"] - hint["x"] - hint["width"] <= 12
