"""A successful connection must publish running for SSE replay as well as state."""

from types import SimpleNamespace

import pytest

from codey.app import provider_services
from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessAppContext
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_connection_event_stream_agrees_with_authoritative_state(tmp_path, monkeypatch, headless, failed):
    def connect(*args, **kwargs):
        if failed:
            raise OSError("connection failed")
        return SimpleNamespace(close=lambda: None)

    if headless:
        context = HeadlessAppContext(tmp_path, port=0, emit_jsonl=lambda row: None, connect_provider=connect)
    else:
        context = AppContext(tmp_path)
        monkeypatch.setattr(provider_services, "connect_provider", connect)
        monkeypatch.setattr(provider_services, "provider_status_update", lambda *args: [])
    events = context.subscribe()
    try:
        if failed:
            with pytest.raises(OSError, match="connection failed"):
                context.get_provider("deepseek")
        else:
            context.get_provider("deepseek")
        statuses = []
        while not events.empty():
            event = events.get_nowait()
            if event["type"] == "status":
                statuses.append(event["status"])
        assert statuses == (["connecting"] if failed else ["connecting", "running"])
        assert context.run_status() == statuses[-1]
    finally:
        context.close()


@pytest.mark.parametrize("status,text,kind", [("connecting", "Connecting", "warn"),
                                               ("running", "Running", "run")])
@pytest.mark.parametrize("scope", ["unlinked", "current", "previous", "settled"])
def test_replayed_connection_status_keeps_running_display(page, status, text, kind, scope):
    page.evaluate("""scope => {
      handleServerEvent({type:'task_start',session_id:'a',run_id:'run',task_revision:1,task_status:'running'});
      if (scope !== 'unlinked') activeSession().messages = [{type:'user', id:'request', attempts:[
        {runId:'previous-run',state:'failed'},
        {runId:'run',state:scope === 'settled' ? 'done' : 'running'}]}];
    }""", scope)
    before = page.locator('#status').text_content()
    page.evaluate("event => handleServerEvent(event)", {'type':'status', 'session_id':'a',
                  'run_id':'previous-run' if scope == 'previous' else 'run', 'status':status})
    if scope in {'unlinked', 'current'}:
        assert page.locator('#status').text_content() == text
        assert kind in page.locator('#status').get_attribute('class').split()
    else:
        assert page.locator('#status').text_content() == before
