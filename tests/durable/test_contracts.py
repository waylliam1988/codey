from __future__ import annotations

from contextlib import closing

from codey.durable import (
    ApprovalPort,
    DurableExecutionPort,
    DurableSession,
    EffectPort,
    MemoryDurableStore,
    TaskAdmissionPort,
)


def test_durable_session_exposes_the_stable_capability_contracts() -> None:
    with closing(DurableSession(MemoryDurableStore())) as session:
        assert isinstance(session, TaskAdmissionPort)
        assert isinstance(session, ApprovalPort)
        assert isinstance(session, EffectPort)
        assert isinstance(session, DurableExecutionPort)
