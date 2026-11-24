from codey.automation.browser_session import BrowserSession
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser


def test_snapshot_reports_disabled_accessibility_group_controls(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    browser = production_browser[2]
    assert browser.navigate(URL).ok
    install(browser, """
        <div role="group" aria-label="Unavailable actions" aria-disabled="true">
            <button>Queue</button>
        </div>
        <button>Send</button>
    """)
    snapshot = browser.snapshot()
    assert snapshot.ok, snapshot.model_text
    controls = {row["name"]: row["disabled"] for row in elements(snapshot.observation)
                if isinstance(row["name"], str)}
    assert controls["Queue"] is True
    assert controls["Send"] is False
