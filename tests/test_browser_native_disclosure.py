from codey.automation.browser_detail import observe_details, read_detail
from codey.automation.browser_session import BrowserSession
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from tests.test_browser_context_delivery import URL, install
from tests.test_browser_context_delivery import production_browser as production_browser


def test_disclosure_resolves_the_native_summary_after_an_inserted_element(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    browser = production_browser[2]
    assert browser.navigate(URL).ok
    install(browser, """
        <section aria-label="Order A-19">
            <details><summary>Shipping address</summary><p>Dock 27</p></details>
            <details><summary>Empty details</summary></details>
        </section>
    """)
    assert browser.evaluate("""
        for (const details of document.querySelectorAll('details')) {
            const marker = document.createElement('span');
            marker.hidden = true;
            details.prepend(marker);
        }
    """).ok
    document = browser.document_identity()
    assert document is not None
    task = "Read Order A-19"
    controls = observe_details(browser, document, task)
    assert [control.name for control in controls] == ["Shipping address"]
    selected = read_detail(browser, document, task, controls[0].choice(document))
    assert selected is not None and "Dock 27" in str(selected["text"])
    assert browser.inspect_state("document.querySelector('details').open") is False
