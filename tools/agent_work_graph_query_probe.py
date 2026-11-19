"""Production-kernel live experiment for an optional, callable work graph.

No provider prompt injection, scripted live actions, completion override, or
production file edits. The only kernel wrapper binds the existing tool plugin
to that run's committed resources, then calls the original entry unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.env_names import NATIVE_TOOLS_ENV
from codey.operations import task_loop
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.operations.kernel_receipts import restore_result_receipt
from codey.operations.task_session import TaskSession
from codey.runtime.core.execution_receipt import execution_receipt
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.toolchain import tool_spec
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    require_json_integer,
    require_json_string,
)
from codey.workspace.revision import scan_workspace_fingerprint
from tools import agent_work_graph_probe as pilot
from tools import local_model_gate_attempts as attempts
from tools.work_graph_candidate import (
    TOOL_NAME,
    WorkContextService,
    build_snapshot,
    installed_tool,
    production_inputs,
)

MAX_TURNS = 16
ARMS = ("baseline", "flat", "graph")
ROOT = Path(__file__).resolve().parents[1]
RUNNER_FILES = ("tools/agent_work_graph_query_probe.py", "tools/work_graph_candidate.py", "tools/agent_work_graph_probe.py")


def cases() -> dict[str, pilot.Case]:
    result = {name: case for name, case in pilot.cases().items() if name in {"checkout", "relay"}}
    original = (ROOT / "codey/types/json_value.py").read_text(encoding="utf-8")
    needle = "if not isinstance(value, int) or isinstance(value, bool):"
    if original.count(needle) != 1:
        raise ValueError("Production codec changed; review the mutation before preparing")
    result["codec"] = pilot.Case(
        "Restore strict integer identity validation in this copied Codey JSON codec, following the current project contract. Preserve public signatures and unrelated JSON behavior.",
        {"README.md": "# Copied Codey codec regression\nRequest entry: `service/request.py`; contract: `docs/identity.md`.\n",
         "codec/__init__.py": "", "service/__init__.py": "",
         "codec/json_value.py": original.replace(needle, "if not isinstance(value, int):"),
         "service/request.py": "from codec.json_value import require_json_integer\n\ndef request_version(payload):\n    return require_json_integer(payload['version'], 'version')\n",
         "docs/identity.md": "# Request identity contract\n`service/request.py` uses `codec/json_value.py`.\nInteger identities must accept integers including zero and negative values, but must reject booleans, strings, floats and None with ValueError.\nPreserve the independent Boolean decoder: it must still accept True and False.\nJSON parsing must still reject duplicate object keys and non-finite numbers.\nPreserve signatures and error messages. Do not tighten other decoders as a side effect.\n",
         "tests/test_smoke.py": "from service.request import request_version\n\ndef test_version():\n    assert request_version({'version': 2}) == 2\n"},
        {}, ("codec/json_value.py",),
        "from service.request import request_version as f\nfrom codec.json_value import require_json_boolean, parse_json_object, decode_json_value\n"
        "def rejects(value):\n    try: f({'version':value})\n    except ValueError: return True\n    return False\n"
        "def duplicate_rejected():\n    try: parse_json_object('{\"x\":1,\"x\":2}')\n    except ValueError: return True\n    return False\n"
        "def nan_rejected():\n    try: decode_json_value(float('nan'))\n    except ValueError: return True\n    return False\n"
        "checks={'integers':all(f({'version':x})==x for x in [0,-2,7]),'booleans_rejected':all(rejects(x) for x in [True,False]),'other_types_rejected':all(rejects(x) for x in ['1',1.0,None]),'bool_decoder_preserved':require_json_boolean(True,'b') is True and require_json_boolean(False,'b') is False,'json_safety_preserved':duplicate_rejected() and nan_rejected()}\n"
        "import json;print(json.dumps(checks))\n")
    suffix = (" Preserve existing tests. Add focused regression tests in tests/test_regression.py, including boundary counterexamples. "
              "Use the available context and source tools to identify requirements and remaining verification gaps. "
              "After implementation and tests, run python -m pytest -q from the project root, then explain the evidence and any limits.")
    for name, case in result.items():
        task = case.task.replace("Modify source only, preserve existing tests.", "Modify the implementation.") + suffix
        result[name] = replace(case, task=task, allowed_changes=(*case.allowed_changes, "tests/test_regression.py"))
    return result


@contextmanager
def kernel_binding(directory: Path, arm: str,
                   inspect: Callable[[TaskSession, KernelRunRequest], None] | None = None) -> Generator[None, None, None]:
    original = task_loop.run_task_kernel
    initial_tools = set(tool_spec.tool_specs())

    def run(session: TaskSession, *, request: KernelRunRequest) -> KernelResult:
        project = request.execution.project_path
        if project is None:
            raise ValueError("Experiment requires the admitted project resource")
        service = WorkContextService(project, lambda: production_inputs(session, request.execution), graph=arm == "graph",
            observe=lambda row: pilot.append(directory / "queries.jsonl", row))
        with installed_tool(service) if arm != "baseline" else nullcontext():
            outcome = original(session, request=request)
            inputs = production_inputs(session, request.execution)
            snapshot = build_snapshot(project, inputs)
            pilot.write(directory / "final-graph.json", {"nodes": {key: asdict(node) for key, node in snapshot.nodes.items()},
                "edges": [asdict(edge) for edge in sorted(snapshot.edges)], "warnings": snapshot.warnings,
                "fingerprint": snapshot.fingerprint})
            receipts: list[JsonObject] = []
            sink = request.execution.intent_sink
            if sink is None:
                raise ValueError("Missing production durable sink")
            for effect in RuntimeEffectStore(durable=sink.durable).load_effects(sink.session_id, sink.run_id):
                if effect.intent.tool_name == TOOL_NAME and effect.settlement is not None:
                    restored = restore_result_receipt(effect, store=sink.managed_outputs, session_id=sink.session_id, run_id=sink.run_id)
                    receipts.append(canonical_json_object({"effect_id": effect.intent.effect_id,
                        "call_id": restored.call.call_id, "ok": restored.ok, "canonical": restored.canonical,
                        "result_sha256": effect.settlement.result_sha256, "restored_text": restored.model_text}))
            pilot.write(directory / "query-receipts.json", receipts)
            checks = []
            for identity, observed in inputs.results:
                execution = execution_receipt(observed)
                if execution is not None:
                    from codey.operations.kernel_provenance import _kernel_workspace_identity_of

                    trusted = _kernel_workspace_identity_of(observed)
                    checks.append({"effect_id": identity, "command": observed.call.args.get("command"),
                        "ok": observed.ok, "exit_code": execution.exit_code, "output": execution.output or observed.model_text,
                        "fingerprint": trusted.fingerprint if trusted else "",
                        "current": bool(trusted and trusted.fingerprint == inputs.fingerprint)})
            pilot.write(directory / "production-checks.json", checks)
            if inspect is not None:
                inspect(session, request)
            return outcome

    try:
        with patch.object(task_loop, "run_task_kernel", run):
            yield
    finally:
        if set(tool_spec.tool_specs()) != initial_tools or tool_spec.custom_executor_for(TOOL_NAME) is not None:
            raise RuntimeError("Experiment leaked a tool registration")


def regression_checks(directory: Path, case: pilot.Case) -> JsonObject:
    project = directory / "project"
    test_path = project / "tests/test_regression.py"
    if not test_path.is_file():
        return {"present": False, "passes_final": False, "detects_original_bug": False}
    command = [sys.executable, "-B", "-m", "pytest", "-q", "tests/test_regression.py"]
    final = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=40)
    target = project / case.allowed_changes[0]
    original_bytes = target.read_bytes()
    before = scan_workspace_fingerprint(project)
    try:
        target.write_text(case.files[case.allowed_changes[0]], encoding="utf-8")
        mutant = subprocess.run(command, cwd=project, capture_output=True, text=True, timeout=40)
    finally:
        target.write_bytes(original_bytes)
    if scan_workspace_fingerprint(project) != before:
        raise ValueError("External mutation grading did not restore the fixture")
    return {"present": True, "passes_final": final.returncode == 0 and bool(re.search(r"\d+ passed", final.stdout)),
        "detects_original_bug": mutant.returncode == 1 and bool(re.search(r"\d+ failed", mutant.stdout)),
        "final_exit": final.returncode, "final_output": final.stdout + final.stderr,
        "mutant_exit": mutant.returncode, "mutant_output": mutant.stdout + mutant.stderr}


def runner_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in RUNNER_FILES}


def prepare(root: Path, repeats: int) -> None:
    root.mkdir(parents=True, exist_ok=False)
    with urlopen("http://127.0.0.1:5001/v1/models", timeout=5) as response:
        models = decode_json_object(json.load(response))["data"]
    if not isinstance(models, list) or not models or decode_json_object(models[0]).get("id") != pilot.MODEL:
        raise ValueError("Pinned model is not loaded")
    frozen_cases = cases()
    matrix = [{"case": name, "arm": ARMS[(index + repeat + offset) % 3], "repeat": repeat}
              for repeat in range(repeats) for index, name in enumerate(frozen_cases) for offset in range(3)]
    pilot.write(root / "protocol.json", {"version": 1, "model": pilot.MODEL, "endpoint": pilot.ENDPOINT,
        "temperature": 0, "native_tools": True, "output_tokens": 4096, "request_timeout": 180,
        "physical_attempts": 1, "max_turns": MAX_TURNS, "repeats": repeats, "matrix": matrix,
        "cases": {name: asdict(case) for name, case in frozen_cases.items()}, "runner_hashes": runner_hashes(),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "copied_codec_sha256": hashlib.sha256((ROOT / "codey/types/json_value.py").read_bytes()).hexdigest(),
        "scoring": "Independent product checks + unchanged original tests/scope + new regression passes and fails original buggy implementation. End-to-end additionally requires done and a committed current-workspace pytest pass.",
        "limits": ["Controlled synthetic sources, not real internet/browser automation.",
            "No worktree creation or repository copying. Independent tiny fixtures; the codec case copies one module only. Serial model requests.",
            "Flat and graph expose identical tool schemas and node corpus; graph changes traversal, links and gap presentation.",
            "No forced queries, edits or tests. No hidden grader data reaches the graph.",
            "Run-local graph only; no cross-session memory experiment or semantic proof of requirement coverage.",
            "Small exploratory sample. Production Python hashes must remain equal across cells."]})
    pilot.write(root / "production-freeze.json", pilot.source_hashes())
    pilot.write(root / "backend-preflight.json", pilot.backend_status())
    print(str(root), flush=True)


def run_cell(root: Path, name: str, arm: str, repeat: int) -> None:
    before = pilot.backend_status()
    if not before.get("idle") or before.get("queue"):
        raise RuntimeError("Backend busy; do not queue or abort unrelated work")
    if pilot.source_hashes() != json.loads((root / "production-freeze.json").read_text(encoding="utf-8")):
        raise RuntimeError("Production changed after freeze; prepare a new experiment")
    frozen = decode_json_object(json.loads((root / "protocol.json").read_text(encoding="utf-8")))
    case = cases()[name]
    if json.loads(json.dumps(asdict(case))) != decode_json_object(frozen["cases"])[name]:
        raise RuntimeError("Fixture differs from the frozen protocol")
    directory = root / f"r{repeat}-{name}-{arm}"
    directory.mkdir(exist_ok=False)
    project = directory / "project"
    for key, text in case.files.items():
        path = project / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=Graph Pilot", "-c", "user.email=graph-pilot@example.invalid",
                    "commit", "-qm", "fixture"], check=True, capture_output=True)
    pilot.write(directory / "backend-before.json", before)
    started = time.monotonic()
    exception = None

    def connect(provider_id: str, *, port: int) -> pilot.Provider:
        if provider_id != "local":
            raise ValueError("Only the pinned provider is admitted")
        return pilot.Provider(directory)

    target = attempts.GateTarget(pilot.ENDPOINT, pilot.MODEL, 262144, 32768, 32000)
    try:
        with (patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}), attempts.isolate_gate_secondary_models(target),
              pilot.controlled(case, directory), kernel_binding(directory, arm)):
            run_headless(HeadlessRequest(project, case.task, provider_id="local", intent="chat", max_turns=MAX_TURNS,
                state_home=directory / "state", requested_capabilities=("web.read",),
                sources_open_required=False, project_changes_required=True,
                model_selection={"model": pilot.MODEL, "base_url": pilot.ENDPOINT}),
                emit_jsonl=lambda row: pilot.append(directory / "events.jsonl", row), connect_provider=connect)
    except Exception as exc:
        exception = f"{type(exc).__name__}: {exc}"
    result = pilot.evaluate(directory, case)
    regression = regression_checks(directory, case)
    pilot.write(directory / "regression-checks.json", regression)
    checks_path = directory / "production-checks.json"
    raw_checks: object = json.loads(checks_path.read_text(encoding="utf-8")) if checks_path.exists() else []
    if not isinstance(raw_checks, list):
        raise ValueError("Production checks must be an array")
    checks = [decode_json_object(item) for item in raw_checks]
    pytest_checks = [check for check in checks if "pytest" in str(check["command"])]
    latest: JsonObject = pytest_checks[-1] if pytest_checks else {}
    verified = bool(latest.get("current") and latest.get("ok") and latest.get("exit_code") == 0
                    and str(latest.get("command", "")).strip() == "python -m pytest -q"
                    and re.search(r"\d+ passed", str(latest.get("output", ""))))
    objective = bool(result["success"] and regression["passes_final"] and regression["detects_original_bug"])
    result.update(canonical_json_object({"case": name, "arm": arm, "repeat": repeat,
        "product_success": result["success"], "objective_success": objective,
        "success": objective and result["stop_reason"] == "done" and verified and exception is None,
        "production_current_pytest_pass": verified, "regression_sensitive": regression["detects_original_bug"],
        "query_count": len(pilot.records(directory / "queries.jsonl")), "exception": exception,
        "seconds": round(time.monotonic() - started, 3), "backend_after": pilot.backend_status(),
        "production_unchanged": pilot.source_hashes() == json.loads((root / "production-freeze.json").read_text(encoding="utf-8"))}))
    pilot.write(directory / "summary.json", result)
    print(json.dumps({key: result.get(key) for key in ("case", "arm", "repeat", "stop_reason", "product_checks",
        "query_count", "request_count", "objective_success", "success", "production_current_pytest_pass",
        "regression_sensitive", "exception", "seconds")}, ensure_ascii=False), flush=True)
    if not decode_json_object(result["backend_after"]).get("idle"):
        raise RuntimeError("Backend remains busy; stop without aborting another run")


def aggregate(root: Path) -> None:
    rows = [decode_json_object(json.loads(path.read_text(encoding="utf-8"))) for path in sorted(root.glob("*/summary.json"))]
    arms = {arm: {"cells": len(group := [row for row in rows if row["arm"] == arm]),
                  "product": sum(bool(row["product_success"]) for row in group),
                  "objective": sum(bool(row["objective_success"]) for row in group),
                  "end_to_end": sum(bool(row["success"]) for row in group),
                  "queries": sum(require_json_integer(row["query_count"], "queries") for row in group),
                  "requests": sum(require_json_integer(row["request_count"], "requests") for row in group)} for arm in ARMS}
    pilot.write(root / "aggregate.json", {"arms": arms, "rows": rows})
    print(json.dumps(arms), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--repeats", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.root, args.repeats)
    elif args.run:
        protocol = decode_json_object(json.loads((args.root / "protocol.json").read_text(encoding="utf-8")))
        if protocol["runner_hashes"] != runner_hashes():
            raise RuntimeError("Experiment code changed after protocol freeze")
        matrix = protocol["matrix"]
        if not isinstance(matrix, list):
            raise ValueError("Protocol matrix must be an array")
        for item in matrix:
            row = decode_json_object(item)
            repeat = require_json_integer(row["repeat"], "repeat")
            name = require_json_string(row["case"], "case")
            arm = require_json_string(row["arm"], "arm")
            if not (args.root / f"r{repeat}-{name}-{arm}" / "summary.json").exists():
                run_cell(args.root, name, arm, repeat)
        aggregate(args.root)
    else:
        aggregate(args.root)


if __name__ == "__main__":
    main()
