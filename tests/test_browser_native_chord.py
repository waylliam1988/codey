from codey.automation.browser_contracts import BrowserAction
from codey.automation.browser_session import BrowserSession
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.types.json_value import require_json_string
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser


def test_native_chord_can_include_the_literal_plus_key(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    browser = production_browser[2]
    assert browser.navigate(URL).ok
    install(browser, """
        <input aria-label="Target" onkeydown="if(event.ctrlKey && event.key === '+') this.value = 'pressed'">
    """)
    target = next(row for row in elements(browser.snapshot().observation) if row["name"] == "Target")
    ref = require_json_string(target["ref"], "ref")
    invalid = browser.action(BrowserAction.PRESS, {"ref": ref, "key": "Control+"})
    assert not invalid.ok
    assert browser.inspect_state("document.querySelector('input').value") == ""

    result = browser.action(BrowserAction.PRESS, {"ref": ref, "key": "Control++"})
    assert result.ok, result.model_text
    assert browser.inspect_state("document.querySelector('input').value") == "pressed"
