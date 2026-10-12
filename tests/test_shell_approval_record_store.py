from __future__ import annotations

from codey.runtime.effects.shell_approval_records import ShellApprovalFact, ShellApprovalRecordStore


def test_shell_approval_facts_survive_store_reopen(tmp_path) -> None:
    fact = ShellApprovalFact("a1", "s1", "r1", "requested", command_ref="artifact:cmd", command_digest="d" * 64)
    store = ShellApprovalRecordStore(tmp_path)
    store.append(fact)
    reopened = ShellApprovalRecordStore(tmp_path)
    assert reopened.latest("a1") == fact


def test_shell_approval_latest_fact_tracks_settlement(tmp_path) -> None:
    store = ShellApprovalRecordStore(tmp_path)
    store.append(ShellApprovalFact("a1", "s1", "r1", "requested"))
    settled = ShellApprovalFact("a1", "s1", "r1", "execution_settled", result_ref="artifact:result")
    store.append(settled)
    assert store.latest("a1") == settled
