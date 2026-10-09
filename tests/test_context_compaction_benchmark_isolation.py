"""The live benchmark only reaches the selected loopback model and imports its arm."""
import pytest

from tests.manual.context_compaction_benchmark_ab import validate_endpoint, verify_import


@pytest.mark.parametrize("url", ["https://example.com/v1", "http://localhost.evil/v1", "http://user@127.0.0.1/v1", "http://127.0.0.1/v1?q=1"])
def test_remote_or_ambiguous_endpoint_is_rejected(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_worker_cannot_load_the_other_editable_checkout(tmp_path):
    with pytest.raises(ValueError, match="import"):
        verify_import(tmp_path, __file__)
    assert validate_endpoint("http://127.0.0.1:5001/v1") == "http://127.0.0.1:5001/v1"


def test_real_task_reports_the_actual_run_result_contract(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from codey.runtime.core.run_result import RunResult
    from tests.manual.context_compaction_benchmark_ab import real_task
    def run(request):
        assert request.workspace_revision_store is not None
        return RunResult("done", checks_passed=True)
    monkeypatch.setattr("codey.operations.project_adapter.run", run)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="1 passed"))
    (tmp_path / "test_app.py").write_text("from app import RATE\ndef test_rate():\n    assert RATE == 7\n", encoding="utf-8")
    row = real_task(object(), tmp_path)
    assert row["kernel_stop_reason"] == "done"
    assert row["success"]


def test_reference_replay_freezes_actual_source_bytes_and_records_each_hash(tmp_path):
    import hashlib

    from tests.manual.context_compaction_benchmark_ab import REFERENCE_SOURCES, freeze_reference
    source, target = tmp_path / 'original', tmp_path / 'frozen'
    for relative in REFERENCE_SOURCES['opencode']:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'original\r\n')
    hashes = freeze_reference(source, target, 'opencode')
    for relative in hashes:
        (source / relative).write_bytes(b'changed')
        assert (target / relative).read_bytes() == b'original\r\n'
        assert hashes[relative] == hashlib.sha256(b'original\r\n').hexdigest()
