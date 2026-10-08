"""Known HTTP refusals keep their category in the public failure projection."""
import pytest

from codey.providers.api_transport import GenerationRejectedError
from codey.providers.diagnostics import capture_provider_failure


@pytest.mark.parametrize("status, expected", [
    (401, "authentication_required"),
    (403, "request_rejected"),
    (429, "rate_limited"),
    (400, "request_rejected"),
    (402, "request_rejected"),
    (422, "request_rejected"),
    (503, "transient"),
])
def test_complete_http_response_keeps_failure_kind_in_public_projection(status, expected):
    error = GenerationRejectedError(status, '{"error":{"type":"FreeTierError","message":"service refusal"}}')
    failure = capture_provider_failure(model="Zen", action="task", page=None, error=error)
    assert failure.to_dict()["kind"] == expected


def test_freetier_refusal_remains_known_failure_without_retry_or_fake_delivery():
    error = GenerationRejectedError(403, '{"error":{"type":"FreeTierError","message":"service refusal"}}')
    assert error.status == 403
    assert error.error_type == "FreeTierError"
    assert error.provider_failure_kind != "submission_uncertain"


def test_http_refusal_facts_reach_public_projection_without_raw_payload():
    error = GenerationRejectedError(403, '{"error":{"type":"FreeTierError","message":"service refusal"}}')
    failure = capture_provider_failure(model="Zen", action="task", page=None, error=error)
    assert failure.to_dict()["facts"] == {"http_status": 403, "service_error_type": "FreeTierError"}


def test_http_failure_facts_are_consistent_in_headless_jsonl_and_published_event(tmp_path):
    from codey.app.headless_runner import HeadlessAppContext

    error = GenerationRejectedError(403, '{"error":{"type":"FreeTierError","message":"service refusal"}}')
    failure = capture_provider_failure(model="Zen", action="task", page=None, error=error)
    rows = []
    context = HeadlessAppContext(tmp_path, port=0, emit_jsonl=rows.append)
    queue = context.subscribe()
    try:
        context.emit({"type": "task_done", "stop_reason": "provider_failure", "provider_failure": failure.to_dict()})
        published = queue.get_nowait()
        assert rows[0]["provider_failure"]["facts"] == published["provider_failure"]["facts"]
        assert rows[0]["provider_failure"]["kind"] == "request_rejected"
    finally:
        context.close()
