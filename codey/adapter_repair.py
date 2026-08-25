"""Repair Provider adapter code inside a sandbox, then install an override."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from codey import adapter_overrides
from codey.adapter_overrides import AdapterOverride
from codey.local_store import DEFAULT_STATE_HOME
from codey.provider_diagnostics import FAILURE_READINESS_STALE, sanitize_failure_facts
from codey.provider_worker import WorkerChatProvider
from codey.repair_journal import RepairJournal
from codey.repair_policy import (
    IMPACT_PROFILE_DATA,
    IMPACT_SHARED_WEB_SURFACE,
    allowed_adapter_files,
    readonly_reference_files,
    validate_candidate,
)
from codey.repair_sandbox import create_repair_sandbox


RepairModel = Callable[[str], str]


@dataclass(frozen=True)
class AdapterRepairResult:
    ok: bool
    provider_id: str
    generation: int = 0
    error: str = ""
    changed_files: tuple[str, ...] = ()


def run_adapter_repair(
    provider_id: str,
    *,
    send_prompt: RepairModel,
    state_home: str | Path | None = None,
    source_root: str | Path | None = None,
    journal: RepairJournal | None = None,
    run_canary: Callable[[AdapterOverride], bool] | None = None,
    failure_kind: str = "",
    failure_stage: str = "",
    failure_facts: dict[str, object] | None = None,
) -> AdapterRepairResult:
    journal = journal or RepairJournal(state_home)
    sandbox = create_repair_sandbox(source_root)
    try:
        prompt = _render_repair_prompt(
            provider_id,
            sandbox.baseline_root,
            failure_kind=failure_kind,
            failure_stage=failure_stage,
            failure_facts=failure_facts,
        )
        reply = send_prompt(prompt)
        _apply_model_reply(provider_id, sandbox.candidate_root, reply)
        policy = validate_candidate(provider_id, sandbox.baseline_root, sandbox.candidate_root)
        if not policy.ok:
            error = "; ".join(policy.errors)
            journal.append("adapter_repair_rejected", provider=provider_id, error=error)
            return AdapterRepairResult(False, provider_id, error=error, changed_files=policy.changed_files)
        tests = _run_static_checks(provider_id, sandbox.candidate_root, impact=policy.impact)
        failed = tuple(item for item in tests if item.startswith("failed:"))
        if failed:
            error = "; ".join(failed)
            journal.append("adapter_repair_failed_checks", provider=provider_id, error=error)
            return AdapterRepairResult(False, provider_id, error=error, changed_files=policy.changed_files)
        override = adapter_overrides.install_candidate(
            provider_id,
            sandbox.candidate_root,
            state_home=state_home,
            base_hash=adapter_overrides.adapter_base_hash(provider_id, sandbox.baseline_root),
            tests=tests,
        )
        if run_canary is None or not run_canary(override):
            error = "candidate worker canary did not pass"
            journal.append(
                "adapter_repair_canary_failed",
                provider=provider_id,
                generation=override.generation,
            )
            return AdapterRepairResult(
                False,
                provider_id,
                generation=override.generation,
                error=error,
                changed_files=policy.changed_files,
            )
        adapter_overrides.mark_provisional(provider_id, override.generation, state_home=state_home)
        journal.append(
            "adapter_repair_installed",
            provider=provider_id,
            generation=override.generation,
            changed_files=policy.changed_files,
            tests=tests,
        )
        return AdapterRepairResult(
            True,
            provider_id,
            generation=override.generation,
            changed_files=policy.changed_files,
        )
    except Exception as exc:
        journal.append("adapter_repair_error", provider=provider_id, error=str(exc))
        return AdapterRepairResult(False, provider_id, error=str(exc))
    finally:
        sandbox.cleanup()


def _render_repair_prompt(
    provider_id: str,
    root: Path,
    *,
    failure_kind: str = "",
    failure_stage: str = "",
    failure_facts: dict[str, object] | None = None,
) -> str:
    files = []
    for rel in (*allowed_adapter_files(provider_id), *readonly_reference_files(provider_id)):
        path = root / rel
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = ""
        files.append(f"--- {rel} ---\n{content[:20_000]}")
    allowed = ", ".join(allowed_adapter_files(provider_id))
    readonly = ", ".join(readonly_reference_files(provider_id))
    example_path = (allowed_adapter_files(provider_id) or ("codey/provider.py",))[0]
    failure_context = _render_failure_context(failure_kind, failure_stage, failure_facts)
    return (
        "Repair this Codey web provider adapter in a temporary sandbox.\n"
        f"Provider: {provider_id}\n"
        "You may modify only the web adapter surface listed below:\n"
        f"{allowed}\n"
        "This repair runs in a provider-scoped override sandbox.\n"
        "Do not modify tests or Codey core runtime.\n"
        f"Tests are read-only references and must not be modified: {readonly}\n"
        "Return one JSON object only, with this shape:\n"
        f'{{"files":[{{"path":"{example_path}","content":"full replacement text"}}]}}\n'
        "Do not add dependencies, subprocess calls, network calls, file writes, "
        "eval/exec, or changes outside the allowed adapter files.\n\n"
        + failure_context
        + "\n\n".join(files)
    )


def _render_failure_context(
    failure_kind: str,
    failure_stage: str,
    failure_facts: dict[str, object] | None,
) -> str:
    kind = str(failure_kind or "").strip()[:80]
    stage = str(failure_stage or "").strip()[:80]
    facts = sanitize_failure_facts(failure_facts)
    if not kind and not stage and not facts:
        return ""
    lines = ["Observed failure:"]
    if kind:
        lines.append(f"kind: {kind}")
    if stage:
        lines.append(f"stage: {stage}")
    if facts:
        lines.append("facts:")
        for key, value in sorted(facts.items()):
            lines.append(f"- {key}={_prompt_fact_value(value)}")
    if kind == FAILURE_READINESS_STALE:
        lines.append(
            "If visible DOM controls prove the page is usable, prefer DOM "
            "readiness over brittle internal bootstrap resources."
        )
    return "\n".join(lines) + "\n\n"


def _prompt_fact_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _apply_model_reply(provider_id: str, root: Path, reply: str) -> None:
    data = json.loads(reply)
    files = data.get("files")
    if not isinstance(files, list):
        raise ValueError("repair reply missing files list")
    allowed = set(allowed_adapter_files(provider_id))
    for item in files:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").replace("\\", "/")
        content = item.get("content")
        if rel not in allowed:
            raise ValueError(f"repair attempted to modify disallowed file: {rel}")
        if not isinstance(content, str):
            raise ValueError(f"repair content missing for {rel}")
        path = (root / rel).resolve()
        root_resolved = root.resolve()
        if root_resolved not in path.parents:
            raise ValueError(f"repair path escapes sandbox: {rel}")
        path.write_text(content, encoding="utf-8")


_WEB_ADAPTER_IMPORT_SCRIPT = (
    "from codey.providers.web_provider import WEB_PROVIDER_CLASSES\n"
    "assert WEB_PROVIDER_CLASSES\n"
)
_PROFILES_SCHEMA_LOAD_SCRIPT = (
    "from pathlib import Path\n"
    "from codey.provider_profiles import load_profiles\n"
    "load_profiles(Path('codey/provider_profiles.json'))\n"
)


def _run_static_checks(
    provider_id: str,
    root: Path,
    impact: tuple[str, ...] = (),
) -> tuple[str, ...]:
    commands: list[tuple[str, list[str]]] = []
    python_files = [
        rel for rel in allowed_adapter_files(provider_id) if rel.endswith(".py")
    ]
    if python_files:
        commands.append(("py_compile", [sys.executable, "-B", "-m", "py_compile", *python_files]))
        commands.append(("ruff", [sys.executable, "-m", "ruff", "check", *python_files]))
    test_module = f"tests.test_{provider_id}"
    commands.append(("provider_unittest", [sys.executable, "-B", "-m", "unittest", test_module]))
    # Escalate validation with the candidate's blast radius: a shared-surface
    # edit must still import the whole web adapter layer, and a profile-data
    # edit must still load through the schema.
    if IMPACT_SHARED_WEB_SURFACE in impact:
        commands.append(("web_adapter_import", [sys.executable, "-B", "-c", _WEB_ADAPTER_IMPORT_SCRIPT]))
    if IMPACT_PROFILE_DATA in impact:
        commands.append(("profiles_schema_load", [sys.executable, "-B", "-c", _PROFILES_SCHEMA_LOAD_SCRIPT]))
    results: list[str] = []
    for name, command in commands:
        try:
            proc = subprocess.run(
                command,
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            results.append(f"failed:{name}:{exc}")
            continue
        if proc.returncode == 0:
            results.append(f"passed:{name}")
        else:
            output = (proc.stderr or proc.stdout or "").strip().splitlines()[:3]
            results.append(f"failed:{name}:{' | '.join(output)}")
    return tuple(results)


def run_worker_canary(
    provider_id: str,
    override: AdapterOverride,
    *,
    state_home: str | Path | None = None,
    attempts: int = 2,
    timeout: float = 45.0,
) -> bool:
    provider = WorkerChatProvider(
        provider_id,
        override,
        state_home=Path(state_home) if state_home is not None else DEFAULT_STATE_HOME,
    )
    try:
        for _ in range(max(1, attempts)):
            marker = "SESSION_CHECK_" + secrets.token_hex(8).upper()
            provider.new_chat(timeout=timeout)
            reply = provider.send(
                f"Return exactly this marker and nothing else: {marker}",
                timeout=timeout,
            )
            if str(reply or "").strip() != marker:
                return False
        return True
    finally:
        provider.close()
