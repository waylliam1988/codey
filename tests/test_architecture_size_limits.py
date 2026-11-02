"""Size-debt exceptions must ratchet downward and cannot silently go stale."""

import pytest

from tests.support.architecture_limits import size_limit_violations


@pytest.mark.parametrize("sizes,limits,expected", [
    ({"small.py": 1000}, {}, []),
    ({"large.py": 1200}, {"large.py": 1200}, []),
    ({"new.py": 1001}, {}, ["new oversized file"]),
    ({"large.py": 1201}, {"large.py": 1200}, ["grew from 1200 to 1201"]),
    ({"large.py": 1100}, {"large.py": 1200}, ["lower ceiling from 1200 to 1100"]),
    ({"large.py": 1000}, {"large.py": 1200}, ["remove exception; now 1000"]),
    ({}, {"gone.py": 1200}, ["remove exception for missing file"]),
    ({"small.py": 900}, {"small.py": 900}, ["invalid exception ceiling"]),
    ({"large.py": 1200}, {"large.py": True}, ["invalid exception ceiling"]),
])
def test_size_debt_gate_reports_required_action(sizes, limits, expected):
    issues = size_limit_violations(sizes, limits)
    assert len(issues) == len(expected)
    assert all(fragment in issue for fragment, issue in zip(expected, issues, strict=True))
