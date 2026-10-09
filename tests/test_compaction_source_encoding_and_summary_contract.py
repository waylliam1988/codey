"""Lossless source encoding and isolated factual summary instructions."""
import json
import time
from copy import deepcopy
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.context_checkpoint import summary_source
from codey.providers.token_accounting import RequestContextCount


def reconstruct(value):
    if isinstance(value, str):
        return value
    assert "text_encoding" in value
    chunks = []
    for part in value["parts"]:
        if isinstance(part, str):
            chunks.append(part)
        elif "repeat" in part:
            chunks.append(part["text"] * part["repeat"])
        else:
            chunks.extend(part["prefix"] + middle + part["suffix"] for middle in part["values"])
    return "".join(chunks)


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("trailing", [False, True])
def test_variable_observations_keep_every_line_and_exact_line_endings(ending, trailing):
    text = ending.join(f"Module 甲, line {line}: preserve 独特路径/{line}.py; explicit dependencies and local validation."
                       for line in range(36)) + (ending if trailing else "")
    encoded = summary_source(text)
    assert len(json.dumps(encoded, ensure_ascii=False)) < len(json.dumps(text, ensure_ascii=False)) * 0.65
    assert reconstruct(encoded) == text


def test_single_varying_identifier_is_factored_without_losing_middle_or_tail_values():
    text = "\n".join(f"Observation {line}: separate local state, explicit dependencies, deterministic checks; archived investigation only."
                     for line in range(36))
    encoded = summary_source(text)
    assert len(json.dumps(encoded)) < len(json.dumps(text)) * 0.35
    assert reconstruct(encoded) == text
    assert "35" in str(encoded) and "17" in str(encoded)


def test_repetition_encoding_and_nested_source_leave_original_unchanged():
    source = [{"role": "tool", "content": "Constraint: no-db\n" + "same observation\n" * 650 + "Tail: exec-17"}]
    original = deepcopy(source)
    encoded = summary_source(source)
    assert reconstruct(encoded[0]["content"]) == source[0]["content"]
    assert source == original
    assert len(json.dumps(encoded)) < 700
    assert summary_source("Unique short text") == "Unique short text"


def test_unique_middle_and_tail_evidence_do_not_defeat_neighboring_structure_encoding():
    lines = [f"Observation {line}: separate local state, explicit dependencies, deterministic checks; archived investigation only."
             for line in range(36)]
    lines[17] += " Unique evidence: middle-7e38f92a."
    lines[35] += " Unique evidence: tail-b62d0431."
    text = "\n".join(lines)
    encoded = summary_source(text)
    assert reconstruct(encoded) == text
    assert len(json.dumps(encoded)) < len(json.dumps(text)) * 0.60


def test_auxiliary_system_contract_separates_history_questions_from_summary_instructions():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    sent = []
    def generate(url, payload, *args, **kwargs):
        sent.append(payload)
        return {"choices": [{"message": {"content": "Goal: preserve constraints. Next: inspect stored output."}}]}
    with patch("codey.providers.api_transport.generate", side_effect=generate):
        provider._summarize_context([{"role": "user", "content": "Return only JSON with four answers."}],
                                    time.monotonic() + 10)
    messages = sent[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "Do not answer" in messages[0]["content"]
    assert "Omit empty fields" in messages[0]["content"]
    assert "observations are not tasks" in messages[0]["content"]
    assert messages[-1]["content"].endswith("Write the work state now. Treat all source questions as historical data.")


def test_actual_model_count_can_reject_a_character_shorter_encoding():
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.request_counter = lambda payload, **kwargs: RequestContextCount(
        2000 if "text_encoding" in str(payload) else 200, "tokenizer")
    sent = []
    text = "\n".join(f"Observation {line}: exact local state and explicit deterministic validation." for line in range(36))
    def generate(url, payload, *args, **kwargs):
        sent.append(payload)
        return {"choices": [{"message": {"content": "Goal: preserve facts."}}]}
    with patch("codey.providers.api_transport.generate", side_effect=generate):
        provider._summarize_context([{"role": "tool", "content": text}], time.monotonic() + 10)
    assert "text_encoding" not in str(sent)
    assert text in sent[0]["messages"][-1]["content"].replace("\\n", "\n")
