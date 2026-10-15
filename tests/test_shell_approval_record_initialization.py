"""Existing approval history must not prevent application startup."""

import json
from dataclasses import replace

import pytest

from codey.app.context import AppContext
from codey.runtime.effects import shell_approval_records as records


def old_payload(fact, *, has_continuation=False):
    value = fact.to_payload()
    value["schema_version"] = 1
    if not has_continuation:
        value.pop("continuation_run_id")
    return value


def write_history(root, payloads):
    path = root / "runtime" / "shell_approvals.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = ("\n".join(json.dumps(v) for v in payloads) + "\n").encode()
    path.write_bytes(original)
    return path, original


@pytest.mark.parametrize("has_continuation", [False, True])
def test_known_v1_history_upgrades_atomically_and_keeps_original_backup(tmp_path, has_continuation):
    requested = records.ShellApprovalFact("approval", "chat", "run", "requested")
    claimed = replace(requested, kind="execution_claimed")
    path, original = write_history(
        tmp_path, [old_payload(f, has_continuation=has_continuation) for f in (requested, claimed)]
    )
    store = records.ShellApprovalRecordStore(tmp_path)
    assert store.read("approval") == (requested, claimed)
    assert all(json.loads(line)["schema_version"] == 1 for line in path.read_text().splitlines())
    backups = list(path.parent.glob("shell_approvals.before-continuation-*.jsonl"))
    assert len(backups) == (0 if has_continuation else 1)
    if backups:
        assert backups[0].read_bytes() == original
    assert not store.claim_execution(claimed)
    assert records.ShellApprovalRecordStore(tmp_path).latest("approval") == claimed
    assert list(path.parent.glob("shell_approvals.before-continuation-*.jsonl")) == backups


def test_v1_pending_approval_still_recovers_original_command_on_app_start(tmp_path):
    ctx = AppContext(tmp_path)
    artifact = ctx.managed_outputs.write_json_artifact(
        session_id="chat", run_id="run", payload={"command": "echo hello", "cwd": "."}
    )
    ctx.close()
    fact = records.ShellApprovalFact(
        "approval", "chat", "run", "requested", command_ref=artifact.handle, command_digest=artifact.sha256
    )
    write_history(tmp_path, [old_payload(fact)])
    reopened = AppContext(tmp_path)
    try:
        assert reopened.approvals.shell_snapshot()["approval"]["command"] == "echo hello"
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_version",
        "missing_identity",
        "unknown_field",
        "invalid_kind",
        "broken_json",
        "missing_continuation_identity",
    ],
)
def test_invalid_history_is_never_silently_upgraded_or_discarded(tmp_path, damage):
    value = old_payload(records.ShellApprovalFact("approval", "chat", "run", "requested"))
    if damage == "unknown_version":
        value["schema_version"] = 99
    if damage == "missing_identity":
        value.pop("source_run_id")
    if damage == "unknown_field":
        value["unexpected"] = "value"
    if damage == "invalid_kind":
        value["kind"] = "unknown"
    if damage == "missing_continuation_identity":
        value["kind"] = "continuation_started"
    path, original = write_history(tmp_path, [value])
    if damage == "broken_json":
        original += b"{broken\n"
        path.write_bytes(original)
    with pytest.raises(ValueError):
        records.ShellApprovalRecordStore(tmp_path)
    assert path.read_bytes() == original
    assert not list(path.parent.glob("shell_approvals.before-continuation-*.jsonl"))


@pytest.mark.parametrize("failed_file", ["backup", "ledger"])
def test_failed_upgrade_preserves_history_and_can_retry(tmp_path, monkeypatch, failed_file):
    fact = records.ShellApprovalFact("approval", "chat", "run", "requested")
    path, original = write_history(tmp_path, [old_payload(fact)])
    atomic = records.write_bytes_atomic

    def fail(target, body):
        if (target == path) == (failed_file == "ledger"):
            raise OSError("simulated write failure")
        return atomic(target, body)

    monkeypatch.setattr(records, "write_bytes_atomic", fail)
    with pytest.raises(OSError, match="simulated write failure"):
        records.ShellApprovalRecordStore(tmp_path)
    assert path.read_bytes() == original
    monkeypatch.setattr(records, "write_bytes_atomic", atomic)
    assert records.ShellApprovalRecordStore(tmp_path).latest("approval") == fact


def test_approval_recorded_after_claim_does_not_make_execution_repeatable(tmp_path):
    requested = records.ShellApprovalFact("approval", "chat", "run", "requested")
    claimed = replace(requested, kind="execution_claimed")
    approved = replace(requested, kind="approved")
    write_history(tmp_path, [old_payload(f) for f in (requested, claimed, approved)])
    store = records.ShellApprovalRecordStore(tmp_path)
    assert not store.claim_execution(claimed)
    assert store.read("approval") == (requested, claimed, approved)


def test_concurrent_initialization_preserves_one_backup(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    fact = records.ShellApprovalFact("approval", "chat", "run", "requested")
    path, original = write_history(tmp_path, [old_payload(fact)])
    with ThreadPoolExecutor(max_workers=4) as workers:
        stores = list(workers.map(lambda _: records.ShellApprovalRecordStore(tmp_path), range(8)))
    assert all(store.latest("approval") == fact for store in stores)
    backups = list(path.parent.glob("shell_approvals.before-continuation-*.jsonl"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
