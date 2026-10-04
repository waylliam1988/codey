"""One review contract surface; a rejection without issues is incomplete."""
import inspect

import pytest

from codey.app import review_service
from codey.operations import project_review_phase
from codey.reviews import core, findings


@pytest.mark.parametrize("module,name", [(core, "review_repair_prompt"),
    (findings, "canonical_changed_paths"), (findings, "is_actionable_path"),
    (project_review_phase, "_review_cycle_phase")])
def test_unused_review_exports_are_removed(module, name):
    assert not hasattr(module, name)


def test_review_event_does_not_accept_dead_artifact_arguments():
    assert set(inspect.signature(review_service.emit_review_with_payload).parameters) == {"ctx", "session_id", "text", "review"}


def test_changes_requested_without_actionable_issues_is_incomplete():
    result = core.parse_review_response('{"verdict":"changes_requested","findings":[]}')
    assert not result.is_complete
