"""A Windows journal reader must not force a successful upgrade to roll back."""

import pytest

from codey.launcher import runner


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_journal_retries_transient_windows_replacement_failure(tmp_path, monkeypatch, winerror):
    path = tmp_path / "upgrade.json"
    runner.write(path, {"phase": "opening"})
    replace = runner.os.replace
    attempts = []
    delays = []

    def busy(source, target):
        attempts.append(True)
        if len(attempts) < 3:
            assert runner.read(path) == {"phase": "opening"}
            error = PermissionError("temporarily shared")
            error.winerror = winerror
            raise error
        replace(source, target)

    monkeypatch.setattr(runner.os, "replace", busy)
    monkeypatch.setattr(runner.time, "sleep", delays.append)
    runner.write(path, {"phase": "active"})
    assert len(attempts) == 3
    assert delays == [0.01, 0.02]
    assert runner.read(path) == {"phase": "active"}


@pytest.mark.parametrize("winerror,expected_attempts", [(5, 8), (None, 1)])
def test_journal_does_not_hide_permanent_write_failure(tmp_path, monkeypatch, winerror, expected_attempts):
    path = tmp_path / "upgrade.json"
    runner.write(path, {"phase": "opening"})
    attempts = []

    def denied(source, target):
        attempts.append(True)
        error = PermissionError("denied")
        if winerror is not None:
            error.winerror = winerror
        raise error

    monkeypatch.setattr(runner.os, "replace", denied)
    monkeypatch.setattr(runner.time, "sleep", lambda delay: None)
    with pytest.raises(PermissionError):
        runner.write(path, {"phase": "active"})
    assert len(attempts) == expected_attempts
    assert runner.read(path) == {"phase": "opening"}
