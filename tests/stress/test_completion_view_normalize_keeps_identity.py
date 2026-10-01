"""Replay comparison must keep workspace fingerprints, not just a bool."""
from __future__ import annotations


def _base_view(**overrides):
    obs = {
        "command": "python -m pytest",
        "cwd": ".",
        "passed": True,
        "exit_code": 0,
        "revision": 1,
        "workspace_revision": 2,
        "workspace_fingerprint": "sha256:" + "b" * 64,
    }
    view = {
        "completed": True,
        "proof_checks": [{"check_id": "relevant_verification", "status": "pass"}],
        "verification_observations": [obs],
        "verification_identity_valid": True,
        "verification_revision": 2,
        "workspace_revision": 2,
        "verification_fingerprint": "sha256:" + "b" * 64,
        "workspace_fingerprint": "sha256:" + "b" * 64,
    }
    view.update(overrides)
    return view


def test_fingerprint_only_change_makes_normalized_views_unequal(tmp_path):
    from tests.stress.test_scheduler_completion_step_produces_real_view import _normalize_completion_view

    a = _base_view()
    b = _base_view()
    b["verification_observations"] = [
        {**b["verification_observations"][0], "workspace_fingerprint": "sha256:" + "c" * 64}
    ]
    b["verification_fingerprint"] = "sha256:" + "c" * 64
    b["workspace_fingerprint"] = "sha256:" + "c" * 64
    assert _normalize_completion_view(a, tmp_path) != _normalize_completion_view(b, tmp_path)


def test_same_relative_content_across_temp_dirs_stays_equal(tmp_path):
    from tests.stress.test_scheduler_completion_step_produces_real_view import _normalize_completion_view

    a = _base_view()
    a["verification_observations"] = [
        {**a["verification_observations"][0], "cwd": str(tmp_path / "orig"), "command": f"pytest {tmp_path / 'orig'}"}
    ]
    b = _base_view()
    b["verification_observations"] = [
        {**b["verification_observations"][0], "cwd": str(tmp_path / "replay"), "command": f"pytest {tmp_path / 'replay'}"}
    ]
    na = _normalize_completion_view(a, tmp_path / "orig", tmp_path)
    nb = _normalize_completion_view(b, tmp_path / "replay", tmp_path)
    assert na == nb
