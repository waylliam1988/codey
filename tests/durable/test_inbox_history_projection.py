from codey.durable.inbox import history_event_from_projection
from codey.durable.model import SubmissionPlacement, SubmissionStatus


def test_history_projection_ignores_entries_without_an_inbox_event_type() -> None:
    assert history_event_from_projection(1, "conversation", {"type": ["submission_admitted"]}) is None

    event = history_event_from_projection(2, "conversation", {
        "type": "submission_admitted",
        "submission_id": "submission",
        "request_id": "request",
        "status": "queued",
        "placement": "queued",
    })

    assert event is not None
    assert event.kind == "submission_admitted"
    assert event.status is SubmissionStatus.QUEUED
    assert event.placement is SubmissionPlacement.QUEUED
