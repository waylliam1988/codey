"""replay_script must close its browser context on success and failure."""
from __future__ import annotations


def test_replay_success_closes_context(tmp_path):
    from tests.stress import scheduler as sch

    closed = []
    orig = sch.SoakContext

    class Tracking(orig):
        def close(self):
            closed.append(True)
            return super().close()

    sch.SoakContext = Tracking
    try:
        sch.replay_script([], tmp_path / "ok-home")
    finally:
        sch.SoakContext = orig
    assert closed, "successful replay must close its SoakContext"


def test_replay_failure_still_closes_context(tmp_path):
    from tests.stress import scheduler as sch

    closed = []
    orig = sch.SoakContext

    class Tracking(orig):
        def close(self):
            closed.append(True)
            return super().close()

    sch.SoakContext = Tracking
    try:
        try:
            sch.replay_script([{"op": "no-such-op"}], tmp_path / "fail-home")
        except Exception:
            pass
        else:
            raise AssertionError("bad op must raise SoakFailure")
    finally:
        sch.SoakContext = orig
    assert closed, "failed replay must still close its SoakContext"
