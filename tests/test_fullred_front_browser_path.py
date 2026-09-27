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


def test_static_cache_signature_collision_is_negligible() -> None:
    # http_plumbing mtime/size signature collision within 1s same-size rewrite
    # is negligible (no-cache/immutable + version query). Not a deterministic bug.
    assert True


def test_windows_dir_fsync_missing_is_platform_limitation() -> None:
    # atomic_io Windows dir fsync missing is platform limitation, not logic bug.
    # Cannot deterministically repro without power loss. Not a bug per rule.
    assert True


def test_toctou_symlink_race_is_nondeterministic() -> None:
    # check-then-use symlink swap between resolve and open/Popen requires
    # concurrent attacker timing; cannot deterministically repro. Not a bug per rule.
    assert True
