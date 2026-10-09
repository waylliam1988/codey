# ruff: noqa: E402 -- direct execution adds the repository before manual imports.
"""Run paired, loopback-only Codey/Pi coding-agent stability experiments.

The agents keep their production prompts, tools, completion and recovery paths.
Sampling is set before provider admission; the proxy only observes or injects an
explicit recorded fault. Results are journaled continuously and never overwritten.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tests.manual.agent_stability_cases import (
    TASK_CASES,
    ExperimentCase,
    execution_rows,
)
from tests.manual.agent_stability_cases import (
    fixture as _fixture,
)
from tests.manual.agent_stability_cases import (
    run_verification as _run_verification,
)
from tests.manual.agent_stability_cases import (
    snapshot_files as _snapshot_files,
)
from tests.manual.agent_stability_measurements import (
    ENDS,
    STARTS,
    paired_summary,
    row_id,
    stored_output_was_read,
    usage_totals,
)
from tests.manual.agent_stability_measurements import (
    event_rows as _event_rows,
)
from tests.manual.agent_stability_measurements import (
    metrics as _metrics,
)
from tests.manual.agent_stability_proxy import Proxy as _Proxy
from tests.manual.agent_stability_proxy import local_url

MODEL_ID = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"


def _json_line(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _new_project_root():
    return Path(tempfile.mkdtemp(prefix="codey-pi-stability-")).resolve()


def _write_pi_config(config_dir, base_url, model_id, *, max_tokens=2048, window=32768, keep=12000):
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"providers": {"kobold": {"baseUrl": base_url.rstrip("/") + "/v1", "apiKey": "local",
        "api": "openai-completions", "models": [{"id": model_id, "name": model_id, "reasoning": False,
        "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": window, "maxTokens": max_tokens, "compat": {"maxTokensField": "max_tokens",
        "supportsStore": False, "supportsDeveloperRole": False, "supportsReasoningEffort": False,
        "supportsUsageInStreaming": True, "supportsStrictMode": False}}]}}}
    (config_dir / "models.json").write_text(_json_line(payload), encoding="utf-8")
    (config_dir / "settings.json").write_text(_json_line({"cacheWarming": "off", "compaction": {
        "enabled": True, "reserveTokens": max_tokens, "keepRecentTokens": keep}}), encoding="utf-8")


def _pid_alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _spawn(command, root, env):
    # The watchdog owns the entire experiment process tree. This is separate
    # from each agent's own production cancellation, which is scored first.
    from codey.runtime.core.cancellation import _resume_windows_process, attach_process_tree
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x4} if os.name == "nt" else {
        "start_new_session": True}
    proc = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **flags)
    owner = attach_process_tree(proc)
    if os.name == "nt":
        _resume_windows_process(proc)
    return proc, owner


def _run_process(command, root, env, directory, *, deadline, control="", rpc_task=None):
    from codey.runtime.core.cancellation import terminate_process_tree
    proc, owner = _spawn(command, root, env)
    rows, starts = [], {}
    lock = threading.Lock()
    trigger = threading.Event()
    forced = False
    stop_at = None
    live_pids = []
    initial = _snapshot_files(root)
    trace = Path(env["CODEY_AB_TRACE"])

    def write_rpc(payload):
        with lock:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write((_json_line(payload) + "\n").encode())
                proc.stdin.flush()

    def read_stdout():
        with (directory / "stdout.log").open("wb") as raw, (directory / "events.jsonl").open("a", encoding="utf-8") as journal:
            for line in proc.stdout:
                raw.write(line)
                raw.flush()
                parsed = _event_rows(line.decode("utf-8", "replace"))
                for row in parsed:
                    identity = row_id(row)
                    if row.get("type") in STARTS:
                        row["observed_workspace"] = _json_line(_snapshot_files(root))
                        starts[identity] = row
                    if row.get("type") in ENDS:
                        current = _json_line(_snapshot_files(root))
                        row["observed_result"] = _json_line(row.get("result", row.get("content")))
                        # Attribution is unknown for overlapping tools, not falsely precise.
                        if identity in starts and len(starts) == 1:
                            row["observed_changed"] = current != starts[identity]["observed_workspace"]
                        starts.pop(identity, None)
                        if control == "crash-after-edit" and _snapshot_files(root) != initial:
                            trigger.set()
                    rows.append(row)
                    journal.write(_json_line(row) + "\n")
                    journal.flush()
                    if rpc_task and row.get("type") == "agent_settled":
                        with lock, suppress(OSError):
                            proc.stdin.close()

    def read_stderr():
        with (directory / "stderr.log").open("wb") as handle:
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                handle.write(chunk)
                handle.flush()

    readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    for thread in readers:
        thread.start()
    if rpc_task:
        write_rpc({"id": "task", "type": "prompt", "message": rpc_task})
    else:
        proc.stdin.close()  # Pi print mode reads piped stdin before starting its prompt.
    started = time.perf_counter()
    status = "completed"
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if control == "cancel" and stop_at is None and any(r.get("kind") == "heartbeat" for r in execution_rows(trace)):
                stop_at = now
                trigger.set()
                if rpc_task:
                    write_rpc({"id": "stop", "type": "abort"})
                else:
                    (trace / "stop").touch()
            if control == "crash-after-edit" and trigger.is_set():
                status = "injected_crash"
                terminate_process_tree(proc, owner)
                break
            if now >= deadline or (stop_at is not None and now - stop_at > 10):
                status = "timeout"
                forced = True
                terminate_process_tree(proc, owner)
                break
            time.sleep(.02)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            forced = True
            terminate_process_tree(proc, owner)
        for thread in readers:
            thread.join(3)
        # Observe before the watchdog closes its Job Object and kills survivors.
        pids = {r.get("pid") for r in execution_rows(trace) if type(r.get("pid")) is int}
        live_pids = [pid for pid in pids if _pid_alive(pid)]
        if live_pids:
            forced = True
            terminate_process_tree(proc, owner)
        if owner:
            owner.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            with suppress(OSError):
                stream.close()
    elapsed = time.perf_counter() - started
    return {"status": status, "returncode": proc.returncode, "rows": rows, "wall_time_seconds": elapsed,
            "triggered": trigger.is_set(), "forced_cleanup": forced,
            "readers_finished": all(not t.is_alive() for t in readers),
            "live_fixture_processes": live_pids,
            "stop_seconds": time.monotonic() - stop_at if stop_at is not None else None}


def _sampling_extension(directory, seed, temperature):
    path = directory / "sampling.ts"
    path.write_text("export default function(pi) { pi.on('before_provider_request', (event) => "
                    + "({ ...event.payload, temperature: " + str(temperature) + ", seed: " + str(seed) + " })); }\n",
                    encoding="utf-8")
    return path


def _pi_command(pi_root, node, entry, resources, env):
    if entry == "source":
        return [node, "--import", (pi_root / "packages/coding-agent/src/experimental/source-resolver.ts").as_uri(),
                str(pi_root / "packages/coding-agent/src/cli.ts")]
    # Use shipped build resources, not newer src themes from the checkout.
    resources.mkdir()
    package = pi_root / "packages/coding-agent"
    for name in ("package.json", "README.md"):
        shutil.copy2(package / name, resources / name)
    shutil.copytree(package / "dist", resources / "dist")
    env["PI_PACKAGE_DIR"] = str(resources)
    return [node, str(package / "dist/cli.js")]


def _preflight_pi(directory, entry):
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Pi startup preflight: Node is unavailable")
    directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PI_OFFLINE="1", PI_TELEMETRY="0", PI_CODING_AGENT_DIR=str(directory / "pi-config"))
    command = _pi_command(ROOT / "reference-projects/pi", node, entry, directory / "pi-built-package", env)
    proc = subprocess.run([*command, "--version"], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=30, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Pi startup preflight: {proc.stderr[-2000:]}")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True)
    return {"pi_cli_version": proc.stdout.strip(), "node_version": version.stdout.strip(),
            "platform": sys.platform, "python_version": platform.python_version(), "python_executable": sys.executable}


def _wait_for_backend_idle(upstream, *, timeout):
    started = time.monotonic()
    while True:
        with urlopen(local_url(upstream) + "/api/extra/perf", timeout=5) as response:
            status = json.load(response)
        if status.get("idle") == 1 and status.get("queue") == 0:
            return time.monotonic() - started
        if time.monotonic() - started >= timeout:
            raise RuntimeError("KoboldCpp backend remained busy; paired isolation could not be established")
        time.sleep(.5)


def _score_scenario(case, measured, verification, phases, baseline_hashes, records):
    phase = phases[0]
    success = measured["task_success"]
    if case.expected == "blocked":
        success = measured["blocked"] and verification["scope_ok"] and not measured["false_completion"]
    if case.expected == "stopped":
        success = (phase["triggered"] and measured["stopped"] and verification["scope_ok"]
                   and not phase["forced_cleanup"] and not phase["live_fixture_processes"])
    if case.case_id in {"test-first", "long-output"}:
        checks = verification["agent_verifications"]
        success = success and bool(checks and checks[0]["passed"] is False
                                 and checks[0]["files"].get("app.py") == baseline_hashes["app.py"])
    if case.case_id == "long-output":
        measured["stored_middle_output_recovered"] = stored_output_was_read(records, "REQUIRED_VALUE=river")
        initial_runs = sum(v["files"].get("app.py") == baseline_hashes["app.py"]
                           for v in verification["agent_verifications"])
        success = success and measured["stored_middle_output_recovered"] and initial_runs == 1
    if case.followup:
        measured["recovery_success"] = success and len(phases) == 2
        success = success and measured["recovery_success"]
    success = success and phases[-1]["status"] == "completed" and all(
        not p["forced_cleanup"] and not p["live_fixture_processes"] for p in phases)
    if case.followup:
        measured["recovery_success"] = bool(success)
    measured["scenario_success"] = bool(success)
    measured["scope_ok"] = verification["scope_ok"]
    counts = {}
    for v in verification["agent_verifications"]:
        key = _json_line(v["files"])
        counts[key] = counts.get(key, 0) + 1
    measured["repeated_verification_same_files"] = sum(n - 1 for n in counts.values() if n > 1)


def _run_arm(arm, root, run_dir, proxy_url, proxy_records, *, case: ExperimentCase, baseline_hashes, max_turns,
             model_id, max_tokens, seed=41, temperature=0.0, timeout=180, window=32768, keep=12000, pi_entry="source"):
    run_dir.mkdir(parents=True, exist_ok=True)
    trace = run_dir / "trace"
    trace.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.endswith(("_API_KEY", "_AUTH_TOKEN", "_OAUTH_TOKEN"))}
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(ROOT), "NATIVE_TOOLS": "1", "CODEY_AB_TRACE": str(trace),
        "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
        "CODEY_AB_TEMPERATURE": str(temperature), "CODEY_AB_SEED": str(seed),
        "LOCAL_OPENAI_BASE_URL": proxy_url + "/v1", "LOCAL_OPENAI_MODEL": model_id, "LOCAL_OPENAI_API_KEY": "local",
        "LOCAL_OPENAI_CONTEXT_WINDOW": str(window), "LOCAL_OPENAI_CONTEXT_RESERVE": str(max_tokens),
        "LOCAL_OPENAI_CONTEXT_KEEP": str(keep), "CODEY_AB_WAIT": "1" if case.control == "cancel" else "0",
        "CODEY_AB_LONG_OUTPUT": "1" if case.case_id == "long-output" else "0"})
    node = shutil.which("node")
    pi_root = ROOT / "reference-projects/pi"
    if arm == "pi" and (not node or not (pi_root / "node_modules").exists()):
        return {"arm": arm, "status": "environment_error", "error": "Pi Node/dependencies are unavailable"}
    if arm == "pi":
        config = run_dir / "pi-config"
        _write_pi_config(config, proxy_url, model_id, max_tokens=max_tokens, window=window, keep=keep)
        env.update({"PATH": str(Path(node).parent) + os.pathsep + env.get("PATH", ""), "PI_CODING_AGENT_DIR": str(config)})
        extension = _sampling_extension(run_dir, seed, temperature)
        entry = _pi_command(pi_root, node, pi_entry, run_dir / "pi-built-package", env)
        base = entry + ["--provider", "kobold", "--model", model_id,
            "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "-e", str(extension),
            "--session", str(run_dir / "pi.session.jsonl")]
        command = base + ["--mode", "rpc"] if case.control == "cancel" else base + ["--mode", "json", "--print", case.task]
    else:
        base = [sys.executable, "-B", "-m", "tests.manual.agent_stability_codey_worker", "--project", str(root),
            "--state-home", str(run_dir / "state"), "--session-id", "ab_session", "--max-turns", str(max_turns)]
        command = base + [case.task]
    deadline = time.monotonic() + timeout
    phase_dir = run_dir / "phase-1"
    phase_dir.mkdir()
    phase = _run_process(command, root, env, phase_dir, deadline=deadline, control=case.control,
                         rpc_task=case.task if arm == "pi" and case.control == "cancel" else None)
    phases = [phase]
    if case.followup and phase["triggered"] and not phase["forced_cleanup"]:
        (trace / "stop").unlink(missing_ok=True)
        env["CODEY_AB_WAIT"] = "0"
        if case.case_id == "correction-after-stop":
            test = (root / "test_app.py").read_text(encoding="utf-8").replace("hello-world", "hello_world")
            (root / "test_app.py").write_text(test, encoding="utf-8")
            baseline_hashes = {**baseline_hashes, "test_app.py": _snapshot_files(root)["test_app.py"]}
        next_command = (base + ["--mode", "json", "--print", case.followup] if arm == "pi"
                        else base + (["--continue"] if case.control == "crash-after-edit" else []) + [case.followup])
        next_dir = run_dir / "phase-2"
        next_dir.mkdir()
        phases.append(_run_process(next_command, root, env, next_dir, deadline=deadline))
    rows = []
    for index, p in enumerate(phases):
        for row in p["rows"]:
            identity = row_id(row)
            if identity:
                row = {**row, "tool_id": f"{index}:{identity}"}
            rows.append(row)
    verification = _run_verification(root, case=case, baseline_hashes=baseline_hashes, trace=trace)
    arm_records = list(proxy_records)
    measured = _metrics(rows, arm_records, verification, phases[-1]["returncode"])
    _score_scenario(case, measured, verification, phases, baseline_hashes, arm_records)
    return {"arm": arm, "case": case.case_id, "status": phases[-1]["status"], "seed": seed,
            "wall_time_seconds": sum(p["wall_time_seconds"] for p in phases),
            "verification": verification, "metrics": measured,
            "phases": [{k: v for k, v in p.items() if k != "rows"} for p in phases], "artifacts": str(run_dir)}


def _source_identity(path):
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True)
    digest = hashlib.sha256()
    patterns = ("*.py",) if path == ROOT else ("*.ts",)
    folder = path / ("codey" if path == ROOT else "packages")
    for pattern in patterns:
        for file in sorted(folder.rglob(pattern)):
            if "node_modules" not in file.parts:
                digest.update(file.relative_to(path).as_posix().encode())
                digest.update(file.read_bytes())
    return {"commit": proc.stdout.strip(), "source_sha256": digest.hexdigest()}


def _pi_build_identity(path):
    digest = hashlib.sha256()
    for package in sorted((path / "packages").iterdir()):
        if (package / "dist").is_dir():
            for file in sorted((package / "dist").rglob("*")):
                if file.is_file():
                    digest.update(file.relative_to(path).as_posix().encode())
                    digest.update(file.read_bytes())
    return {"dist_sha256": digest.hexdigest(), "matching_source_commit": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--proxy-port", type=int, default=0)
    parser.add_argument("--max-turns", type=int, default=1000, help="Codey guard; shared HTTP budget normally binds first")
    parser.add_argument("--request-limit", type=int, default=24)
    parser.add_argument("--model", default="", help="exact model ID; otherwise discover the loaded model")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--window", type=int, default=32768)
    parser.add_argument("--keep", type=int, default=12000)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--idle-timeout", type=float, default=300,
                        help="isolation wait outside scored agent time; a busy backend stops the experiment")
    parser.add_argument("--seeds", default="41,42")
    parser.add_argument("--cases", default=",".join(c.case_id for c in TASK_CASES))
    parser.add_argument("--pi-entry", choices=("source", "dist"), default="source",
                        help="explicit runtime selection; never automatically substitutes a build")
    args = parser.parse_args()
    upstream = local_url(args.upstream)
    with urlopen(upstream + "/v1/models", timeout=5) as response:
        catalog = json.load(response)
    model = args.model or catalog["data"][0]["id"]
    if model not in {m["id"] for m in catalog["data"]}:
        parser.error("selected model is not loaded")
    selected = set(args.cases.split(","))
    if selected - {c.case_id for c in TASK_CASES}:
        parser.error("unknown case selection")
    seeds = [int(s) for s in args.seeds.split(",")]
    if len(set(seeds)) != len(seeds) or any(s < 0 for s in seeds):
        parser.error("seeds must be distinct nonnegative integers")
    if args.timeout <= 0 or args.idle_timeout <= 0 or args.request_limit <= 0 or not 0 < args.max_tokens < args.window:
        parser.error("time/request budgets must be positive and output must fit the context window")
    if not 0 < args.keep <= args.window - args.max_tokens:
        parser.error("recent-context budget must fit the input budget")
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    environment = _preflight_pi(run_dir / "preflight", args.pi_entry)
    projects = _new_project_root()
    with urlopen(upstream + "/api/extra/version", timeout=5) as response:
        backend = json.load(response)
    result = {"model": model, "backend": backend, "sampling": {"temperature": args.temperature, "seeds": seeds},
              "execution_environment": environment,
              "selected_cases": [c.case_id for c in TASK_CASES if c.case_id in selected],
              "unselected_cases": [c.case_id for c in TASK_CASES if c.case_id not in selected],
              "provider_profile": {"enabled_sources": ["local"], "codey_reviewer": "same local model",
                                   "sampling_stage": "before admission", "proxy": "observe without rewriting"},
              "budgets": {"window": args.window, "output": args.max_tokens, "keep": args.keep,
                          "seconds_per_arm": args.timeout, "generation_requests_per_arm": args.request_limit},
              "sources": {"codey": _source_identity(ROOT), "pi": _source_identity(ROOT / "reference-projects/pi")},
              "project_root": str(projects), "results": [], "pi_entry": args.pi_entry,
              "not_measured": ["live steering vs follow-up queues", "cross-model switching", "ambiguous edit injection",
                               "parallel speedup in a dedicated workload", "large real repository task completion"]}
    if args.pi_entry == "dist":
        result["sources"]["pi"]["executed_build"] = _pi_build_identity(ROOT / "reference-projects/pi")
    digest = hashlib.sha256()
    for path in sorted((ROOT / "tests/manual").glob("agent_stability*.py")) + [Path(__file__).resolve()]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    result["benchmark_sha256"] = digest.hexdigest()
    def save():
        result["paired_summary"] = paired_summary(result["results"])
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    try:
        for repeat, seed in enumerate(seeds):
            for case_index, case in enumerate(c for c in TASK_CASES if c.case_id in selected):
                arms = ("pi", "codey") if (repeat + case_index) % 2 == 0 else ("codey", "pi")
                for arm in arms:
                    _wait_for_backend_idle(upstream, timeout=args.idle_timeout)
                    directory = run_dir / f"seed-{seed}" / case.case_id / arm
                    directory.mkdir(parents=True)
                    root = projects / f"seed-{seed}" / case.case_id / arm
                    _fixture(root, case)
                    proxy = _Proxy(("127.0.0.1", args.proxy_port), upstream, args.timeout,
                                   fault=case.fault, journal=directory / "requests.jsonl",
                                   request_limit=args.request_limit)
                    proxy.active_arm = f"{seed}/{case.case_id}/{arm}"
                    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
                    thread.start()
                    try:
                        row = _run_arm(arm, root, directory, f"http://127.0.0.1:{proxy.server_port}", proxy.records,
                            case=case, baseline_hashes=_snapshot_files(root), max_turns=args.max_turns, model_id=model,
                            max_tokens=args.max_tokens, seed=seed, temperature=args.temperature, timeout=args.timeout,
                            window=args.window, keep=args.keep, pi_entry=args.pi_entry)
                    except Exception as exc:
                        row = {"arm": arm, "case": case.case_id, "seed": seed, "status": "harness_error",
                               "error": f"{type(exc).__name__}: {exc}"}
                    finally:
                        proxy.shutdown()
                        try:
                            drain = _wait_for_backend_idle(upstream, timeout=args.idle_timeout)
                        except Exception as exc:
                            row["backend_isolation_error"] = f"{type(exc).__name__}: {exc}"
                            result["results"].append(row)
                            save()
                            raise
                        finally:
                            proxy.server_close()
                    row["backend_drain_seconds"] = drain
                    if row.get("metrics"):
                        row["metrics"].update(usage_totals(proxy.records))
                    result["results"].append(row)
                    save()
                    print(_json_line({"seed": seed, "case": case.case_id, "arm": arm, "status": row["status"],
                        "success": row.get("metrics", {}).get("scenario_success"), "seconds": row.get("wall_time_seconds"),
                        "tokens": row.get("metrics", {}).get("token_usage")}), flush=True)
    finally:
        save()
    return 0 if result["results"] and all(r.get("metrics", {}).get("scenario_success") for r in result["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
