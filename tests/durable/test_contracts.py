from __future__ import annotations

from contextlib import closing

from codey.durable import (
    ApprovalPort,
    DurableExecutionPort,
    DurableSession,
    EffectPort,
    EntryDraft,
    MemoryDurableStore,
    TaskAdmissionPort,
)
from codey.durable.model import thaw_json


def test_durable_session_exposes_the_stable_capability_contracts() -> None:
    with closing(DurableSession(MemoryDurableStore())) as session:
        assert isinstance(session, TaskAdmissionPort)
        assert isinstance(session, ApprovalPort)
        assert isinstance(session, EffectPort)
        assert isinstance(session, DurableExecutionPort)


def test_entry_detaches_objects_inside_nested_json_arrays() -> None:
    source = {"text": "original"}
    entry = EntryDraft("entry", "chat", None, "user_message", {"values": [(source,)]})

    source["text"] = "changed outside the entry"

    assert thaw_json(entry.payload) == {"values": [[{"text": "original"}]]}
