"""RunTrace admission rows for completion repair context (0.4.13)."""

from __future__ import annotations

import json
import tempfile

import pytest

from codey.context_epoch import context_epoch_id


def _payload() -> dict[str, object]:
    from codey.completion_repair_context import project_repair_context

    projection = project_repair_context(
        proof={
            "status": "failed",
            "proof_id": "completion_proof:" + "a" * 16,
            "contract_id": "completion_contract:" + "b" * 16,
            "reason_codes": ["relevant_verification_failed"],
            "checks": [
                {
                    "check_id": "relevant_verification",
                    "status": "fail",
                    "reason_code": "relevant_verification_failed",
                }
            ],
        },
        failure_class="product_failure",
        decisive_checks=[{
            "command": "pytest -q",
            "cwd": ".",
            "exit_code": 1,
            "result_summary": (
                "api_key=sk-abcdefghijklmnop123456\n"
                "FAILED tests/test_x.py - assert 1 == 2"
            ),
        }],
        changed_files=["src/foo.py"],
    )
    assert projection.admitted
    return projection.to_payload()


def _open(store, run_id: str):
    return store.open(
        run_id=run_id,
        session_id="s-repair",
        project=None,
        mode_initial="agent",
        provider_initial="deepseek",
    )


def _manifest(store, run_id: str) -> dict:
    return json.loads(
        store.path_for("s-repair", run_id).read_text(encoding="utf-8")
    )


def test_repair_admission_row_is_digest_only_and_send_bound() -> None:
    from codey.run_trace import RunTraceStore

    payload = _payload()
    epoch = context_epoch_id("repair followup outbound bytes")
    with tempfile.TemporaryDirectory() as td:
        store = RunTraceStore(td)
        recorder = _open(store, "run-repair")
        recorder.record_completion_repair_context(payload, epoch_id=epoch)
        recorder.finish(status="done")
        manifest = _manifest(store, "run-repair")

    rows = manifest["completion_repair_context"]
    assert len(rows) == 1
    row = rows[0]
    assert row["digest"] == payload["digest"]
    assert row["epoch_id"] == epoch
    assert row["context_source"] == "completion_repair_context"
    assert row["failure_class"] == "product_failure"
    assert row["proof_id"] == payload["proof_id"]
    raw = json.dumps(manifest, ensure_ascii=False)
    # Raw failure output and secret-bearing lines have no field to live in.
    assert "sk-abcdefghijklmnop" not in raw
    assert "FAILED tests/test_x.py" not in raw


def test_repair_row_dedupes_and_fails_closed_without_digest_or_admission() -> None:
    from codey.run_trace import RunTraceStore

    payload = _payload()
    epoch = context_epoch_id("outbound bytes")
    with tempfile.TemporaryDirectory() as td:
        store = RunTraceStore(td)
        recorder = _open(store, "run-dup")
        recorder.record_completion_repair_context(payload, epoch_id=epoch)
        recorder.record_completion_repair_context(payload, epoch_id=epoch)  # dedupe
        recorder.record_completion_repair_context({"admitted": True}, epoch_id=epoch)
        recorder.record_completion_repair_context({**payload, "admitted": False}, epoch_id=epoch)
        recorder.flush()
        manifest = _manifest(store, "run-dup")

    rows = manifest["completion_repair_context"]
    assert len(rows) == 1
    assert rows[0]["digest"] == payload["digest"]


def test_repair_row_requires_epoch_kwarg_structurally() -> None:
    import inspect

    from codey.run_trace import RunTraceRecorder

    # The binding parameter has no default: an admitted row cannot exist
    # outside a send-boundary binding.
    signature = inspect.signature(RunTraceRecorder.record_completion_repair_context)
    assert signature.parameters["epoch_id"].default is inspect.Parameter.empty

    from codey.run_trace import RunTraceStore

    payload = {
        "schema_version": 1,
        "context_source": "completion_repair_context",
        "admitted": True,
        "digest": "sha256:" + "4" * 64,
    }
    with tempfile.TemporaryDirectory() as td:
        store = RunTraceStore(td)
        recorder = _open(store, "run-no-epoch")
        with pytest.raises(TypeError):
            recorder.record_completion_repair_context(payload)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "bad",
    (
        "",
        "not-an-epoch",
        "ctx_epoch:",
        "ctx_epoch:" + "x" * 16,
        "ctx_epoch:" + "a" * 15,
        "ctx_epoch:" + "A" * 16,
        "sha256:" + "a" * 64,
    ),
)
def test_repair_row_rejects_malformed_epochs_fail_closed(bad: str) -> None:
    from codey.run_trace import RunTraceStore

    payload = _payload()
    with tempfile.TemporaryDirectory() as td:
        store = RunTraceStore(td)
        recorder = _open(store, f"run-bad-{abs(hash(bad)) % 10000}")
        recorder.record_completion_repair_context(payload, epoch_id=bad)
        recorder.flush()
        manifest = _manifest(store, f"run-bad-{abs(hash(bad)) % 10000}")
        assert manifest["completion_repair_context"] == []


def test_repair_row_survives_a_rejected_epoch_then_admits_on_valid_send() -> None:
    from codey.run_trace import RunTraceStore

    payload = _payload()
    good = context_epoch_id("outbound bytes")
    with tempfile.TemporaryDirectory() as td:
        store = RunTraceStore(td)
        recorder = _open(store, "run-good-epoch")
        for epoch in ("", good):
            recorder.record_completion_repair_context(payload, epoch_id=epoch)
        recorder.flush()
        manifest = _manifest(store, "run-good-epoch")

    rows = manifest["completion_repair_context"]
    assert len(rows) == 1
    assert rows[0]["epoch_id"] == good
