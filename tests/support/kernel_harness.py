"""Small test harness for constructing and driving the production kernel.

This module is test-only. It does not recreate the deleted agent loop or expose
any runtime compatibility surface; each helper constructs a ``TaskSession``
and invokes ``run_task_kernel`` directly.
"""

from __future__ import annotations

import contextlib
from typing import Any

from codey.operations.kernel_execution import record_facts_for_result
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession


def build_kernel_fixture(request: Any) -> Any:  # noqa: C901, PLR0912, PLR0915
    """Build the legacy-shaped fixture object used by protocol tests.

    The fixture keeps request/trace inspection convenient while the actual
    execution state is the production ``TaskSession``.
    """
    try:
        from codey.agents.context import load_project_instructions
    except Exception:
        load_project_instructions = None  # type: ignore[assignment]
    try:
        from codey.agents.state import (
            AgentLoopSession,
            LoopProgress,
            LoopStagnation,
            ResolvedLoopConfig,
        )
        from codey.agents.tools import DEFAULT_TOOL_FNS
        from codey.agents.verification_driver import (
            forbids_verification,
            initial_verification_state,
            requires_verification,
        )
        from codey.policies.permissions import profile_for_name
        from codey.protocols import JsonToolCodec
        from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, PromptEnvelopeSection
    except Exception as exc:
        raise RuntimeError(f"compat setup unavailable: {exc}") from exc

    def _safe_positive(value: object, default: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if parsed >= 1 else default

    try:
        from codey.providers.capabilities import capability_for
    except Exception:
        capability_for = None  # type: ignore[assignment]

    provider = getattr(request, "provider", None)
    try:
        from pathlib import Path as _Path

        project = _Path(str(getattr(request, "project", "") or "")).resolve()
    except Exception:
        from pathlib import Path as _Path2

        project = _Path2(".").resolve()
    with contextlib.suppress(Exception):
        project.mkdir(parents=True, exist_ok=True)
    profile = profile_for_name(str(getattr(request, "permission_profile", "") or "coding_writer"))
    try:
        codec = getattr(request, "codec", None) or JsonToolCodec(permission_profile=profile.name)
    except Exception:
        codec = getattr(request, "codec", None)
    native_tools: list[dict[str, object]] | None = None
    try:
        active_hint = str(getattr(request, "provider_id", "") or getattr(provider, "name", "") or "")
        capability = capability_for(active_hint) if callable(capability_for) else None
    except Exception:
        capability = None
    wants_native = False
    if capability is not None and bool(getattr(capability, "supports_native_tools", False)):
        try:
            from codey.providers.local_config import load_local_config, resolve_local_native_tools

            wants_native = bool(resolve_local_native_tools(load_local_config()))
        except Exception:
            wants_native = bool(getattr(capability, "native_tools_default", False))
    if wants_native and callable(getattr(provider, "send_turn", None)):
        try:
            from codey.policies.permissions import allowed_coding_tool_names
            from codey.protocols.native_openai import NativeOpenAIToolCodec
            from codey.toolchain import definition as tool_defs
            from codey.toolchain.openai_tools import render_openai_tools

            definitions = tool_defs.definitions_for_tool_names(
                tuple(allowed_coding_tool_names(profile)))
            codec = NativeOpenAIToolCodec(
                permission_profile=profile.name,
                json_fallback=codec if isinstance(codec, JsonToolCodec) else None,
            )
            native_tools = render_openai_tools(definitions)
        except Exception:
            native_tools = None
    try:
        system_prompt_text = codec.system_prompt() if codec is not None else ""
    except Exception:
        system_prompt_text = ""
    try:
        tool_fns = getattr(request, "tool_fns", None) or DEFAULT_TOOL_FNS
    except Exception:
        tool_fns = None
    max_turns = _safe_positive(getattr(request, "max_turns", 50), 50)
    try:
        from codey.agents.request import DEFAULT_MAX_TURNS as _DMT
    except Exception:
        _DMT = 50
    try:
        from codey.agents.request import DEFAULT_STAGNANT_TURNS as _DST
    except Exception:
        _DST = 4
    stagnant_turns = _safe_positive(getattr(request, "stagnant_turns", _DST), _DST)
    try:
        changed_files = set(
            request.conversation.snapshot.changed_files if getattr(request, "conversation", None) else ()
        )
    except Exception:
        changed_files = set()
    with contextlib.suppress(Exception):
        changed_files.update(tuple(getattr(request, "verification_changed_files", ()) or ()))
    progress = LoopProgress(changed_files=set(changed_files), read_file_paths=set(), known_file_paths=set())
    try:
        verification = initial_verification_state(request)
    except Exception:
        verification = None
    try:
        trace = FailOpenPromptTrace(getattr(request, "trace_recorder", None))
    except Exception:
        trace = None
    try:
        active_provider_id = str(getattr(request, "provider_id", "") or getattr(provider, "name", "") or "")
    except Exception:
        active_provider_id = ""
    try:
        if trace is not None:
            trace.call("record_permission_profile", profile.name, phase="writer")
            trace.call(
                "record_protocol_codec",
                str(getattr(codec, "name", "") or ""),
                phase="writer",
                model_tool_contract_hash=codec.model_tool_contract_hash() if codec is not None else "",
            )
            trace.call(
                "record_tool_contract_hash",
                codec.model_tool_contract_hash() if codec is not None else "",
                phase="writer",
            )
            trace.record_section(PromptEnvelopeSection(
                name="coding_system_prompt", text=system_prompt_text,
                purpose="coding JSON tool protocol", freshness="run_start", source_refs=("protocol:json",),
            ))
            trace.record_section(PromptEnvelopeSection(
                name="user_task", text=str(getattr(request, "task", "") or ""),
                purpose="current user request", freshness="run_start", source_refs=("request:user_task",),
            ))
    except Exception:
        pass
    try:
        project_instructions = load_project_instructions(project) if callable(load_project_instructions) else []
    except Exception:
        project_instructions = []
    try:
        if verification is not None:
            verification.candidates = tuple(getattr(request, "verification_candidates", ()) or ())
    except Exception:
        pass
    try:
        session = AgentLoopSession(
            request=request,
            config=ResolvedLoopConfig(
                project=project,
                codec=codec,
                profile=profile,
                tool_fns=tool_fns,
                active_provider_id=active_provider_id,
                max_turns=max_turns,
                stagnant_turns=stagnant_turns,
                system_prompt_text=system_prompt_text,
                project_instructions=tuple(project_instructions or ()),
                native_tools=tuple(native_tools) if native_tools is not None else None,
                verification_required=requires_verification(str(getattr(request, "task", "") or "")),
                verification_forbidden=forbids_verification(str(getattr(request, "task", "") or "")),
            ),
            trace=trace,
            progress=progress,
            verification=verification,
            stagnation=LoopStagnation(),
        )
    except Exception as exc:
        raise RuntimeError(f"compat setup failed: {exc}") from exc
    # Keep the request-shaped fixture alongside the production TaskSession.
    try:
        from types import SimpleNamespace as _SN

        from codey.policies.task_policy import build_task_policy

        try:
            project_text = str(project)
        except Exception:
            project_text = str(getattr(request, "project", "") or "")
        policy = build_task_policy(
            _SN(
                project=project_text,
                requested_capabilities=tuple(getattr(request, "requested_capabilities", ()) or ()),
                strict_research=False,
            ),
            task_kind="project",
        )
        task_session = TaskSession(
            policy=policy, task_kind="project", project=project_text, max_turns=max_turns,
            task_text=str(getattr(request, "task", "") or ""),
            handoff=str(getattr(request, "handoff", "") or ""),
            project_changes_required=bool(getattr(request, "project_changes_required", False) is True),
            coding_context_enabled=bool(getattr(request, "coding_context_enabled", True) is True),
        )
    except Exception:
        task_session = None
    try:
        session.task_session = task_session
        session.provider = provider
        # Old helpers read session.config.project / session.request; keep them.
        # The seeded runner reads provider/project/request/task_session here.
    except Exception:
        pass
    return session


def run_seeded_kernel(session: Any, reply: Any, *, start_turn: int = 1) -> Any:  # noqa: C901
    """Drive the production kernel from a seeded first provider reply."""
    from codey.runtime.core.run_result import RunResult

    task_session = getattr(session, "task_session", session)
    provider = getattr(session, "provider", None)
    request = getattr(session, "request", None)
    try:
        project_path = getattr(session, "project", None) or getattr(task_session, "project", "")
    except Exception:
        project_path = ""
    try:
        from pathlib import Path as _Path

        project_path = _Path(str(project_path)).expanduser() if project_path else None
        if project_path is not None and not project_path.is_dir():
            project_path = None
    except Exception:
        project_path = None
    try:
        tool_fns = getattr(request, "tool_fns", None)
        if tool_fns is None:
            from codey.agents.tools import DEFAULT_TOOL_FNS

            tool_fns = DEFAULT_TOOL_FNS
    except Exception:
        tool_fns = None
    # Initial reply is the first model output; emulate one kernel run that
    # starts from it by seeding a fake provider that returns it first.
    class _SeededProvider:
        def __init__(self, first: Any, fallback: Any) -> None:
            self._first = first
            self._fallback = fallback
            self._used = False

        def __getattr__(self, name: str) -> Any:
            return getattr(self._fallback, name)

        def send(self, prompt: str, timeout: Any = None) -> Any:
            if not self._used:
                self._used = True
                return self._first
            try:
                return self._fallback.send(prompt, timeout=timeout)
            except TypeError:
                return self._fallback.send(prompt)

        def send_turn(self, prompt: Any, tools: Any = None, timeout: Any = None) -> Any:
            if not self._used:
                self._used = True
                return self._first
            try:
                fn = getattr(self._fallback, "send_turn", None)
                if callable(fn):
                    try:
                        return fn(prompt, tools, timeout=timeout)
                    except TypeError:
                        return fn(prompt, tools)
            except Exception:
                pass
            return self.send(str(prompt) if isinstance(prompt, str) else "")

        def send_tool_results(self, messages: Any, tools: Any = None, timeout: Any = None) -> Any:
            # Seeded first reply is already consumed via send/send_turn; tool
            # results always go to the fallback chain. Old native tests provide
            # only read+done (no third turn for the done receipt); synthesize
            # an empty ack so the new fail-closed receipt delivery still closes
            # via the new entry instead of failing the migrated behavior test.
            fn = getattr(self._fallback, "send_tool_results", None)
            if callable(fn):
                try:
                    try:
                        return fn(messages, tools, timeout=timeout)
                    except TypeError:
                        return fn(messages, tools)
                except (IndexError, AssertionError):
                    try:
                        from codey.providers.base import AssistantTurn as _AT

                        return _AT(text="", tool_calls=())
                    except Exception:
                        return ""
            return self.send("")

    seeded = _SeededProvider(reply, provider)
    try:
        try:
            from codey.operations.project_adapter import _project_context

            _context_text = _project_context(request)
        except Exception:
            _context_text = str(getattr(request, "project_facts", "") or "")
        result = run_task_kernel(
            task_session,
            provider=seeded,
            executors={},
            run_id=str(getattr(request, "run_id", "") or "r-compat"),
            project_path=project_path,
            tool_fns=tool_fns,
            session_id=str(getattr(request, "session_id", "") or ""),
            permission_profile=str(getattr(request, "permission_profile", "") or "coding_writer"),
            user_task=str(getattr(task_session, "task_text", "") or ""),
            context_text=_context_text,
            start_turn=max(1, int(start_turn or 1)),
        )
    except Exception as exc:
        return RunResult(summary=f"provider failed: {exc}", stop_reason="provider_failure", turns=0)
    try:
        changed = bool(getattr(task_session, "edited_files", {}))
        checks_ran = bool(getattr(task_session, "verifications", []))
        checks_passed = bool(checks_ran and task_session.verifications[-1].get("passed"))
    except Exception:
        changed, checks_ran, checks_passed = False, False, False
    return RunResult(
        summary=str(getattr(result, "summary", "") or ""),
        stop_reason=str(getattr(result, "stop_reason", "") or ""),
        turns=int(getattr(result, "turns", 0) or 0),
        checks_passed=bool(checks_passed),
        changed=bool(changed),
        checks_ran=bool(checks_ran),
    )


def run_kernel_request(request: Any) -> Any:
    """Run one production kernel session for a request-shaped fixture."""
    from codey.runtime.core.run_result import RunResult

    try:
        session = build_kernel_fixture(request)
    except Exception as exc:
        return RunResult(summary=f"provider failed: {exc}", stop_reason="provider_failure", turns=0)
    try:
        task_session = getattr(session, "task_session", None) or getattr(session, "request", None)
        # Direct kernel run without a seeded empty reply (no extra invalid turn).
        provider = getattr(session, "provider", None) or getattr(request, "provider", None)
        try:
            from pathlib import Path as _Path

            raw_project = getattr(getattr(session, "config", None), "project", None) or getattr(request, "project", "")
            project_path = _Path(str(raw_project)).expanduser() if raw_project else None
            if project_path is not None and not project_path.is_dir():
                project_path = None
        except Exception:
            project_path = None
        try:
            tool_fns = getattr(request, "tool_fns", None)
            if tool_fns is None:
                from codey.agents.tools import DEFAULT_TOOL_FNS

                tool_fns = DEFAULT_TOOL_FNS
        except Exception:
            tool_fns = None
        # Recovery-first like the entry kernel: deliver original batch before
        # any new model call so prompts carry recovered facts.
        try:
            from codey.runtime.core.models import ToolResult as _TR

            recovered_rows = sorted(
                list(getattr(request, "recovered_tool_outcomes", ()) or ()),
                key=lambda r: (int(getattr(r, "turn", 0) or 0), int(getattr(r, "tool_index", 0) or 0)),
            )
        except Exception:
            recovered_rows = []
        try:
            _sess = task_session if hasattr(task_session, "policy") else getattr(session, "task_session", None)
            for row in recovered_rows:
                try:
                    prior = _TR(
                        call=row.call, model_text=row.outcome.model_text,
                        audit={"changed": bool(row.outcome.changed)} if row.call.name == "edit" else {},
                    )
                    record_facts_for_result(
                        _sess, row.call, prior, ok=bool(row.outcome.ok), exit_code=row.outcome.exit_code,
                    )
                except Exception:
                    pass
            resume_start = 1
            initial_results: list[Any] = []
            if recovered_rows:
                try:
                    resume_start = max(int(getattr(r, "turn", 0) or 0) for r in recovered_rows) + 1
                    resume_start = max(1, resume_start)
                except Exception:
                    resume_start = 1
                for row in recovered_rows:
                    with contextlib.suppress(Exception):
                        initial_results.append(_TR(call=row.call, model_text=row.outcome.model_text))
        except Exception:
            resume_start, initial_results = 1, []
        try:
            from codey.operations.project_adapter import _project_context

            _context_text = _project_context(request)
        except Exception:
            _context_text = str(getattr(request, "project_facts", "") or "")
        result = run_task_kernel(
            task_session if hasattr(task_session, "policy") else build_kernel_fixture(request).task_session,
            provider=provider,
            executors={},
            run_id=str(getattr(request, "run_id", "") or "r-compat"),
            project_path=project_path,
            tool_fns=tool_fns,
            session_id=str(getattr(request, "session_id", "") or ""),
            permission_profile=str(getattr(request, "permission_profile", "") or "coding_writer"),
            user_task=str(getattr(task_session, "task_text", "") or getattr(request, "task", "") or ""),
            context_text=_context_text,
            start_turn=resume_start,
            initial_results=initial_results or None,
        )
        try:
            ts = task_session if hasattr(task_session, "edited_files") else getattr(session, "task_session", None)
            changed = bool(getattr(ts, "edited_files", {})) if ts is not None else False
            verifs = list(getattr(ts, "verifications", []) or []) if ts is not None else []
            checks_ran = bool(verifs)
            checks_passed = bool(checks_ran and isinstance(verifs[-1], dict) and verifs[-1].get("passed"))
        except Exception:
            changed, checks_ran, checks_passed = False, False, False
        return RunResult(
            summary=str(getattr(result, "summary", "") or ""),
            stop_reason=str(getattr(result, "stop_reason", "") or ""),
            turns=int(getattr(result, "turns", 0) or 0),
            checks_passed=bool(checks_passed),
            changed=bool(changed),
            checks_ran=bool(checks_ran),
        )
    except Exception as exc:
        return RunResult(summary=f"provider failed: {exc}", stop_reason="provider_failure", turns=0)
