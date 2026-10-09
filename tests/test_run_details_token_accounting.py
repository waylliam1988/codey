"""Run details separates request context from complete or partial API usage."""


def test_run_details_separates_context_estimate_from_known_usage_and_marks_incomplete():
    from codey.runs.details import _summary_rows

    summary = _summary_rows(None, trace={"api_usage_totals": {
        "requests": 2, "known_input_tokens": 100, "known_output_tokens": 50, "incomplete_requests": 1},
        "api_usage_latest": {"context": {"value": 40, "method": "estimated"},
                       "budget": {"window_tokens": 1000}}})
    rows = {row.label: row.value for row in summary}
    assert rows["API usage"] == "Known: 100 input · 50 output · 1 request with incomplete usage"
    assert rows["Request context"] == "~40 / 1,000 tokens · Last prepared request"
