"""Provider-control flows that need a live task state.

Borrowed-tab sibling probes (profile-doctor, flow recovery) plus the
click-capture teach loop: candidate iteration, tab borrowing, and choosing.
Callers bind these with the state (``functools.partial(handle_*, ctx)``);
``AppContext`` itself stays assembly-only and owns none of this logic.

Import cost: the registry (Playwright-backed) loads only when a probe runs.
Everything else here is light.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from codey.app import provider_services as provider_services
from codey.operations.task_state import TaskState
from codey.providers import controls as provider_controls
from codey.providers import flow as provider_flow
from codey.providers import profile_doctor
from codey.runtime.core import cancellation

CONTROL_TEACH_TIMEOUT = 300.0

PROFILE_DOCTOR_TIMEOUT = 90.0


def _profile_doctor_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    return max(0.1, min(PROFILE_DOCTOR_TIMEOUT, remaining))


def borrow_open_provider(provider_id: str, owner_page: Any) -> Any | None:
    return provider_services.borrow_open_provider(provider_id, owner_page)


def _sibling_candidates(ctx: TaskState, request_provider_id: str, deadline: float):
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
    ctx: TaskState,
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
    ctx: TaskState,
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


def handle_control_teach(ctx: TaskState, request: provider_controls.ControlTeachRequest):
    """Run the click-capture loop for one provider-control teach request."""
    while True:
        teach_id = "teach_" + uuid.uuid4().hex[:12]
        token = provider_controls.start_click_capture(request.page)
        pending = {
            "id": teach_id,
            "request": request,
            "token": token,
            "event": threading.Event(),
            "cancelled": False,
        }
        with ctx.lock:
            active = ctx.current_run()
            run_id = active.run_id if active is not None and active.session_id == request.session_id else ""
            pending["ui_event"] = {
                "type": "teach_request",
                "run_id": run_id,
                "session_id": request.session_id,
                "id": teach_id,
                "text": request.message,
            }
            ctx.approvals.add_teach(teach_id, pending)
        ctx.emit(pending["ui_event"])
        if not pending["event"].wait(CONTROL_TEACH_TIMEOUT):
            ctx.pop_pending_teach(teach_id)
            provider_controls.cancel_click_capture(request.page)
            raise TimeoutError("Timed out waiting for Resume")
        if pending.get("cancelled"):
            provider_controls.cancel_click_capture(request.page)
            raise provider_controls.ControlTeachCancelled("control teaching was cancelled")
        try:
            captured = provider_controls.finish_click_capture(
                request.page,
                token,
                request.action,
                timeout=1.0,
            )
            return provider_controls.resolve_captured_control(request, captured)
        except ValueError:
            continue
        finally:
            ctx.pop_pending_teach(teach_id)


__all__ = [
    "CONTROL_TEACH_TIMEOUT",
    "PROFILE_DOCTOR_TIMEOUT",
    "borrow_open_provider",
    "handle_control_teach",
    "handle_flow_recovery",
    "handle_profile_doctor",
]
