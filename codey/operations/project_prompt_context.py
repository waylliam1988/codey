"""Prepare an immutable coding-context projection before pure prompt rendering."""

import logging
from typing import Any

from codey.operations.project_completion_checks import project_completion_checks
from codey.operations.project_verification import refresh_verification_candidates
from codey.operations.task_session import TaskSession
from codey.workspace.coding_context import CodingContext

logger = logging.getLogger(__name__)


def prepare_coding_context(session: TaskSession, *, completion_context: Any = None) -> CodingContext | None:
    if not session.coding_context_enabled or session.task_kind not in {"project", "hybrid", "planning"}:
        return None
    if not session.policy.allows("project.read"):
        return None
    try:
        refresh_verification_candidates(session)
        fresh = any(row.check_id == "relevant_verification" and row.status == "pass"
                    for row in project_completion_checks(session, completion_context))
        return CodingContext(
            read_files=tuple(sorted(session.read_files)),
            edit_eligible_files=tuple(sorted(session.read_files)) if session.policy.allows("project.write") else (),
            changed_files=tuple(sorted(session.edited_files)),
            verification_fresh=fresh,
            verification_forbidden=session.verification_forbidden is True,
            selected_verification=session.selected_verification,
        )
    except Exception as exc:
        # This optional projection never decides completion. The completion
        # gate still enforces requirements when this enrichment is unavailable.
        logger.warning("coding context unavailable: %s", str(exc)[:120])
        return None
