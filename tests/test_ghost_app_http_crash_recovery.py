"""Normal App cold recovery preserves actual HTTP and scoped Ghost admission."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.manual.completion_operation_resume_smoke import BOUNDARIES, SCOPES, run_cell


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("boundary", BOUNDARIES)
@pytest.mark.parametrize("revoked", (False, True), ids=("enabled", "revoked"))
def test_normal_app_http_crash_recovery_and_independent_next_run(
    tmp_path: Path, scope: str, boundary: str, revoked: bool,
) -> None:
    result = run_cell(tmp_path, scope, boundary, revoked)
    print("APP_HTTP_CRASH_CELL " + json.dumps(result, sort_keys=True), flush=True)
    assert result["fixture_valid"] is True
    assert result["violations"] == [], result
