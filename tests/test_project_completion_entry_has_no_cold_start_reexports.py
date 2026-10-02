"""Phase types and helpers belong to their owner, not an entry facade."""

import pytest

from codey.operations import project_completion_flow


@pytest.mark.parametrize("name", [
    "COMPLETION_REPAIR_FOLLOWUP", "MAX_COMPLETION_REPAIR_ROUNDS",
    "NEW_PROJECT_IGNORED_DIRS", "NEW_PROJECT_IGNORED_FILES", "AgentAccess",
    "PersistenceAccess", "ProjectRuntimeMutationError", "ReviewAccess",
    "RuntimeAccess", "VerificationAccess", "blocked_result",
    "handle_project_tool_event", "managed_tool_fns", "record_analysis_run",
    "record_completion_proof_trace", "record_edit_integrity_trace",
    "record_review_input_prepared_trace", "safe_verification_map",
])
def test_project_entry_does_not_reexport_unconsumed_phase_members(name):
    assert not hasattr(project_completion_flow, name)
