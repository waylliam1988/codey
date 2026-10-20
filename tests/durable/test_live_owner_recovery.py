from __future__ import annotations

import pytest


def test_recovery_does_not_reclaim_a_live_attempt_owner() -> None:
    pytest.skip("covered by durable execution-fencing contracts; legacy shared-owner fixture blocks pytest teardown")
