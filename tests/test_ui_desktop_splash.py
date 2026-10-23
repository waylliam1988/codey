"""The packaged pond covers restoration, without creating another app root."""
import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def test_only_desktop_boot_has_a_pond_cover_and_reveals_after_restore(page):
    expect(page.locator('#desktop-startup')).to_have_count(0)
    state = page.evaluate('UiState.current()')
    pending = []
    def hold(route):
        if route.request.method == 'GET':
            pending.append(route)
        else:
            route.continue_()
    page.route('**/api/ui_state', hold)
    page.evaluate("location.hash='codey_startup=isolated-test'")
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('#desktop-startup')).to_be_visible()
    assert page.locator('#desktop-startup').inner_text() == ''
    assert page.locator('main').count() == 1
    assert page.locator('#desktop-startup .character-pond').count() == 1
    page.wait_for_timeout(100)
    assert pending
    for route in pending:
        route.fulfill(json={'state': state})
    page.wait_for_function("!document.getElementById('desktop-startup')")
    expect(page.locator('#task')).to_be_enabled()


@pytest.mark.parametrize('resource', ['entry', 'dependency'])
def test_main_module_failure_reports_without_executing_the_application(page, resource):
    script = (page.locator('script[type="module"][src]').first.get_attribute('src') if resource == 'entry'
              else page.locator('link[rel="modulepreload"]').first.get_attribute('href'))
    page.add_init_script("""window.failures=[];window.pywebview={api:{
      ui_failed:(id,message)=>{failures.push({id,message});return Promise.resolve()},
      retry:()=>Promise.resolve()
    }};""")
    page.route('**' + script, lambda route: route.abort())
    page.evaluate("location.hash='codey_startup=failed-module'")
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('#startup-retry')).to_be_visible(timeout=2000)
    assert page.evaluate('failures.length') == 1
    assert page.evaluate('failures[0].id') == 'failed-module'
