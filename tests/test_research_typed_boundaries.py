"""Research codecs own snapshots and reject malformed effect inputs."""

from dataclasses import replace

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.note import KnowledgeNote
from codey.research.answer_contract import AnswerScope, digest, submit_answer, validate_submission
from codey.research.finding_report import FindingReport, compile_finding_report
from codey.research.source_gateway import _fetch_bytes
from codey.research.staging import StagedKnowledgeStore
from codey.research.tool_args import KnowledgeCommitRequest
from codey.research.tools import ResearchToolOutput
from tests.test_research_findings import URL, execute, report_args, setup


def test_submission_digest_is_required_and_payload_is_detached():
    scope = AnswerScope(digest("task"), (), ("failed-acquisition",))
    args = {"markdown": "The attempted acquisition did not establish an answer.", "state": "unresolved"}
    submission = submit_answer(scope, args)
    assert validate_submission(submission, "task") == submission
    submission.pop("submission_digest")
    with pytest.raises(ValueError, match="frozen input"):
        validate_submission(submission, "task")


@pytest.mark.parametrize("value", [True, 4, "pdf", [1, 2]])
def test_binary_fetch_boundary_rejects_non_buffers(value):
    with pytest.raises((ValueError, TypeError)):
        _fetch_bytes(value)


def test_commit_decoder_rejects_boolean_revision_and_detaches_input():
    raw = {"commit_id": "commit", "changes": [{"expected_revision": True,
        "value": {"id": "claim", "kind": "claim", "content": "a supported claim", "origin": "model_extraction"}}]}
    with pytest.raises(ValueError):
        KnowledgeCommitRequest.decode(raw)
    raw["changes"][0]["expected_revision"] = 0
    request = KnowledgeCommitRequest.decode(raw)
    raw["changes"][0]["value"]["content"] = "changed"
    assert request.mutations[0].value.content == "a supported claim"


def test_tool_observation_detaches_nested_containers():
    observation = {"rows": [{"title": "original"}]}
    output = ResearchToolOutput("ok", read_observation=observation, ok=True)
    observation["rows"][0]["title"] = "changed"
    assert output.read_observation == {"rows": [{"title": "original"}]}


def test_stage_owns_notes_mutations_and_read_views(tmp_path):
    store, _, _, _ = setup(tmp_path)
    try:
        staged = StagedKnowledgeStore(store)
        note = KnowledgeNote(id="typed-stage", type="note", title="original", body="original", tags=["kept"])
        value = KnowledgeObject("typed-claim", "claim", "original claim", "model_extraction",
                                provenance={"nested": {"value": "original"}})
        mutations = [KnowledgeMutation(value)]
        staged.publish_note(note, "typed-stage-commit", mutations)
        note.tags.append("leaked")
        mutations.clear()
        value.provenance["nested"]["value"] = "changed"
        view = staged.read_note(note.id)
        assert view.tags == ["kept"]
        view.tags.append("read-leak")
        assert staged.read_note(note.id).tags == ["kept"]
        version = staged.read_version(value.id)
        assert version.value.provenance["nested"]["value"] == "original"
        version.value.provenance["nested"]["value"] = "read-leak"
        assert staged.read_version(value.id).value.provenance["nested"]["value"] == "original"
        assert not store.exists(note.id) and store.repository.read(value.id) is None
        staged.commit_to(store)
        staged.commit_to(store)
        assert store.read_note(note.id).tags == ["kept"]
        assert store.repository.read(value.id).value.content == "original claim"
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_finding_snapshot_rejects_rehashed_unknown_fields_and_owns_rows(tmp_path):
    import json

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "passage_ref": next(iter(tools.ledger.read_passages))})
        report = compile_finding_report(report_args(json.loads(saved.model_text)["finding_ref"]), tools.ledger, store)
        clone = replace(report)
        clone.source_versions[0]["title"] = "changed"
        assert clone.source_versions[0]["title"] != report.source_versions[0]["title"]
        raw = report.to_payload()
        raw["source_versions"][0]["hidden_field"] = "cannot silently drop digest-bound fields"
        raw["compilation_digest"] = digest({key: item for key, item in raw.items() if key != "compilation_digest"})
        with pytest.raises(ValueError, match="unrecognized|altered"):
            FindingReport.from_payload(raw)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_invalid_binary_fetch_becomes_failure_without_ledger_pollution(tmp_path, monkeypatch):
    store, tools, _, _ = setup(tmp_path)
    try:
        monkeypatch.setattr(tools.search, "fetch", lambda url: {
            "status": "ok", "url": url, "content_kind": "pdf", "bytes": 128})
        outcome = tools.open_url(URL)
        assert not outcome.ok and "binary data" in outcome.model_text
        assert not tools.ledger.opened_sources and not tools.ledger.read_passages
    finally:
        store.close()
