"""Replay timing distinguishes tokenizer round trips from model generation."""
import urllib.request
from types import SimpleNamespace

from tests.manual.context_compaction_benchmark_ab import install_network_boundary


def test_request_timing_includes_response_read_without_recording_prompt_content(monkeypatch):
    response = SimpleNamespace(status=200, read=lambda *args: b'{"value":42}')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', lambda *args, **kwargs: response)
    ticks = iter((10.0, 10.4))
    monkeypatch.setattr('tests.manual.context_compaction_benchmark_ab.time.monotonic', lambda: next(ticks))
    metrics = []
    install_network_boundary('http://127.0.0.1:5001/v1', timings=metrics)
    request = urllib.request.Request('http://127.0.0.1:5001/api/extra/tokencount', data=b'private prompt')
    assert urllib.request.build_opener().open(request).read() == b'{"value":42}'
    assert metrics == [{'path': '/api/extra/tokencount', 'seconds': 0.4}]
    assert 'private' not in str(metrics)
