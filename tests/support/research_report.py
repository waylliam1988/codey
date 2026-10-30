"""Canonical tool arguments for tests whose expected result is rendered Markdown."""

from codey.reviews.report_sections import REQUIRED_SECTIONS, parse_sections


def report_args(markdown):
    parts = parse_sections(markdown)
    return {"summary": parts.get("conclusion", ""), "report_sections": {
        key: [parts[key]] for key in REQUIRED_SECTIONS[1:] if key in parts}}
