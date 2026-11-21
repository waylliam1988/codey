"""Human assistance preserves the target and expires prior controls and pixels."""
from __future__ import annotations

from codey.operations.browser_resources import capture_assistance_binding, resume_assistance_observation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import decode_json_object, require_json_string
from tests.support.task_browser import browser_for


def test_answer_requires_new_observation_without_asserting_access_was_restored() -> None:
    browser = browser_for(lambda route: route.fulfill(content_type="text/html",
        body='<main>Sign in to search this record.</main><button>Continue</button>'))
    try:
        assert browser.navigate('https://example.com/assistance').ok
        old = browser.snapshot()
        rows = old.observation['elements']
        assert isinstance(rows, list)
        ref = require_json_string(decode_json_object(rows[0])['ref'], 'ref')
        results = {'before-question': ToolResult(ToolCall('browser_snapshot', {}), old.model_text, ok=True,
            canonical={'value': old.observation})}
        captured = capture_assistance_binding(browser, results)
        continuation = {'browser_assistance': captured}
        assert captured is not None
        binding = browser.resource_binding()
        continuation = {'browser_assistance': captured}
        hint = resume_assistance_observation(browser, continuation, results)
        assert 'does not prove' in hint
        assert browser.resource_binding() == binding
        assert not browser.click(ref).ok
        current = browser.snapshot()
        assert 'Sign in' in current.model_text
        results['after-question'] = ToolResult(ToolCall('browser_snapshot', {}), current.model_text, ok=True,
            canonical={'value': current.observation})
        assert not resume_assistance_observation(browser, continuation, results)
        fresh_rows = current.observation['elements']
        assert isinstance(fresh_rows, list)
        fresh = require_json_string(decode_json_object(fresh_rows[0])['ref'], 'ref')
        assert fresh != ref and browser.click(fresh).ok
    finally:
        browser.close()
