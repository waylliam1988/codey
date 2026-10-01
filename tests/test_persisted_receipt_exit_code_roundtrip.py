"""Persisted effect receipts retain strict exit codes (log+receipt+gate, no payload)."""
from __future__ import annotations

import unittest


class PersistedReceiptExitCodeRoundTripTests(unittest.TestCase):
    def test_exact_int_exit_code_stays_success(self) -> None:
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.utils.refs import strict_exit_code, strict_verification_success

        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "control"})))
        session.executed["effect"] = {
            "name": "run",
            "ok": True,
            "call_id": "c1",
            "args_digest": "digest",
            "excerpt": "exit 0",
            "exit_code": 0,
        }

        self.assertEqual(session.executed["effect"].get("exit_code"), 0)
        self.assertIs(type(session.executed["effect"].get("exit_code")), int)
        self.assertEqual(strict_exit_code(session.executed["effect"].get("exit_code")), 0)
        self.assertTrue(strict_verification_success(True, session.executed["effect"].get("exit_code")))

    def test_string_exit_code_never_becomes_success(self) -> None:
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.utils.refs import strict_exit_code, strict_verification_success

        session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "control"})))
        session.executed["effect"] = {
            "name": "run", "ok": True, "call_id": "c1", "args_digest": "digest",
            "excerpt": "exit 0", "exit_code": "0",
        }

        self.assertIsNone(strict_exit_code(session.executed["effect"].get("exit_code")))
        self.assertFalse(strict_verification_success(True, session.executed["effect"].get("exit_code")))


if __name__ == "__main__":
    unittest.main()
