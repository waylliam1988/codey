from codey.completion.edit_integrity import STATUS_MONITOR_ERROR, observe_edit_integrity


def test_failure_extracting_task_authorization_is_reported_as_a_monitor_error() -> None:
    class UnreadableTask:
        def __str__(self) -> str:
            raise ValueError("task text unavailable")

    observation = observe_edit_integrity(task=UnreadableTask(), run_id="run")

    assert observation.status == STATUS_MONITOR_ERROR
    assert observation.user_authorized_test_edit is False
    assert observation.diagnostic_refs == (observation.observation_ref,)
