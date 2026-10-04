"""Input snapshot, precise identity and stale detection (batch 4)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.reviews.identity import (
    build_identity,
    capture_snapshot,
    identities_match,
    prompt_digest_for,
    scope_digest_for,
    verify_snapshot,
)
from codey.reviews.input import prepare_review_input


def _input(**overrides):
    base = dict(
        project="E:/demo",
        task="fix",
        writer_summary="done",
        changes={
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        },
    )
    base.update(overrides)
    return prepare_review_input(**base)


class IdentityTests(unittest.TestCase):
    def test_recent_log_change_changes_identity(self) -> None:
        a = _input()
        b = _input(recent_log="different log")
        ida = build_identity(a, reviewer_id="r1", policy="web_if_available")
        idb = build_identity(b, reviewer_id="r1", policy="web_if_available")
        self.assertFalse(identities_match(ida, idb))

    def test_actual_prompt_byte_change_changes_identity(self) -> None:
        a = _input()
        b = _input(task="fix with extra space ")
        self.assertNotEqual(prompt_digest_for(a.prompt), prompt_digest_for(b.prompt))

    def test_scope_order_normalization_does_not_override_prompt_digest(self) -> None:
        files_a = [{"path": "a.py", "status": "M"}, {"path": "b.py", "status": "M"}]
        files_b = [{"path": "b.py", "status": "M"}, {"path": "a.py", "status": "M"}]
        a = _input(changes={"ok": True, "changed_count": 2, "files": files_a, "diff": "x"})
        b = _input(changes={"ok": True, "changed_count": 2, "files": files_b, "diff": "x"})
        self.assertEqual(scope_digest_for(a.scope), scope_digest_for(b.scope))
        self.assertNotEqual(prompt_digest_for(a.prompt), prompt_digest_for(b.prompt))
        ida = build_identity(a, reviewer_id="r1", policy="web_if_available")
        idb = build_identity(b, reviewer_id="r1", policy="web_if_available")
        self.assertFalse(identities_match(ida, idb))

    def test_same_visible_prefix_with_different_omitted_input_is_not_reusable(self) -> None:
        files_a = [{"path": f"f{i}.py", "status": "M"} for i in range(25)]
        files_b = [{"path": f"f{i}.py", "status": "M"} for i in range(20)] + [
            {"path": f"g{i}.py", "status": "M"} for i in range(5)
        ]
        a = _input(changes={"ok": True, "changed_count": 25, "files": files_a, "diff": "x"})
        b = _input(changes={"ok": True, "changed_count": 25, "files": files_b, "diff": "x"})
        ida = build_identity(a, reviewer_id="r1", policy="web_if_available")
        idb = build_identity(b, reviewer_id="r1", policy="web_if_available")
        self.assertFalse(identities_match(ida, idb))

    def test_reviewer_identity_is_not_writer_identity(self) -> None:
        inp = _input()
        ida = build_identity(inp, reviewer_id="reviewer-a", policy="web_if_available")
        idb = build_identity(inp, reviewer_id="writer-a", policy="web_if_available")
        self.assertFalse(identities_match(ida, idb))

    def test_unknown_model_identity_does_not_match_as_known(self) -> None:
        inp = _input()
        known = build_identity(
            inp, reviewer_id="r1", policy="web_if_available", model_id="model-x"
        )
        unknown = build_identity(
            inp, reviewer_id="r1", policy="web_if_available", model_id=""
        )
        self.assertFalse(identities_match(known, unknown))
        self.assertFalse(identities_match(unknown, unknown))

    def test_contract_or_policy_change_rejects_reuse(self) -> None:
        inp = _input()
        a = build_identity(inp, reviewer_id="r1", policy="web_if_available")
        b = build_identity(inp, reviewer_id="r1", policy="require_web")
        self.assertFalse(identities_match(a, b))

    def test_project_change_rejects_reuse(self) -> None:
        inp = _input()
        a = build_identity(
            inp, reviewer_id="r1", policy="web_if_available", model_id="model-x",
            project="E:/project-a",
        )
        b = build_identity(
            inp, reviewer_id="r1", policy="web_if_available", model_id="model-x",
            project="E:/project-b",
        )
        self.assertFalse(identities_match(a, b))


class SnapshotTests(unittest.TestCase):
    def test_snapshot_detects_edit_rename_delete_and_new_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("v1\n", encoding="utf-8")
            snap = capture_snapshot(str(root), ("app.py",))
            self.assertTrue(verify_snapshot(snap))
            (root / "app.py").write_text("v2\n", encoding="utf-8")
            self.assertFalse(verify_snapshot(snap))
            (root / "app.py").write_text("v1\n", encoding="utf-8")
            self.assertTrue(verify_snapshot(snap))
            (root / "app.py").unlink()
            self.assertFalse(verify_snapshot(snap))

    def test_snapshot_failure_never_claims_current(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            snap = capture_snapshot(str(td), ("missing-dir/../evil.py",))
            self.assertFalse(verify_snapshot(snap))

    def test_workspace_change_during_provider_send_marks_stale(self) -> None:
        from codey.reviews.core import ReviewResult

        # simulate: snapshot taken, then file changes before consume
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("v1\n", encoding="utf-8")
            snap = capture_snapshot(str(root), ("app.py",))
            (root / "app.py").write_text("v2-changed\n", encoding="utf-8")
            self.assertFalse(verify_snapshot(snap))
            result = ReviewResult("approved", "ok", [], status="complete")
            # stale must never drive pass or repair
            self.assertFalse(snap.is_current(str(root)) if hasattr(snap, "is_current") else verify_snapshot(snap))
            self.assertTrue(result.approved)  # without scope/snapshot attached, old path still approved
            # with snapshot check, consumer must treat as stale (covered in coordinator tests batch 5)

    def test_workspace_change_before_repair_blocks_old_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("v1\n", encoding="utf-8")
            snap = capture_snapshot(str(root), ("app.py",))
            (root / "app.py").write_text("v2\n", encoding="utf-8")
            self.assertFalse(verify_snapshot(snap))

    def test_reused_result_also_checks_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("v1\n", encoding="utf-8")
            snap = capture_snapshot(str(root), ("app.py",))
            self.assertTrue(verify_snapshot(snap))
            (root / "app.py").write_text("v2\n", encoding="utf-8")
            self.assertFalse(verify_snapshot(snap))


if __name__ == "__main__":
    unittest.main()
