"""Exercise the app's HTML preview using the shipped bundle and real document reads."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, Route, expect

from codey.app.api import run_changes_response
from codey.app.context import AppContext
from codey.app.documents import document_response
from codey.app.server import CodeyHTTPServer
from codey.runs.task_diff import record_task_changes
from codey.utils.object_fields import object_fields
from codey.workspace.changes import ChangeTracker
from codey.workspace.session import session_workspace
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser

HTML = """<!doctype html>
<button id="start">Start</button>
<canvas id="board" width="150" height="90"></canvas>
<output id="score"></output>
<output id="isolation"></output>
<button id="navigate">Leave page</button>
<script>
let score = Number(localStorage.getItem('score') ?? 0);
document.querySelector('#score').textContent = score;
let playing = false;
document.querySelector('#start').onclick = () => { playing = true; };
document.addEventListener('keydown', (event) => {
  if (!playing || event.key !== 'ArrowRight') return;
  event.preventDefault();
  localStorage.setItem('score', ++score);
  document.querySelector('#score').textContent = localStorage.getItem('score');
});
try {
  parent.document.title = 'changed';
} catch (error) {
  document.querySelector('#isolation').textContent = error.name;
}
fetch('/api/html-preview-probe').catch(() => {});
document.querySelector('#navigate').onclick = () => { location.href = '/api/html-preview-probe'; };
</script>
"""


def _route_documents(page: Page, context: AppContext) -> None:
    def read_document(route: Route) -> None:
        data: object = route.request.post_data_json
        assert object_fields(data)
        response: tuple[int, dict[str, object]] = document_response(context, data)
        route.fulfill(status=response[0], json=response[1])

    page.route("**/api/documents/read", read_document)


def test_finished_html_opens_and_runs_in_isolated_workspace_preview(page: Page) -> None:
    context = CodeyHTTPServer.app_context
    assert context is not None and context.state_home is not None
    run = "html-preview-run"
    context.durable.admit_input(
        conversation_id="a", request_id=run, task_id=run,
        text="Build a game in HTML", provider_snapshot={"provider_id": "deepseek"},
    )
    root = session_workspace(context.state_home, "a")
    (root / "game.html").write_text(HTML, encoding="utf-8")

    probes: list[str] = []

    def record_probe(route: Route) -> None:
        probes.append(route.request.url)
        route.fulfill(status=200, body="external request")

    _route_documents(page, context)
    page.route("**/api/html-preview-probe", record_probe)
    page.evaluate("localStorage.setItem('score', '42')")
    page.evaluate("id => handleServerEvent({type:'task_start',run_id:id,session_id:'a'})", run)
    page.wait_for_function("id => ConversationUI.acceptsRun('a', id)", arg=run)
    page.evaluate("""id => handleServerEvent({
        type:'task_done', run_id:id, session_id:'a', mode:'chat', stop_reason:'done',
        changed:true, changes:{changed_count:1, files:[{path:'game.html', status:'A'}]}
    })""", run)

    workspace = page.locator("#right-workspace")
    expect(workspace).to_be_visible()
    frame = workspace.frame_locator('iframe[title="game.html"]')
    expect(frame.locator("#score")).to_have_text("0")
    expect(frame.locator("#isolation")).to_have_text("SecurityError")
    frame.get_by_role("button", name="Start").click()
    page.keyboard.press("ArrowRight")
    expect(frame.locator("#score")).to_have_text("1")
    assert page.evaluate("localStorage.getItem('score')") == "42"

    with page.expect_console_message(predicate=lambda item: "frame-src 'none'" in item.text):
        frame.get_by_role("button", name="Leave page").click()
    assert not probes
    workspace.get_by_role("button", name="View source").click()
    expect(workspace.locator("iframe")).to_have_count(0)
    expect(workspace.locator("pre")).to_contain_text("let playing = false;")
    workspace.get_by_role("button", name="View preview").click()
    expect(frame.get_by_role("button", name="Start")).to_be_visible()


def test_changes_offer_preview_for_existing_html_files(page: Page) -> None:
    context = CodeyHTTPServer.app_context
    assert context is not None and context.state_home is not None
    root = session_workspace(context.state_home, "a")
    (root / "design.htm").write_text("<h1>Page design</h1>", encoding="utf-8")
    _route_documents(page, context)

    def changes(route: Route) -> None:
        route.fulfill(json={
            "ok": True, "mode": "git", "diff": "",
            "files": [
                {"path": "design.htm", "status": "M"},
                {"path": "deleted.html", "status": "D"},
            ],
        })

    page.route("**/api/changes", changes)
    page.evaluate("project => openChangesDrawer(project)", str(root))
    expect(page.get_by_role("button", name="Preview current workspace · deleted.html")).to_have_count(0)
    page.get_by_role("button", name="Preview current workspace · design.htm").click()
    view = page.locator("#right-workspace").frame_locator('iframe[title="design.htm"]')
    expect(view.get_by_role("heading", name="Page design")).to_be_visible()


def test_historical_preview_keeps_settled_html_and_blocks_navigation(page: Page) -> None:
    context = CodeyHTTPServer.app_context
    assert context is not None and context.state_home is not None and context.run_ledgers is not None
    root = session_workspace(context.state_home, "a")
    path = root / "settled.html"
    tracker = ChangeTracker(root)
    tracker.capture_before(path.name)
    path.write_text(
        '<!doctype html><head><meta http-equiv="Refresh" content="0;url=/preview-live"></head>'
        '<body><h1>Settled value</h1><a href="/preview-live">Live link</a>'
        '<script>document.querySelector("h1").textContent = "Changed by script"; location.href = "/preview-live";</script></body>',
        encoding="utf-8",
    )
    tracker.capture_after(path.name)
    run = "historical-preview"
    writer = context.run_ledgers.open(run_id=run, session_id="a", project=root, task=run, provider="local", mode="project")
    record_task_changes(writer, context.managed_outputs, tracker.collect_produced(), checks_passed=False, receipt={})
    path.write_text("<h1>Live value</h1>", encoding="utf-8")
    probes: list[str] = []

    def live_page(route: Route) -> None:
        probes.append(route.request.url)
        route.fulfill(body="<h1>Live value</h1>", content_type="text/html")

    def run_changes(route: Route) -> None:
        status, payload = run_changes_response(context, parse_qs(urlsplit(route.request.url).query))
        route.fulfill(status=status, json=payload)

    page.route("**/api/run_changes?*", run_changes)
    page.route("**/preview-live", live_page)
    page.evaluate("args => ChangesDrawer.openRun(...args)", [str(root), "a", run])
    page.get_by_role("button", name="Run preview · settled.html").click()
    frame = page.frame_locator('iframe[title="Run preview · settled.html"]')
    expect(frame.get_by_role("heading")).to_have_text("Settled value")
    frame.get_by_text("Live link").click()
    expect(frame.get_by_role("heading")).to_have_text("Settled value")
    assert not probes
