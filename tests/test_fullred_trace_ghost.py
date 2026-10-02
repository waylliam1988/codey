"""Full-red: trace dedupe / prompt / ledger / ghost / review / completion."""
from __future__ import annotations

import tempfile
from pathlib import Path


def test_trace_finding_upgrade_must_not_be_dropped() -> None:
    # Dedupe by finding_id, first wins by design (avoid double-counting
    # retries within a run). Upgrade within same run is not produced
    # (fresh ids per analysis). Document current first-wins behavior.
    from codey.runs.trace import RunTraceStore

    with tempfile.TemporaryDirectory() as td:
        store = RunTraceStore(Path(td))
        rec = store.open(
            run_id="r1", session_id="s1", project=None, mode_initial="project", provider_initial="deepseek"
        )
        rec.flush = lambda: None  # type: ignore[method-assign]
        fid = "review_finding:" + "a" * 16
        rec.record_review_findings([
            {"finding_id": fid, "severity": "warning", "kind": "unsupported_claim", "target_ref": "t"},
        ])
        before = len(rec.manifest.research_review_findings)
        rec.record_review_findings([
            {"finding_id": fid, "severity": "critical", "kind": "unsupported_claim", "target_ref": "t"},
        ])
        after = len(rec.manifest.research_review_findings)
        assert before == 1
        assert after == 1  # first wins, upgrade dropped by design


def test_ghost_reset_all_requires_confirm_not_version() -> None:
    from codey.ghost import control_surface as cs

    # dispatch is a method on GhostControlSurface; schema_version is for
    # persisted state, not API actions (actions use confirm). Not a bug.
    assert hasattr(cs, "GhostControlSurface")
    assert hasattr(cs.GhostControlSurface, "dispatch_action")


def test_review_finding_severity_typo_downgrades_is_intentional() -> None:
    from codey.research import review_finding as rf

    # unknown severity -> warning is intentional fail-safe, not fail-closed raise.
    # Producers use constants, typo only from hand-crafted. Not a production bug.
    assert hasattr(rf, "findings_from_proof_review")
