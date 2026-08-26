"""Live fault-injection smoke for bounded provider control revival.

The target provider's production composer selectors are replaced in memory.
Message-box discovery remains local; send-button heuristic selection is disabled
so a healthy sibling must choose among bounded DOM candidates. Recovered controls
are persisted only to a temporary store.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.app import server
from codey.providers import controls as provider_controls, flow as provider_flow
from codey.storage.local_store import read_json
from codey.providers.profiles import ProviderProfile
from codey.providers.registry import connect_provider, provider_ids
from codey.providers.web_drivers import deepseek, glm, mimo, qwen, stepfun


PROVIDER_MODULES = {
    "deepseek": deepseek,
    "mimo": mimo,
    "qwen": qwen,
    "stepfun": stepfun,
    "glm": glm,
}


def _faulted_profile(profile: ProviderProfile) -> ProviderProfile:
    selectors = dict(profile.selectors_by_action)
    selectors["message_box"] = ('[data-revival-fault="message-box"]',)
    selectors["send_button"] = ('[data-revival-fault="send-button"]',)
    return replace(profile, selectors_by_action=selectors)


def _reply_marker(provider_id: str, phase: str) -> str:
    del provider_id, phase
    return f"SESSION_CHECK_{uuid.uuid4().hex[:12]}"


def _send_marker(provider, marker: str, timeout: float) -> tuple[str, float]:
    if "codey" in marker.lower():
        raise ValueError("web verification markers must be product-neutral")
    prompt = (
        "Reply with exactly one JSON object and no markdown: "
        f'{{"nonce":"{marker}"}}'
    )
    started = time.monotonic()
    reply = provider.send(prompt, timeout=timeout)
    elapsed = time.monotonic() - started
    if marker not in reply:
        raise AssertionError(f"reply did not contain marker {marker}: {reply[:300]!r}")
    return reply, elapsed


def _bundle(store: Path, provider_id: str) -> dict:
    data = read_json(store) or {}
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        raise AssertionError(f"no recovery bundle was saved for {provider_id}")
    meta = provider.get("_revival")
    if not isinstance(meta, dict):
        raise AssertionError(f"no revival metadata was saved for {provider_id}")
    return provider


def run_provider_smoke(
    provider_id: str,
    *,
    port: int,
    timeout: float,
    state: server.State,
    store: Path,
) -> dict:
    module = PROVIDER_MODULES[provider_id]
    provider = connect_provider(provider_id, port=port)
    doctor_events: list[dict] = []
    helper_attempts: list[str] = []
    original_doctor = state.handle_profile_doctor
    original_borrow = server.borrow_open_provider
    original_select = provider_controls.discovery.select_control_candidate

    def tracked_borrow(helper_id, owner_page):
        helper_attempts.append(helper_id)
        return original_borrow(helper_id, owner_page)

    def tracked_doctor(request):
        start = len(helper_attempts)
        selected = original_doctor(request)
        doctor_events.append({
            "action": request.action,
            "selected": selected,
            "helpers": helper_attempts[start:],
            "candidate_count": len(request.candidates),
        })
        return selected

    def force_send_button_doctor(candidates, action):
        if action == provider_controls.CONTROL_SEND_BUTTON:
            return None
        return original_select(candidates, action)

    try:
        provider.new_chat(timeout=min(timeout, 90.0))
        faulted = _faulted_profile(module.PROFILE)
        extra_patches = []
        if provider_id == "qwen":
            extra_patches.append(mock.patch.object(qwen, "_qwen_enabled_send_button", return_value=None))

        with (
            mock.patch.object(module, "PROFILE", faulted),
            mock.patch.object(server, "borrow_open_provider", side_effect=tracked_borrow),
            mock.patch.object(
                provider_controls.discovery,
                "select_control_candidate",
                side_effect=force_send_button_doctor,
            ),
        ):
            for patcher in extra_patches:
                patcher.start()
            try:
                provider_controls.set_doctor_handler(tracked_doctor)
                provider_controls.begin_task_context(f"revival-smoke:{provider_id}:first")
                first_marker = _reply_marker(provider_id, "FIRST")
                _, first_elapsed = _send_marker(provider, first_marker, timeout)
                first_bundle = _bundle(store, provider_id)
                first_meta = dict(first_bundle["_revival"])
                if first_meta.get("status") != "provisional":
                    raise AssertionError(f"first recovery was not provisional: {first_meta}")
                if "send_button" not in first_meta.get("changed_actions", []):
                    raise AssertionError(f"send button was not recovered: {first_meta}")
                if not any(
                    event["action"] == "send_button" and event["selected"]
                    for event in doctor_events
                ):
                    raise AssertionError("no sibling selected the injected send button")

                first_doctor_count = len(doctor_events)
                provider_controls.end_task_context()
                provider_controls.begin_task_context(f"revival-smoke:{provider_id}:second")
                second_marker = _reply_marker(provider_id, "SECOND")
                _, second_elapsed = _send_marker(provider, second_marker, timeout)
                second_bundle = _bundle(store, provider_id)
                second_meta = dict(second_bundle["_revival"])
                if second_meta.get("status") != "active":
                    raise AssertionError(f"second recovery did not become active: {second_meta}")
                if len(doctor_events) != first_doctor_count:
                    raise AssertionError("persisted controls unexpectedly invoked Doctor again")
            finally:
                for patcher in reversed(extra_patches):
                    patcher.stop()
    finally:
        provider_controls.end_task_context()
        provider.close()

    return {
        "provider": provider_id,
        "ok": True,
        "first_seconds": round(first_elapsed, 2),
        "second_seconds": round(second_elapsed, 2),
        "doctor_events": doctor_events,
        "helper_attempts": helper_attempts,
        "first_status": first_meta.get("status"),
        "final_status": second_meta.get("status"),
        "actions": second_meta.get("required_actions", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Provider Revival fault injection")
    parser.add_argument("--provider", choices=(*provider_ids(), "all"), default="all")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    selected = provider_ids() if args.provider == "all" else (args.provider,)
    output = args.output or Path(tempfile.gettempdir()) / "codey-provider-revival-smoke.json"
    old_state = server.STATE
    old_store = provider_controls.CONTROL_STORE
    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="codey-revival-state-") as td:
        state_home = Path(td)
        store = state_home / "provider-controls.json"
        state = server.State(state_home)
        try:
            server.STATE = state
            provider_controls.CONTROL_STORE = store
            provider_controls.set_teach_handler(None)
            provider_flow.set_recovery_handler(state.handle_flow_recovery)
            for provider_id in selected:
                print(f"[revival-smoke] {provider_id}: starting", flush=True)
                try:
                    result = run_provider_smoke(
                        provider_id,
                        port=args.port,
                        timeout=args.timeout,
                        state=state,
                        store=store,
                    )
                except Exception as exc:
                    result = {
                        "provider": provider_id,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            provider_controls.end_task_context()
            provider_controls.CONTROL_STORE = old_store
            server.STATE = old_state
            provider_controls.set_teach_handler(old_state.handle_control_teach)
            provider_controls.set_doctor_handler(old_state.handle_profile_doctor)
            provider_flow.set_recovery_handler(old_state.handle_flow_recovery)

    report = {
        "ok": all(item.get("ok") for item in results),
        "port": args.port,
        "results": results,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"[revival-smoke] report: {output}", flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())