"""Ablation mechanics are bounded to the experiment and use the real kernel."""

import json

import pytest

from codey.operations import kernel_prompt, kernel_protocol, knowledge_context
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
from codey.research import answer_context, answer_contract
from tests.manual.research_process_review import ARMS, _rewrite_view, ablation
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL


@pytest.mark.parametrize("arm", ARMS)
def test_each_experimental_patch_restores_original_owners(arm):
    targets = ((kernel_prompt, "kernel_prompt_for_session"), (kernel_prompt, "_repair_prompt"),
               (kernel_protocol, "controller_allowed_for_session"), (knowledge_context, "knowledge_background"),
               (answer_context, "answer_context"), (answer_contract, "submit_answer"),
               (answer_contract, "answer_spec"), (answer_contract, "ANSWER_GUIDANCE"))
    before = [getattr(module, name) for module, name in targets]
    with ablation(arm):
        pass
    assert before == [getattr(module, name) for module, name in targets]


def test_view_ablation_preserves_text_evidence_task_and_other_indices():
    row = {"read_receipts": ["receipt"], "source_locators": ["locator"], "omitted_windows": ["recovery"],
           "investigation_attempts": [{"error": "details", "ok": False}], "latest_error": ["details"]}
    text = "[e1] Exact source\n\n" + json.dumps(row, sort_keys=True) + "\n\nComplete user task (verbatim):\nQUESTION"
    changed = _rewrite_view(text, fields=("read_receipts",))
    assert "Exact source" in changed and "QUESTION" in changed and "locator" in changed and "recovery" in changed
    assert '"read_receipts":[]' in changed
    without_error = _rewrite_view(text, strip_errors=True)
    assert "details" not in without_error and '"ok":false' in without_error


@pytest.mark.usefixtures("public_source_dns")
def test_state_omission_still_uses_original_research_publication(tmp_path):
    def factory(provider):
        calls = []

        def send(prompt):
            calls.append(prompt)
            row = ({"tool": "open_url", "args": {"url": URL}} if len(calls) == 1 else
                   {"tool": "submit_research_answer", "args": {"markdown": "Only one writer at a time [e1]."}})
            return json.dumps(row)

        provider.send = send
        return provider

    with ablation("no_state_field"):
        result = _run(tmp_path, native=True, provider_factory=factory, compact_planning=True,
                      required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS)
    assert result.stop_reason == "done" and result.accepted_report.state == "partial"
    assert result.summary.startswith("Only one writer")
