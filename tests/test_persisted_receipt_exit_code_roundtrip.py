"""Persisted effect receipts retain strict exit codes across session restart."""
from __future__ import annotations

import unittest


class PersistedReceiptExitCodeRoundTripTests(unittest.TestCase):
    def test_exit_code_survives_task_session_payload_round_trip(self) -> None:
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "control"})))
        session.executed["effect"] = {
            "name": "run",
            "ok": True,
            "call_id": "c1",
            "args_digest": "digest",
            "excerpt": "exit 0",
            "exit_code": 0,
        }

        restored = TaskSession.from_payload(session.to_payload(), policy=session.policy)

        self.assertEqual(restored.executed["effect"].get("exit_code"), 0)

    def test_invalid_exit_code_is_not_persisted(self) -> None:
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "control"})))
        session.executed["effect"] = {
            "name": "run", "ok": True, "call_id": "c1", "args_digest": "digest",
            "excerpt": "exit 0", "exit_code": "0",
        }

        row = TaskSession.from_payload(session.to_payload(), policy=session.policy).to_payload()["executed"]["effect"]
        self.assertNotIn("exit_code", row)


if __name__ == "__main__":
    unittest.main()
