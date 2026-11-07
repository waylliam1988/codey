"""Language-neutral behavior vectors; expected outputs are committed, never regenerated in tests."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from codey.operations.kernel_driver import decode_provider_invocation
from codey.operations.project_completion_checks import _gap_for_latest_row
from codey.providers.context_ledger import digest
from codey.providers.request_codec import encode_provider_request
from codey.providers.response_record import decode_reply, encode_reply, json_value
from codey.providers.text_result_views import validate_text_results
from codey.research.passages import passage_ref
from codey.runtime.core.operation_reducer import next_runtime_action
from codey.runtime.log.session_view import SessionView

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "kernel_go_contract_v1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["provider_invocations"], ids=lambda case: case["method"])
def test_frozen_provider_invocation_vector(case):
    invocation = decode_provider_invocation(case)
    assert invocation.method == case["method"]
    assert encode_provider_request(invocation) == case


@pytest.mark.parametrize("case", VECTORS["replies"], ids=lambda case: case["protocol"])
def test_reply_wire_vector(case):
    assert encode_reply(decode_reply(case)) == case


@pytest.mark.parametrize("case", VECTORS["text_spans"], ids=lambda case: case["id"])
def test_unicode_coordinate_and_digest_vector(case):
    spans = validate_text_results(case["text"], case["spans"])
    assert len(case["text"]) == case["codepoints"]
    assert len(case["text"].encode("utf-8")) == case["utf8_bytes"]
    for span, expected in zip(spans, case["excerpts"], strict=True):
        assert case["text"][span["start"]:span["end"]] == expected
        assert digest(expected) == span["digest"]


@pytest.mark.parametrize("case", VECTORS["passage_refs"], ids=lambda case: case["id"])
def test_passage_identity_vector(case):
    assert passage_ref(**case["input"]) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["verification_gaps"], ids=lambda case: case["id"])
def test_latest_verification_gap_vector(case):
    assert _gap_for_latest_row(case["row"], case["row"]["command"]) == case["expected"]


def test_missing_operation_state_action_vector():
    assert json_value(asdict(next_runtime_action(SessionView(None, (), ())))) == VECTORS["missing_state_action"]
