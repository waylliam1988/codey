"""Borrowed-tab sibling probes for provider recovery.

When one provider's controls or flow break, a healthy sibling tab can answer
a bounded chooser question (profile-doctor candidate, flow predicate) without
opening a new browser context. This module owns that plumbing: candidate
iteration, tab borrowing, greeting, and choosing. ``AppContext`` keeps thin
delegates (``handle_profile_doctor`` / ``handle_flow_recovery``) so existing
wiring and doubles keep working; the logic lives here by lifecycle boundary,
not by file size.

Import cost: the registry (Playwright-backed) loads only when a probe runs.
Everything else here is light, but this module is still imported lazily from
the ``AppContext`` delegates to keep ``import codey.app.context`` cheap.
"""

from __future__ import annotations

import time
from typing import Any

from codey.app import provider_services as provider_services
from codey.providers import controls as provider_controls
from codey.providers import flow as provider_flow
from codey.providers import profile_doctor
from codey.runtime.core import cancellation

PROFILE_DOCTOR_TIMEOUT = 90.0


def _profile_doctor_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    return max(0.1, min(PROFILE_DOCTOR_TIMEOUT, remaining))


def borrow_open_provider(provider_id: str, owner_page: Any) -> Any | None:
    return provider_services.borrow_open_provider(provider_id, owner_page)


def _sibling_candidates(ctx: Any, request_provider_id: str, deadline: float):
    """Healthy sibling ids within the recovery deadline (shared preamble)."""
    supervisor = ctx.providers.supervisor
    for provider_id in provider_services.reviewer_candidates(
        ctx, request_provider_id, supervisor=supervisor
    )[:3]:
        cancellation.check()
        if not supervisor.is_available(provider_id):
            continue
        if time.monotonic() >= deadline:
            return
        yield provider_id


def handle_profile_doctor(
    ctx: Any,
    request: profile_doctor.ProfileDoctorRequest,
) -> str | None:
    """Try healthy sibling tabs within one bounded recovery deadline."""
    cancellation.check()
    deadline = time.monotonic() + PROFILE_DOCTOR_TIMEOUT
    for provider_id in _sibling_candidates(ctx, request.provider_id, deadline):
        helper = borrow_open_provider(provider_id, request.page)
        if helper is None:
            continue
        try:
            helper.new_chat(timeout=_profile_doctor_timeout(deadline))
        except cancellation.TaskCancelled:
            helper.close()
            raise
        except cancellation.DeadlineExceeded:
            helper.close()
            return None
        except Exception:
            helper.close()
            continue
        ctx.set_provider_session(provider_id, None)
        try:
            selected = profile_doctor.choose_candidate(
                request,
                lambda prompt, helper=helper: helper.send(
                    prompt,
                    timeout=_profile_doctor_timeout(deadline),
                ),
            )
        except cancellation.TaskCancelled:
            raise
        except cancellation.DeadlineExceeded:
            return None
        except Exception:
            continue
        finally:
            helper.close()
        if selected:
            return selected
    return None


def handle_flow_recovery(
    ctx: Any,
    request: provider_flow.FlowRecoveryRequest,
) -> str | None:
    """Ask healthy siblings to choose only among fixed flow predicates."""
    cancellation.check()
    deadline = time.monotonic() + PROFILE_DOCTOR_TIMEOUT
    for provider_id in _sibling_candidates(ctx, request.provider_id, deadline):
        helper = borrow_open_provider(provider_id, request.page)
        if helper is None:
            continue
        with provider_controls.suppress_assistance():
            try:
                helper.new_chat(timeout=_profile_doctor_timeout(deadline))
            except cancellation.TaskCancelled:
                helper.close()
                raise
            except cancellation.DeadlineExceeded:
                helper.close()
                return None
            except Exception:
                helper.close()
                continue
            ctx.set_provider_session(provider_id, None)
            try:
                selected = provider_flow.choose_candidate(
                    request,
                    lambda prompt, helper=helper: helper.send(
                        prompt,
                        timeout=_profile_doctor_timeout(deadline),
                    ),
                )
            except cancellation.TaskCancelled:
                raise
            except cancellation.DeadlineExceeded:
                return None
            except Exception:
                continue
            finally:
                helper.close()
        if selected:
            return selected
    return None


__all__ = [
    "PROFILE_DOCTOR_TIMEOUT",
    "borrow_open_provider",
    "handle_flow_recovery",
    "handle_profile_doctor",
]
