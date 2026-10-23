import json
import sys
import threading
from types import SimpleNamespace

import pytest

from codey.app import desktop, desktop_startup
from codey.control.protocol import ControlError
from codey.launcher.startup import StartupTrace, startup_stage


@pytest.fixture(autouse=True)
def deadlines(monkeypatch):
    timers = []
    class Deadline:
        def __init__(self, interval, function, args=()):
            self.interval, self.function, self.args = interval, function, args
            self.cancelled = False
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            self.cancelled = True
        def fire(self):
            self.function(*self.args)
    monkeypatch.setattr(desktop_startup.threading, 'Timer', Deadline)
    return timers


class Window:
    def __init__(self):
        self.scripts, self.urls, self.evaluations = [], [], []
        self.navigated = threading.Event()
    def evaluate_js(self, script):
        self.evaluations.append(script)
        raise AssertionError("Native JS must not be on the startup path")
    def load_url(self, url):
        self.urls.append(url)
        self.navigated.set()
    def load_html(self, content):
        self.document = content
        self.scripts.append(json.loads(content.rsplit("window.codeyStartup(", 1)[1].split(");</script>", 1)[0]))


def controller(tmp_path, *, shown=True):
    window = Window()
    startup = desktop_startup.DesktopStartup(tmp_path, 0, StartupTrace(tmp_path))
    startup.bind(window)
    if shown:
        startup.shown()
    return startup, window


def client():
    return SimpleNamespace(lifecycle=lambda operation: {"url": "http://127.0.0.1:5173/"})


def test_success_navigates_the_same_window_and_records_actual_interface_ready(tmp_path, monkeypatch):
    startup, window = controller(tmp_path)
    monkeypatch.setattr(desktop_startup, "ensure_service", lambda *args, **kwargs: client())
    startup.shown()
    startup.start()
    startup.ui_ready(startup._trace.identity)
    startup.ui_ready(startup._trace.identity)
    assert len(window.urls) == 1 and window.urls[0].startswith("http://127.0.0.1:5173/#codey_startup=")
    rows = [json.loads(row) for row in (tmp_path / "startup-timings.jsonl").read_text().splitlines()]
    assert sum(row["stage"] == "desktop.interface_ready" for row in rows) == 1
    assert all(row["elapsed_seconds"] >= 0 and row["since_start_seconds"] >= 0 for row in rows)
    assert all("http" not in json.dumps(row) for row in rows)


def test_interface_deadline_recovers_missing_scripts_and_cannot_fail_a_later_attempt(tmp_path, monkeypatch, deadlines):
    startup, window = controller(tmp_path)
    monkeypatch.setattr(desktop_startup, 'ensure_service', lambda *args, **kwargs: client())
    startup.start()
    old = deadlines[-1]
    old.fire()
    assert 'did not become ready' in window.scripts[-1]['error']
    assert old.cancelled and startup._failed
    assert startup._begin(retry=True)
    startup._run()
    current = deadlines[-1]
    old.fire()
    assert startup._navigating and not current.cancelled
    startup.ui_ready(startup._trace.identity)
    assert current.cancelled and not startup._navigating
    current.fire()
    assert not startup._failed


def test_close_cancels_interface_deadline_and_ignores_late_expiry(tmp_path, monkeypatch, deadlines):
    startup, window = controller(tmp_path)
    monkeypatch.setattr(desktop_startup, 'ensure_service', lambda *args, **kwargs: client())
    startup.start()
    startup.close()
    assert deadlines[-1].cancelled
    deadlines[-1].fire()
    assert not startup._failed


def test_service_ready_before_shown_and_old_window_deadline_cannot_fail_navigation(tmp_path, monkeypatch, deadlines):
    startup, window = controller(tmp_path, shown=False)
    calls = []
    monkeypatch.setattr(desktop_startup, 'ensure_service', lambda *args, **kwargs: calls.append(True) or client())
    startup.start()
    assert calls == [True] and not window.urls and not window.evaluations
    window_deadline = deadlines[-1]
    startup.shown()
    assert window_deadline.cancelled and len(window.urls) == 1
    app_deadline = deadlines[-1]
    window_deadline.fire()
    assert startup._navigating and not app_deadline.cancelled
    startup.shown()
    startup.loaded()
    assert len(window.urls) == 1 and not window.evaluations


def test_missing_window_event_has_a_deadline_without_blocking_service(tmp_path, monkeypatch, deadlines):
    startup, window = controller(tmp_path, shown=False)
    calls = []
    monkeypatch.setattr(desktop_startup, 'ensure_service', lambda *args, **kwargs: calls.append(True) or client())
    startup.start()
    deadlines[-1].fire()
    assert startup._failed and calls == [True]
    startup.shown()
    assert 'desktop window did not become ready' in window.scripts[-1]['error']
    assert not window.urls and not window.evaluations


def test_navigation_error_renders_failure_without_holding_preparation_gate(tmp_path, monkeypatch):
    startup, window = controller(tmp_path)
    monkeypatch.setattr(desktop_startup, 'ensure_service', lambda *args, **kwargs: client())
    def load(url):
        assert startup._gate.acquire(blocking=False)
        startup._gate.release()
        raise RuntimeError('Navigation failed')
    window.load_url = load
    startup.start()
    assert window.scripts[-1]['error'] == 'Navigation failed'
    assert not window.evaluations


@pytest.mark.parametrize('shown', [False, True])
def test_deadline_thread_failure_keeps_retry_and_releases_preparation(tmp_path, monkeypatch, shown):
    startup, window = controller(tmp_path, shown=shown)
    timer = desktop_startup.threading.Timer
    class BrokenTimer(timer):
        def start(self):
            raise RuntimeError('Cannot start readiness timer')
    monkeypatch.setattr(desktop_startup.threading, 'Timer', BrokenTimer)
    monkeypatch.setattr(desktop_startup, 'ensure_service', lambda *args, **kwargs: client())
    startup.start()
    assert startup._failed
    assert startup._gate.acquire(blocking=False)
    startup._gate.release()
    startup.shown()
    assert window.scripts[-1]['error'] == 'Cannot start readiness timer'
    monkeypatch.setattr(desktop_startup.threading, 'Timer', timer)
    assert startup._begin(retry=True)
    startup._run()
    assert len(window.urls) == 1


def test_failure_retry_is_single_flight_and_keeps_the_same_window(tmp_path, monkeypatch):
    startup, window = controller(tmp_path)
    count = 0
    def ensure(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            raise ControlError("VALIDATION_FAILED", "The update could not be validated")
        return client()
    monkeypatch.setattr(desktop_startup, "ensure_service", ensure)
    startup.start()
    assert window.scripts[-1]["error"] == "The update could not be validated"
    pending = []
    class DeferredThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            pending.append(self.target)
    monkeypatch.setattr(desktop_startup.threading, "Thread", DeferredThread)
    startup.retry()
    startup.retry()
    assert len(pending) == 1
    pending[0]()
    assert count == 2 and len(window.urls) == 1
    startup.retry()  # The bridge cannot restart an already ready application.
    assert len(pending) == 1


def test_closing_during_preparation_never_navigates_or_retries(tmp_path, monkeypatch):
    startup, window = controller(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def ensure(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return client()
    monkeypatch.setattr(desktop_startup, "ensure_service", ensure)
    worker = threading.Thread(target=startup.start)
    worker.start()
    assert entered.wait(5)
    startup.close()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    startup.retry()
    assert window.urls == []


def test_service_preparation_starts_without_document_loaded(tmp_path, monkeypatch):
    order = []
    class Event:
        def __init__(self):
            self.handlers = []
        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self
        def fire(self):
            for handler in self.handlers:
                handler()
    window = Window()
    window.events = SimpleNamespace(shown=Event(), loaded=Event(), closed=Event())
    def create(*args, **kwargs):
        order.append("window")
        assert "data-desktop-startup" in kwargs["html"]
        assert "url" not in kwargs
        return window
    def ensure(*args, **kwargs):
        order.append("service")
        return client()
    def start(**kwargs):
        order.append("event-loop")
        window.events.shown.fire()
        kwargs["func"]()
        window.events.loaded.fire()
        assert window.navigated.wait(5)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create, start=start))
    monkeypatch.setattr(desktop_startup, "ensure_service", ensure)
    desktop.open_desktop(tmp_path, port=0)
    assert order == ["window", "event-loop", "service"]
    assert len(window.urls) == 1


def test_timing_failure_and_log_io_do_not_hide_original_startup_error(tmp_path):
    trace = StartupTrace(tmp_path)
    with pytest.raises(RuntimeError, match="original"), trace.activate(), startup_stage("test.phase"):
        raise RuntimeError("original")
    row = json.loads((tmp_path / "startup-timings.jsonl").read_text())
    assert row["stage"] == "test.phase" and row["outcome"] == "failed"
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    StartupTrace(blocked).record("test.phase")


def test_thread_start_failure_leaves_retry_available(tmp_path, monkeypatch):
    startup, window = controller(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError('Initial preparation failed')
    monkeypatch.setattr(desktop_startup, 'ensure_service', fail)
    startup.start()
    class BrokenThread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            raise RuntimeError('Could not start the startup worker')
    monkeypatch.setattr(desktop_startup.threading, 'Thread', BrokenThread)
    startup.retry()
    assert 'startup worker' in window.scripts[-1]['error']
    monkeypatch.setattr(desktop_startup, 'ensure_service', lambda *args, **kwargs: client())
    assert startup._begin(retry=True)
    startup._run()
    assert len(window.urls) == 1


def test_failed_frontend_returns_to_startup_and_old_ready_callback_cannot_settle_retry(tmp_path, monkeypatch):
    startup, window = controller(tmp_path)
    monkeypatch.setattr(desktop_startup, "ensure_service", lambda *args, **kwargs: client())
    startup.start()
    old_id = startup._trace.identity
    startup.ui_failed(old_id, "The interface could not load")
    assert "data-desktop-startup" in window.document
    assert window.scripts[-1]["error"] == "The interface could not load"
    count = len(window.scripts)
    startup.loaded()
    assert len(window.scripts) == count
    assert window.scripts[-1]["error"] == "The interface could not load"
    assert startup._begin(retry=True)
    startup._run()
    startup.ui_ready(old_id)
    assert startup._navigating
    startup.ui_ready(startup._trace.identity)
    assert not startup._navigating


def test_startup_document_error_is_text_and_retry_remains_in_the_same_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(desktop_startup.startup_document())
            assert page.locator("#desktop-startup").inner_text() == ""
            assert page.locator(".character-pond").count() == 1
            page.evaluate("""() => {
              window.retryCount=0;window.pywebview={api:{retry(){window.retryCount++;return Promise.resolve(true)}}};
              window.codeyStartup({status:'Could not start Codey',error:'<img src=x> failure'});
            }""")
            assert page.locator("#startup-error").inner_text() == "<img src=x> failure"
            assert page.locator("img").count() == 0
            page.locator("#startup-retry").click()
            assert page.evaluate("retryCount") == 1
            assert page.locator("#startup-retry").is_hidden()
            page.evaluate("window.codeyStartup({status:'Checking the update',error:''})")
            assert page.locator("#startup-retry").is_hidden()
            assert page.locator("#desktop-startup").inner_text() == ""
            page.evaluate("""() => {
              window.pywebview.api.retry=()=>Promise.resolve(false);
              window.codeyStartup({error:'Preparation is still active'});
            }""")
            page.locator('#startup-retry').click()
            assert not page.locator('#startup-retry').is_disabled()
            assert page.locator('#startup-error').inner_text() == 'Preparation is still active'
            page.evaluate("window.dispatchEvent(new Event('codey:desktop-ready'))")
            page.wait_for_function("!document.getElementById('desktop-startup')")
            assert page.locator('.character-pond').count() == 0
        finally:
            browser.close()


def test_embedded_error_cannot_end_the_startup_script():
    from playwright.sync_api import sync_playwright
    message = '</SCRIPT><img src=x onerror="window.injected=true"><!--'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(desktop_startup.startup_document(message))
            assert page.locator('#startup-error').inner_text() == message
            assert page.locator('img').count() == 0
            assert not page.evaluate('Boolean(window.injected)')
        finally:
            browser.close()
