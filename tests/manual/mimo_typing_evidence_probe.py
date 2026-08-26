"""Live boolean-only evidence probe for MiMo typing-transition Flow."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.providers import controls as provider_controls
from codey.storage.local_store import read_json
from codey.providers.registry import connect_provider
from codey.providers.web_drivers import mimo


CASES = {
    "short": (
        "Reply with exactly one JSON object and no markdown: "
        '{{"nonce":"{marker}"}}'
    ),
    "long-code": (
        "Write a self-contained Python example of roughly 80 lines that implements "
        "a bounded LRU cache, includes a short unittest, and ends with this marker "
        "on its own comment line: {marker}"
    ),
    "deep-thinking": (
        "Reason carefully about three edge cases in a bounded LRU cache, then give a "
        "concise final answer ending with this exact marker: {marker}"
    ),
}


def _new_marker() -> str:
    return f"SESSION_CHECK_{uuid.uuid4().hex[:12]}"


@dataclass
class _TextChangeTracker:
    last: str = ""

    def update(self, current: str) -> bool:
        changed = current != self.last
        self.last = current
        return changed


def _event(
    *,
    attempt: int,
    started: float,
    response_nonempty: bool,
    response_changed: bool,
    typing_true: bool,
    typing_false: bool,
    copy_visible: bool,
    generation_active: bool,
    phase: str,
) -> dict[str, object]:
    return {
        "attempt": int(attempt),
        "seconds": round(time.monotonic() - started, 3),
        "phase": phase,
        "response_nonempty": bool(response_nonempty),
        "response_changed": bool(response_changed),
        "typing_known": bool(typing_true or typing_false),
        "typing_true": bool(typing_true),
        "typing_false": bool(typing_false),
        "copy_visible": bool(copy_visible),
        "generation_active": bool(generation_active),
    }


def _attempt_summary(events: list[dict[str, object]]) -> dict[str, object]:
    first_true = next(
        (index for index, item in enumerate(events) if item["typing_true"]),
        None,
    )
    later_false = next(
        (
            index
            for index, item in enumerate(events)
            if first_true is not None and index > first_true and item["typing_false"]
        ),
        None,
    )
    completion_ready = next(
        (
            index
            for index in range(1, len(events))
            if later_false is not None
            and index > later_false
            and events[index - 1]["typing_false"]
            and events[index]["typing_false"]
            and events[index - 1]["response_nonempty"]
            and events[index]["response_nonempty"]
            and not events[index - 1]["response_changed"]
            and not events[index]["response_changed"]
        ),
        None,
    )
    changed_after_first_false = bool(
        later_false is not None
        and any(item["response_changed"] for item in events[later_false + 1 :])
    )
    changed_after_ready = bool(
        completion_ready is not None
        and any(item["response_changed"] for item in events[completion_ready + 1 :])
    )
    return {
        "event_count": len(events),
        "typing_true_seen": first_true is not None,
        "typing_false_after_true": later_false is not None,
        "completion_ready_seen": completion_ready is not None,
        "response_changed_after_first_typing_false": changed_after_first_false,
        "response_changed_after_completion_ready": changed_after_ready,
    }


def _summary(events: list[dict[str, object]]) -> dict[str, object]:
    attempts = sorted({int(item["attempt"]) for item in events})
    per_attempt = {
        str(attempt): _attempt_summary(
            [item for item in events if int(item["attempt"]) == attempt]
        )
        for attempt in attempts
    }
    return {
        "event_count": len(events),
        "attempts": per_attempt,
        "typing_transition_every_attempt": bool(per_attempt)
        and all(item["typing_false_after_true"] for item in per_attempt.values()),
        "completion_ready_every_attempt": bool(per_attempt)
        and all(item["completion_ready_seen"] for item in per_attempt.values()),
        "response_changed_after_completion_ready": any(
            item["response_changed_after_completion_ready"]
            for item in per_attempt.values()
        ),
    }


def _attempt_plan(force_flow: bool) -> tuple[tuple[int, bool], ...]:
    return ((1, True), (2, False)) if force_flow else ((1, False),)


def _probe_ok(
    *,
    markers_ok: bool,
    lifecycle_ok: bool,
    summary: dict[str, object],
) -> bool:
    return bool(
        markers_ok
        and lifecycle_ok
        and summary.get("typing_transition_every_attempt") is True
        and summary.get("completion_ready_every_attempt") is True
        and summary.get("response_changed_after_completion_ready") is False
    )


def _self_test() -> None:
    marker = _new_marker()
    assert marker.startswith("SESSION_CHECK_")
    assert "codey" not in marker.lower()
    events = [
        _event(
            attempt=1,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=True,
            typing_true=True,
            typing_false=False,
            copy_visible=False,
            generation_active=True,
            phase="send",
        ),
        _event(
            attempt=1,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=False,
            typing_true=False,
            typing_false=True,
            copy_visible=True,
            generation_active=False,
            phase="post",
        ),
        _event(
            attempt=1,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=False,
            typing_true=False,
            typing_false=True,
            copy_visible=True,
            generation_active=False,
            phase="post",
        ),
        _event(
            attempt=2,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=True,
            typing_true=True,
            typing_false=False,
            copy_visible=False,
            generation_active=True,
            phase="send",
        ),
        _event(
            attempt=2,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=False,
            typing_true=False,
            typing_false=True,
            copy_visible=True,
            generation_active=False,
            phase="post",
        ),
        _event(
            attempt=2,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=False,
            typing_true=False,
            typing_false=True,
            copy_visible=True,
            generation_active=False,
            phase="post",
        ),
    ]
    summary = _summary(events)
    assert summary["typing_transition_every_attempt"] is True
    assert summary["completion_ready_every_attempt"] is True
    assert summary["response_changed_after_completion_ready"] is False
    assert _probe_ok(markers_ok=True, lifecycle_ok=True, summary=summary) is True
    assert _attempt_plan(False) == ((1, False),)
    assert _attempt_plan(True) == ((1, True), (2, False))
    tracker = _TextChangeTracker()
    assert tracker.update("answer before return") is True
    assert tracker.update("answer before return") is False
    assert tracker.update("answer grew after return") is True

    changed_after_return = list(events[:3])
    changed_after_return.append(
        _event(
            attempt=1,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=True,
            typing_true=False,
            typing_false=True,
            copy_visible=True,
            generation_active=False,
            phase="post",
        )
    )
    assert _summary(changed_after_return)["response_changed_after_completion_ready"] is True
    assert (
        _probe_ok(
            markers_ok=True,
            lifecycle_ok=True,
            summary=_summary(changed_after_return),
        )
        is False
    )

    no_transition = _summary([
        _event(
            attempt=1,
            started=time.monotonic(),
            response_nonempty=True,
            response_changed=False,
            typing_true=False,
            typing_false=True,
            copy_visible=True,
            generation_active=False,
            phase="post",
        )
    ])
    assert no_transition["typing_transition_every_attempt"] is False
    assert _probe_ok(markers_ok=True, lifecycle_ok=True, summary=no_transition) is False
    assert _probe_ok(markers_ok=False, lifecycle_ok=True, summary=summary) is False
    assert _probe_ok(markers_ok=True, lifecycle_ok=False, summary=summary) is False
    encoded = json.dumps({"events": events})
    for forbidden in ("reply", "prompt", "response_text", "url", "cookie"):
        assert forbidden not in encoded


def _run_attempt(
    provider,
    page,
    *,
    attempt: int,
    message: str,
    marker: str,
    force_flow: bool,
    timeout: float,
    post_seconds: float,
    started: float,
    events: list[dict[str, object]],
) -> dict[str, object]:
    original_observation = mimo._completion_observation
    text_changes = _TextChangeTracker()

    def observe(response, *, current: str, stable: bool):
        observation = original_observation(
            response,
            current=current,
            stable=stable,
        )
        try:
            copy_visible = (
                response is not None
                and mimo._copy_button_after_response(page, response) is not None
            )
        except Exception:
            copy_visible = False
        try:
            generation_active = mimo._generation_active(page)
        except Exception:
            generation_active = False
        events.append(
            _event(
                attempt=attempt,
                started=started,
                response_nonempty=observation.response_nonempty,
                response_changed=text_changes.update(current),
                typing_true=observation.typing_true,
                typing_false=observation.typing_false,
                copy_visible=copy_visible,
                generation_active=generation_active,
                phase="send",
            )
        )
        return observation

    completion_patch = (
        mock.patch.object(mimo, "_generation_complete", return_value=False)
        if force_flow
        else nullcontext()
    )
    with (
        mock.patch.object(mimo, "_completion_observation", side_effect=observe),
        completion_patch,
    ):
        reply = provider.send(message, timeout=timeout)

    post_deadline = time.monotonic() + max(0.0, post_seconds)
    while time.monotonic() < post_deadline:
        response = provider_controls.locate_response(
            page,
            mimo.PROVIDER_ID,
            mimo.PROFILE.selectors("response"),
        )
        current = mimo._response_text(response) if response is not None else ""
        typing_state = (
            mimo._response_typing_state(response) if response is not None else None
        )
        try:
            copy_visible = (
                response is not None
                and mimo._copy_button_after_response(page, response) is not None
            )
        except Exception:
            copy_visible = False
        try:
            generation_active = mimo._generation_active(page)
        except Exception:
            generation_active = False
        events.append(
            _event(
                attempt=attempt,
                started=started,
                response_nonempty=bool(current),
                response_changed=text_changes.update(current),
                typing_true=typing_state is True,
                typing_false=typing_state is False,
                copy_visible=copy_visible,
                generation_active=generation_active,
                phase="post",
            )
        )
        time.sleep(0.5)

    return {
        "attempt": attempt,
        "force_flow": force_flow,
        "marker_ok": marker in reply,
        "reply_chars": len(reply),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MiMo typing-transition evidence probe")
    parser.add_argument("--case", choices=tuple(CASES), default="short")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--post-seconds", type=float, default=3.0)
    parser.add_argument("--force-flow", action="store_true")
    parser.add_argument("--no-new-chat", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        print("mimo typing evidence probe self-test: OK")
        return 0

    output = args.output or (
        Path(tempfile.gettempdir()) / f"codey-mimo-typing-{args.case}.json"
    )
    events: list[dict[str, object]] = []
    attempt_results: list[dict[str, object]] = []
    provider = None
    old_store = provider_controls.CONTROL_STORE
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="codey-mimo-typing-") as td:
        provider_controls.CONTROL_STORE = Path(td) / "provider-controls.json"
        try:
            provider = connect_provider("mimo", port=args.port)
            page = provider.session.page
            if not args.no_new_chat:
                provider.new_chat(timeout=min(args.timeout, 90.0))
            statuses: list[str | None] = []
            for attempt, force_flow in _attempt_plan(args.force_flow):
                provider_controls.end_task_context()
                provider_controls.begin_task_context(
                    f"mimo-typing-probe:{args.case}:{attempt}"
                )
                marker = _new_marker()
                message = (
                    CASES[args.case].format(marker=marker)
                    if attempt == 1
                    else CASES["short"].format(marker=marker)
                )
                attempt_results.append(
                    _run_attempt(
                        provider,
                        page,
                        attempt=attempt,
                        message=message,
                        marker=marker,
                        force_flow=force_flow,
                        timeout=args.timeout,
                        post_seconds=args.post_seconds,
                        started=started,
                        events=events,
                    )
                )
                store = read_json(provider_controls.CONTROL_STORE) or {}
                meta = store.get("mimo", {}).get("_revival", {})
                statuses.append(meta.get("status"))

            summary = _summary(events)
            markers_ok = all(bool(item["marker_ok"]) for item in attempt_results)
            lifecycle_ok = (
                statuses == ["provisional", "active"]
                if args.force_flow
                else True
            )
            report = {
                "provider": "mimo",
                "case": args.case,
                "ok": _probe_ok(
                    markers_ok=markers_ok,
                    lifecycle_ok=lifecycle_ok,
                    summary=summary,
                ),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "force_flow": bool(args.force_flow),
                "flow_statuses": statuses,
                "attempt_results": attempt_results,
                "summary": summary,
                "events": events,
            }
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            print(f"[mimo-typing-probe] report: {output}", flush=True)
            return 0 if report["ok"] else 1
        finally:
            try:
                if provider is not None:
                    provider.close()
            finally:
                provider_controls.end_task_context()
                provider_controls.CONTROL_STORE = old_store


if __name__ == "__main__":
    raise SystemExit(main())