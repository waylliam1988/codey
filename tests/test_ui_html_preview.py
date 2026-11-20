"""Exercise the app's HTML preview using the shipped bundle and real document reads."""
from __future__ import annotations

from playwright.sync_api import Page, Route, expect

from codey.app.context import AppContext
from codey.app.documents import document_response
from codey.app.server import CodeyHTTPServer
from codey.utils.object_fields import object_fields
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
    expect(page.get_by_role("button", name="Preview deleted.html")).to_have_count(0)
    page.get_by_role("button", name="Preview design.htm").click()
    view = page.locator("#right-workspace").frame_locator('iframe[title="design.htm"]')
    expect(view.get_by_role("heading", name="Page design")).to_be_visible()
