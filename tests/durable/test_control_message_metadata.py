"""External message labels cannot replace input or history identity."""

import pytest

from codey.durable import DurableSession
from tests.support.sqlite_store import TemporarySQLiteStore


def test_metadata_cannot_override_canonical_input_fields():
    session = DurableSession(TemporarySQLiteStore())
    with pytest.raises(ValueError, match="metadata"):
        session.admit_input(conversation_id="c", request_id="r", task_id="t", text="actual task", provider_snapshot={},
                            task_state={"message_metadata": {"text": "forged task"}})
