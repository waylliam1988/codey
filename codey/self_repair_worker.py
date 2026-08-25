"""Subprocess entry point for Provider adapter self-repair.

The main Codey process must not run model-assisted adapter repair on the shared
BrowserWorker. This worker owns its Playwright calls in a separate process and
returns one bounded JSON result to the parent.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from codey import cancellation
from codey.adapter_repair import AdapterRepairResult, run_adapter_repair, run_worker_canary
from codey.browser import DEFAULT_PORT
from codey.provider_diagnostics import sanitize_failure_facts
from codey.providers.registry import PROVIDER_TYPES, PROVIDER_WORKER_PORT_OFFSETS
from codey import provider_controls, provider_flow
from codey.self_repair import SelfRepairJob


DEFAULT_REPAIR_TIMEOUT = 900.0
DEFAULT_MODEL_TIMEOUT = 300.0
MAX_WORKER_OUTPUT = 32 * 1024


def run_self_repair_worker(
    job: SelfRepairJob,
    *,
    helper_ids: tuple[str, ...],
    state_home: str | Path,
    source_root: str | Path,
    timeout: float = DEFAULT_REPAIR_TIMEOUT,
) -> AdapterRepairResult:
    """Run one self-repair job in an isolated Python process."""
    command = [
        sys.executable,
        "-B",
        "-m",
        "codey.self_repair_worker",
        "--provider",
        job.provider_id,
        "--failure-kind",
        job.failure_kind,
        "--failure-stage",
        job.failure_stage,
        "--failure-facts-json",
        json.dumps(job.failure_facts, ensure_ascii=False, separators=(",", ":")),
        "--state-home",
        str(state_home),
        "--source-root",
        str(source_root),
    ]
    for helper_id in helper_ids[:3]:
        command.extend(["--helper", helper_id])
    env = dict(os.environ)
    source_root = Path(source_root).resolve()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(source_root) + (os.pathsep + existing if existing else "")
    try:
        proc = cancellation.run_process(
            command,
            cwd=str(source_root),
            env=env,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, cancellation.DeadlineExceeded):
        return AdapterRepairResult(False, job.provider_id, error="self-repair worker timed out")
    except cancellation.TaskCancelled:
        return AdapterRepairResult(False, job.provider_id, error="self-repair worker cancelled")
    except OSError as exc:
        return AdapterRepairResult(False, job.provider_id, error=str(exc))
    return _parse_worker_result(job.provider_id, proc.stdout, proc.stderr, proc.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--failure-kind", default="")
    parser.add_argument("--failure-stage", default="")
    parser.add_argument("--failure-facts-json", default="{}")
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--helper", action="append", default=[])
    parser.add_argument("--model-timeout", type=float, default=DEFAULT_MODEL_TIMEOUT)
    args = parser.parse_args(argv)
    result = _run_worker_job(
        provider_id=args.provider,
        failure_kind=str(args.failure_kind or ""),
        failure_stage=str(args.failure_stage or ""),
        failure_facts=_failure_facts_from_json(args.failure_facts_json),
        helper_ids=tuple(str(item) for item in args.helper),
        state_home=Path(args.state_home),
        source_root=Path(args.source_root),
        model_timeout=float(args.model_timeout),
    )
    print(json.dumps(_result_payload(result), ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0 if result.ok else 1


def _run_worker_job(
    *,
    provider_id: str,
    failure_kind: str = "",
    failure_stage: str = "",
    failure_facts: dict[str, object] | None = None,
    helper_ids: tuple[str, ...],
    state_home: Path,
    source_root: Path,
    model_timeout: float,
) -> AdapterRepairResult:
    last_error = "no repair model available"
    for helper_id in helper_ids[:3]:
        helper = None
        try:
            helper = connect_repair_helper(helper_id, state_home=state_home)
            with provider_controls.suppress_assistance(), provider_flow.suppress_assistance():
                helper.new_chat(timeout=model_timeout)
                result = run_adapter_repair(
                    provider_id,
                    send_prompt=lambda prompt, provider=helper: provider.send(
                        prompt,
                        timeout=model_timeout,
                    ),
                    state_home=state_home,
                    source_root=source_root,
                    failure_kind=failure_kind,
                    failure_stage=failure_stage,
                    failure_facts=failure_facts,
                    run_canary=lambda override: run_worker_canary(
                        provider_id,
                        override,
                        state_home=state_home,
                    ),
                )
                if result.ok:
                    return result
                last_error = result.error or "repair candidate did not pass validation"
        except Exception as exc:
            last_error = str(exc)
        finally:
            if helper is not None:
                try:
                    helper.close()
                except Exception:
                    pass
    return AdapterRepairResult(False, provider_id, error=last_error)


def _failure_facts_from_json(value: str) -> dict[str, object]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return sanitize_failure_facts(decoded)


def connect_repair_helper(provider_id: str, *, state_home: str | Path):
    """Open a fresh helper tab in an isolated self-repair profile.

    The helper must never attach to the user's default browser profile from
    a second CDP port (profile lock / session corruption); it gets a
    dedicated profile directory per provider, like override workers do.
    """
    normalized = str(provider_id or "").strip().lower()
    provider_type = PROVIDER_TYPES.get(normalized)
    if provider_type is None:
        raise ValueError(f"unsupported repair helper provider: {provider_id}")
    return provider_type.connect(
        port=DEFAULT_PORT + 200 + PROVIDER_WORKER_PORT_OFFSETS.get(normalized, 100),
        profile=Path(state_home) / "self-repair" / normalized,
        open_if_missing=True,
        bring_to_front=False,
        isolated=False,
        fresh_tab=True,
    )


def _parse_worker_result(provider_id: str, stdout: str, stderr: str, returncode: int) -> AdapterRepairResult:
    text = (stdout or "").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    if lines:
        try:
            payload = json.loads(lines[-1])
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return AdapterRepairResult(
                ok=bool(payload.get("ok")),
                provider_id=str(payload.get("provider_id") or provider_id),
                generation=max(0, int(payload.get("generation") or 0)),
                error=str(payload.get("error") or ""),
                changed_files=tuple(str(item) for item in payload.get("changed_files") or ()),
            )
    output = ((stderr or "") + "\n" + (stdout or "")).strip()[:MAX_WORKER_OUTPUT]
    error = output or f"self-repair worker exited with code {returncode}"
    return AdapterRepairResult(False, provider_id, error=error)


def _result_payload(result: AdapterRepairResult) -> dict:
    return {
        "ok": result.ok,
        "provider_id": result.provider_id,
        "generation": result.generation,
        "error": result.error,
        "changed_files": list(result.changed_files),
    }


if __name__ == "__main__":
    raise SystemExit(main())
