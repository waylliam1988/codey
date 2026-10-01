"""Source hit mapping must validate raw types and commit atomically.

- Non-string url/pages rejected (no str() washing).
- bool/float offset rejected.
- Illegal batch leaves original state unchanged.
- Conflicting second row does not leave first row behind.
- Same id + same target stays idempotent.
"""
from __future__ import annotations


def _session():
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    s = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), task_kind="research", project="")
    s.hit_targets = {}
    return s


def test_non_string_url_rejected():
    from codey.operations.kernel_facts import _apply_hit_targets

    s = _session()
    try:
        _apply_hit_targets(s, {"h1": {"url": 123, "offset": 0, "pages": ""}})
    except ValueError:
        pass
    else:
        raise AssertionError("int url must be rejected")
    assert s.hit_targets == {}


def test_non_string_pages_rejected():
    from codey.operations.kernel_facts import _apply_hit_targets

    s = _session()
    try:
        _apply_hit_targets(s, {"h1": {"url": "https://example.com/a", "offset": 0, "pages": False}})
    except ValueError:
        pass
    else:
        raise AssertionError("bool pages must be rejected")
    assert s.hit_targets == {}


def test_bool_float_offset_rejected():
    from codey.operations.kernel_facts import _apply_hit_targets

    for bad in (True, False, 1.0, "0"):
        s = _session()
        try:
            _apply_hit_targets(s, {"h1": {"url": "https://example.com/a", "offset": bad, "pages": ""}})
        except ValueError:
            pass
        else:
            raise AssertionError(f"offset {bad!r} must be rejected")
        assert s.hit_targets == {}


def test_illegal_second_row_leaves_no_partial_state():
    from codey.operations.kernel_facts import _apply_hit_targets

    s = _session()
    mapping = {
        "h1": {"url": "https://example.com/a", "offset": 0, "pages": ""},
        "h2": {"url": 123, "offset": 0, "pages": ""},
    }
    try:
        _apply_hit_targets(s, mapping)
    except ValueError:
        pass
    else:
        raise AssertionError("batch with illegal second row must be rejected")
    assert s.hit_targets == {}


def test_conflicting_second_row_leaves_no_partial_state():
    from codey.operations.kernel_facts import _apply_hit_targets

    s = _session()
    s.hit_targets = {"h1": {"url": "https://example.com/a", "offset": 0, "pages": ""}}
    before = dict(s.hit_targets)
    mapping = {
        "h2": {"url": "https://example.com/b", "offset": 0, "pages": ""},
        "h1": {"url": "https://example.com/other", "offset": 0, "pages": ""},
    }
    try:
        _apply_hit_targets(s, mapping)
    except ValueError:
        pass
    else:
        raise AssertionError("conflicting batch must be rejected")
    assert s.hit_targets == before
    assert "h2" not in s.hit_targets


def test_same_target_recovery_stays_idempotent():
    from codey.operations.kernel_facts import _apply_hit_targets

    s = _session()
    target = {"url": "https://example.com/a", "offset": 2, "pages": "3"}
    _apply_hit_targets(s, {"h1": dict(target)})
    _apply_hit_targets(s, {"h1": dict(target)})
    assert s.hit_targets == {"h1": target}
