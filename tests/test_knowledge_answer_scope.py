"""Scope selection uses the exact versions cited by answers and inferences."""

from contextlib import closing
from pathlib import Path

import pytest

from codey.knowledge.answer_refs import KnowledgeReferenceError, answer_references, answer_scope
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.research.references import bind_version
from codey.research.staging import StagedKnowledgeStore
from codey.research.tools import ResearchTools
from codey.types.json_value import parse_json_object, require_json_string


def test_answer_scope_requires_the_original_reference_to_still_exist(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        premise = KnowledgeObject("premise", "claim", "A personal observation", "user_statement",
                                  scope="personal", scope_id=store.owner_id)
        store.commit_knowledge("premise-commit", [KnowledgeMutation(premise)])
        refs = list(answer_references([{"id": premise.id, "revision": 1}], store))
        assert answer_scope(refs, store, project="", session_id="") == ("personal", store.owner_id)

        store.repository.purge([premise.id])

        with pytest.raises(KnowledgeReferenceError) as rejected:
            answer_scope(refs, store, project="", session_id="")
        assert rejected.value.reference == refs[0]


def test_inference_inherits_the_scope_of_a_staged_premise(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        parent = ResearchTools(search=None, store=store, changes=None, session_id="session")
        tools = parent.create_staged()
        staged = tools.store
        assert isinstance(staged, StagedKnowledgeStore)
        premise = KnowledgeObject("premise", "claim", "A personal observation", "user_statement",
                                  scope="personal", scope_id=store.owner_id)
        note = KnowledgeNote.create(type="fact", title="Personal observation", body=premise.content, id=premise.id)
        staged.publish_note(note, "premise-commit", [KnowledgeMutation(premise)])
        version = staged.read_version(premise.id, 1)
        assert version is not None and store.repository.read(premise.id, 1) is None
        label = bind_version(tools.ledger, version)

        result = tools.save_inference({"statement": "An inference from the personal observation",
                                       "premise_refs": [label], "limitations": "One observation only"})

        assert result.ok, result.model_text
        output = parse_json_object(result.model_text)
        binding = tools.ledger.references[require_json_string(output["finding_ref"], "finding_ref")]
        inference = staged.read_version(binding.object_id, binding.revision)
        assert inference is not None
        assert (inference.value.scope, inference.value.scope_id) == ("personal", store.owner_id)

        parent.commit_staged(tools)
        persisted = store.repository.read(binding.object_id, binding.revision)
        assert persisted is not None and persisted.value == inference.value
