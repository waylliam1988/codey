"""Real builder/kernel/API HTTP boundary; synthetic inputs, no model required."""
from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.agents.request import AgentRequest
from codey.app.context import AppContext
from codey.ghost.directive import GhostDirective
from codey.ghost.payload_types import mapping_fields
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelResult,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.project_adapter import _project_context
from codey.operations.provider_session import adapt_provider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.runs.trace import RunTraceRecorder, RunTraceStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.utils.refs import digest_text
from codey.workspace.context_admission import GhostContextSource, request_scope, sources_from_values
from codey.workspace.context_epoch import context_epoch_id
from tests.durable_runtime import accept_writer, open_runtime
from tests.test_ghost_experience_role_budget import Endpoint, _execute
from tests.test_ghost_experience_role_budget import endpoint as endpoint


def run_test_kernel(root: Path, session: TaskSession, request: KernelRunRequest, *, session_id: str) -> KernelResult:
    durable, line = open_runtime(root, session_id=session_id, run_id="admission")
    with closing(durable):
        accept_writer(durable, line, session_id=session_id, run_id="admission")
        sink = KernelEffectSink(line, session_id=session_id, run_id="admission", provider_id="local",
            durable=durable, managed_outputs=ManagedOutputStore(root / "outputs"))
        return run_task_kernel(session, request=replace(request,
            transport=replace(request.transport, run_id="admission", provider_id="local"),
            execution=replace(request.execution, intent_sink=sink),
            observation=replace(request.observation, on_boundary=durable_generation_boundary_callback(
                durable=durable, session_id=session_id, task_id="admission", sink=sink))))


@pytest.mark.parametrize("native", [False, True], ids=["text", "native"])
@pytest.mark.parametrize("fault", [
    "none", "off", "scope", "global_first_scope", "global_then_project", "session_scope", "budget", "expiry",
    "invalid_expiry", "missing_bytes", "disabled", "duplicate_key", "trace_failure", "scope_trace_failure", "empty_scope",
])
def test_builder_kernel_api_wire_admission(tmp_path: Path, endpoint: Endpoint, native: bool,
                                         fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = endpoint.payloads
    provider = ApiProvider(endpoint.url, "synthetic-admission", native_tools=native, timeout=5,
                           temperature=0, output_tokens=128)
    request = AgentRequest(provider=provider, project=tmp_path, task="Say done.",
                           session_id="session-a", ghost_directive="Prefer Chinese answers.",
                           permission_profile="planning_readonly", admit_hints=fault != "off")
    context = _project_context(request)
    # This matrix tests admission of synthetic sources, not the adapter's
    # separate durable-preference authority (covered by portable-context tests).
    sources = sources_from_values(directive=request.ghost_directive,
        scope=request_scope(tmp_path, "session-a")) if request.admit_hints else ()
    if fault in {"scope", "global_first_scope", "scope_trace_failure"}:
        sources = (replace(sources[0], scope=request_scope("project-other")),)
        if fault == "global_first_scope":
            sources = (replace(sources[0], key="global", scope="global"), *sources)
    elif fault == "global_then_project":
        sources = (replace(sources[0], key="global", scope="global"), *sources)
    elif fault == "session_scope":
        sources = (replace(sources[0], scope=request_scope(session_id="session-other")),)
    elif fault == "budget":
        sources = (replace(sources[0], budget=1),)
    elif fault in {"expiry", "invalid_expiry"}:
        sources = (replace(sources[0], expires_at="2000-01-01T00:00:00Z" if fault == "expiry" else "invalid"),)
    elif fault == "missing_bytes":
        context = "No candidate bytes."
    elif fault == "duplicate_key":
        sources = (*sources, sources[0])

    admissions: list[tuple[tuple[GhostContextSource, ...], dict[str, str]]] = []
    boundaries: list[Mapping[str, object]] = []
    recorder = RunTraceStore(tmp_path / "trace").open(
        run_id="scope-capture", session_id="session-a", project=tmp_path,
        mode_initial="planning", provider_initial="local",
    )

    def record_sources(rows: tuple[GhostContextSource, ...], *, epoch_id: str = "",
                       admission_reason: str = "") -> None:
        if "trace_failure" in fault:
            raise RuntimeError("trace sink unavailable")
        admissions.append((rows, {"epoch_id": epoch_id, "admission_reason": admission_reason}))
        RunTraceRecorder.record_context_sources(recorder, rows, epoch_id=epoch_id, admission_reason=admission_reason)

    def record_boundary(row: Mapping[str, object], surface_payload: Mapping[str, object] | None = None) -> None:
        if "trace_failure" in fault:
            raise RuntimeError("trace sink unavailable")
        boundaries.append(row)
        RunTraceRecorder.record_provider_prompt_boundary(recorder, row, surface_payload)

    monkeypatch.setattr(recorder, "record_context_sources", record_sources)
    monkeypatch.setattr(recorder, "record_provider_prompt_boundary", record_boundary)
    outcome = run_test_kernel(
        tmp_path / "runtime",
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1),
        session_id="session-a", request=KernelRunRequest(
            transport=KernelTransportDeps(provider=adapt_provider(provider), user_task=request.task, context_text=context,
                                          context_scope="" if fault == "empty_scope" else request_scope(tmp_path, "session-a"),
                                          ghost_sources=sources, admit_hints=fault != "disabled"),
            execution=KernelExecutionDeps(project_path=tmp_path),
            observation=KernelObservationDeps(trace_recorder=recorder),
        ),
    )
    allowed = fault in {"none", "off", "global_then_project", "trace_failure"}
    assert len(payloads) == int(allowed), (fault, outcome)
    assert outcome.stop_reason == ("done" if allowed else "provider_failure"), outcome
    if allowed:
        messages = payloads[0]["messages"]
        assert isinstance(messages, list)
        entries: list[object] = messages
        outbound = next(text for message in entries if mapping_fields(message)
                        and isinstance(text := message.get("content"), str) and "User task (verbatim):" in text)
        assert (request.ghost_directive in outbound) == (fault != "off")
        assert context in outbound
        if fault != "trace_failure":
            assert len(boundaries) == 1
            provider_prompt = boundaries[0]["text"]
            assert isinstance(provider_prompt, str)
            assert outbound.startswith(provider_prompt)
            assert boundaries[0]["epoch_id"] == context_epoch_id(provider_prompt)
            if sources:
                assert admissions == [(sources, {"epoch_id": context_epoch_id(provider_prompt),
                                                 "admission_reason": "provider_turn_boundary"})]
                for source in sources:
                    section = next(row for row in recorder.manifest.prompt_sections if row.name == source.key)
                    assert section.digest == digest_text(source.text)
                    assert section.scope == source.scope
                    assert section.budget == source.budget
                    assert section.epoch_id == context_epoch_id(provider_prompt)
                    assert section.source_refs == (f"context_source:{source.key}",)
            else:
                assert admissions == []
            outbound_section = next(row for row in recorder.manifest.prompt_sections
                                    if row.name == "workspace_outbound_prompt")
            assert outbound_section.digest == digest_text(provider_prompt)
            assert outbound_section.epoch_id == context_epoch_id(provider_prompt)
            assert request.ghost_directive not in repr(recorder.manifest.to_payload())
    else:
        assert admissions == []
        assert boundaries == []
        expected_reason = {
            "scope": "scope_mismatch", "scope_trace_failure": "scope_mismatch",
            "global_first_scope": "scope_mismatch", "session_scope": "scope_mismatch",
            "budget": "over_budget", "expiry": "expired", "invalid_expiry": "invalid_expiry",
            "missing_bytes": "not_in_provider_payload", "disabled": "hints_disabled",
            "duplicate_key": "invalid_or_duplicate_key", "empty_scope": "missing_request_scope",
        }[fault]
        assert f":{expected_reason}" in outcome.summary


def test_session_scope_uses_request_session_not_candidate(tmp_path: Path) -> None:
    sent: list[str] = []

    class NoSend:
        def send(self, prompt: str) -> str:
            sent.append(prompt)
            return ""

    sources = sources_from_values(directive="Synthetic preference", scope=request_scope(session_id="other"))
    result = run_test_kernel(
        tmp_path,
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1),
        session_id="current", request=KernelRunRequest(
            transport=KernelTransportDeps(provider=adapt_provider(NoSend()),
                                          context_scope=request_scope(session_id="current"),
                                          context_text=sources[0].text, ghost_sources=sources),
            execution=KernelExecutionDeps(),
        ),
    )
    assert result.stop_reason == "controller_failure"
    assert sent == []


@pytest.mark.parametrize("kind", ["project", "session"])
def test_scope_identity_does_not_alias_long_shared_prefixes(kind: str) -> None:
    prefix = "shared-" + "x" * 160
    def scope(suffix: str) -> str:
        return request_scope(prefix + suffix) if kind == "project" else request_scope(session_id=prefix + suffix)

    assert scope("a") != scope("b")
    assert scope("a") == scope("a")
    assert len(scope("a")) <= 160


@pytest.mark.parametrize("control", ["enabled", "disabled", "scoped_error"])
def test_scoped_hint_control_reaches_entry_api_http(tmp_path: Path, endpoint: Endpoint,
                                                   monkeypatch: pytest.MonkeyPatch, control: str) -> None:
    payloads = endpoint.payloads
    hint = "Synthetic reviewed preference"

    def directive(state: object, *, session_id: str, project: str, provider: object = None) -> GhostDirective:
        return GhostDirective(text=hint)

    monkeypatch.setattr("codey.operations.ghost_context.ghost_directive", directive)
    calls: list[dict[str, str]] = []

    def read(name: str, **scope: str) -> bool:
        if name == "admit_hints":
            calls.append(scope)
            if control == "scoped_error" and scope:
                raise TypeError("scoped read failed")
            return control != "disabled"
        return True

    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        assert state.ghost_inbox is not None
        monkeypatch.setattr(state.ghost_inbox, "control_enabled", read)
        event = _execute(state, endpoint, task="Hello", session="scope-session", identity="scope-run")
    assert event["stop_reason"] == "done", event
    assert len(payloads) == 1
    assert (hint in json.dumps(payloads[0])) == (control == "enabled")
    # The entry and personal-context supplement each check the same scope,
    # including when admission is disabled or a scoped read fails.
    assert calls == [{"project": "", "session_id": "scope-session"}] * 2
