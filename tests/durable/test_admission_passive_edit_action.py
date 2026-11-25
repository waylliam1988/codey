import pytest

from codey.durable import DurableSession
from tests.support.sqlite_store import TemporarySQLiteStore


def test_passive_write_rejects_non_string_edit_action_before_admission() -> None:
    session = DurableSession(TemporarySQLiteStore())
    try:
        sequence = session.commit_sequence()

        with pytest.raises(ValueError, match="unknown passive entry edit action"):
            session.admit_write(
                conversation_id="new-conversation",
                request_id="invalid-write",
                kind="context_edit",
                data={},
                display_only=False,
                edits=({"target": "entry-1", "action": ["omit"]},),
            )

        assert not session.has_conversation("new-conversation")
        assert session.commit_sequence() == sequence
    finally:
        session.close()
