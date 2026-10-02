"""Full-red: frontend/browser/path/concurrency deterministic甄别."""
from __future__ import annotations


def test_browser_substring_marker_is_loose() -> None:
    # automation/browser.py must not use bare `marker in url`; evil URL
    # containing marker as query param would falsely match. Correct: hostname.
    marker = "chat.deepseek.com"
    evil = "https://evil.test/?x=chat.deepseek.com"
    assert marker in evil  # loose substring reproduces the attack
    from codey.automation.browser import _url_host_matches

    assert _url_host_matches("https://chat.deepseek.com/", marker) is True
    assert _url_host_matches(evil, marker) is False
    assert _url_host_matches("https://sub.chat.deepseek.com/", marker) is True
    # lock cleaned state: open_chat_page + helpers must use _url_host_matches
    import inspect

    import codey.automation.browser as br

    assert "_url_host_matches" in inspect.getsource(br.open_chat_page)
    assert "hostname" in inspect.getsource(br._url_host_matches).lower()


def test_safe_project_cwd_empty_project_must_reject() -> None:
    from codey.app.shell_service import safe_project_cwd

    try:
        safe_project_cwd("", ".")
    except (ValueError, OSError):
        return
    raise AssertionError("empty project silently resolved to CWD instead of rejecting")
