"""Run a reproducible Pi vs Codey local-model coding comparison.

The runner deliberately keeps model traffic local.  A small OpenAI-compatible
proxy records request/response usage before forwarding to KoboldCpp, while the
two agents run in isolated copies of the same fixture project.  It is a manual
probe, not a production dependency and is never imported by Codey.

Example (PowerShell):
    python tests/manual/real_local_ab.py --run-dir E:\\codey\\artifacts\\ab-1

Pi needs the downloaded source at ``reference-projects/pi`` and the Node
runtime used by this workspace.  The script fails with an explicit
``environment_error`` if either prerequisite is missing.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MODEL_ID = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
TASK = (
    "Fix app.py so normalize_name returns a lowercase hyphen-separated name, "
    "removes punctuation, trims outer whitespace, and collapses repeated "
    "whitespace. Do not modify the tests. Inspect the existing tests, edit only "
    "app.py, run python -m unittest discover -v, and call done only after the "
    "tests pass."
)
FIXTURE_FILES = {
    "app.py": """def normalize_name(value: str) -> str:\n    return \"-\".join(value.strip().lower().split())\n""",
    "test_app.py": """import unittest\n\nfrom app import normalize_name\n\n\nclass NormalizeNameTests(unittest.TestCase):\n    def test_trims_collapses_and_lowercases(self):\n        self.assertEqual(normalize_name(\"  Hello   World  \"), \"hello-world\")\n\n    def test_removes_punctuation(self):\n        self.assertEqual(normalize_name(\" Hello, World! \"), \"hello-world\")\n\n\nif __name__ == \"__main__\":\n    unittest.main()\n""",
}


@dataclass(frozen=True)
class ExperimentCase:
    case_id: str
    task: str
    fixture_files: dict[str, str]
    allowed_paths: tuple[str, ...]


TASK_CASES = (
    ExperimentCase("normalize-name", TASK, FIXTURE_FILES, ("app.py",)),
)


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _safe_text(value: object, limit: int = 200_000) -> str:
    return str(value or "")[:limit]


def _strict_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _content_length(value: str) -> int:
    try:
        parsed = int(value.strip())
    except (AttributeError, TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _with_sampling(raw: bytes, temperature: float, max_tokens: int) -> bytes:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    if not isinstance(payload, dict):
        return raw
    payload["temperature"] = float(temperature)
    payload["max_tokens"] = int(max_tokens)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _tool_signature(tool: str, payload: dict[str, Any]) -> str:
    args = payload.get("args")
    if not isinstance(args, dict):
        args = {key: payload.get(key) for key in ("path", "command", "cwd") if key in payload}
    return _json_line({"tool": tool, "args": args})


def _fixture(root: Path, case: ExperimentCase | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in (case.fixture_files if case else FIXTURE_FILES).items():
        (root / name).write_text(content, encoding="utf-8")


def _snapshot_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.relative_to(root).parts
    }


def _new_project_root() -> Path:
    """Create projects outside the runner's Git tree.

    Codey intentionally reports the containing Git repository's changes.  A
    benchmark fixture nested in the Codey checkout would therefore measure the
    harness files instead of the fixture patch.
    """

    return Path(tempfile.mkdtemp(prefix="codey-pi-ab-projects-")).resolve()


def _working_directory(_arm: str, root: Path) -> Path:
    """Run both agents from the isolated project they are evaluating."""

    return root


def _run_verification(
    root: Path,
    case: ExperimentCase | None = None,
    baseline_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    try:
        module_name = f"_real_local_ab_app_{hash(root)}"
        spec = importlib.util.spec_from_file_location(module_name, root / "app.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load {root / 'app.py'}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if case is not None and case.case_id == "no-op":
            checks = {"already_satisfied": module.normalize_name("anything") == "hello-world"}
        else:
            checks = {
                "trim_and_collapse": module.normalize_name("  Hello   World  ") == "hello-world",
                "punctuation": module.normalize_name(" Hello, World! ") == "hello-world",
            }
    except Exception as exc:  # verifier output must remain machine-readable
        checks = {"import_error": f"{type(exc).__name__}: {exc}"}
    current_hashes = _snapshot_files(root)
    if baseline_hashes is None:
        changed_paths = []
        scope_ok = True
    else:
        changed_paths = sorted(path for path in set(baseline_hashes) | set(current_hashes)
                               if baseline_hashes.get(path) != current_hashes.get(path))
        allowed = set(case.allowed_paths) if case else {"app.py"}
        scope_ok = set(changed_paths) <= allowed
    visible_tests = proc.returncode == 0
    hidden_checks = all(value is True for value in checks.values())
    return {
        "returncode": proc.returncode,
        "visible_tests": visible_tests,
        "hidden_checks": hidden_checks,
        "scope_ok": scope_ok,
        "changed_paths": changed_paths,
        "passed": visible_tests and hidden_checks and scope_ok and all(value is True for value in checks.values()),
        "checks": checks,
        "stdout": _safe_text(proc.stdout),
        "stderr": _safe_text(proc.stderr),
        "wall_time_seconds": round(time.perf_counter() - started, 3),
    }


def _experiment_succeeded(case_result: dict[str, Any]) -> bool:
    """Return true only when the arm completed and task verification passed."""
    if case_result.get("status") != "completed":
        return False
    metrics = case_result.get("metrics")
    return isinstance(metrics, dict) and metrics.get("task_success") is True


class _KoboldProxy(BaseHTTPRequestHandler):
    server_version = "CodeyABProxy/1"

    def log_message(self, _format: str, *_args: object) -> None:
        return None

    def _forward(self) -> None:
        proxy = self.server  # type: ignore[assignment]
        length = _content_length(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if urlsplit(self.path).path == "/v1/chat/completions":
            body = _with_sampling(body, proxy.temperature, proxy.max_tokens)
        upstream = f"{proxy.upstream}{urlsplit(self.path).path}"
        if urlsplit(self.path).query:
            upstream += "?" + urlsplit(self.path).query
        request = Request(upstream, data=body or None, method=self.command)
        for key in ("Content-Type", "Authorization"):
            value = self.headers.get(key)
            if value:
                request.add_header(key, value)
        started = time.perf_counter()
        status = 599
        response_body = b""
        error = ""
        try:
            with urlopen(request, timeout=proxy.timeout) as response:
                status = int(response.status)
                response_body = response.read(16 * 1024 * 1024)
                self.send_response(status)
                for key in ("Content-Type",):
                    value = response.headers.get(key)
                    if value:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response_body)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response_body = json.dumps({"error": error}).encode("utf-8")
            self.wfile.write(response_body)
        proxy.records.append(
            {
                "arm": proxy.active_arm,
                "method": self.command,
                "path": urlsplit(self.path).path,
                "request": _safe_json(body),
                "status": status,
                "response": _safe_json(response_body),
                "error": error,
                "wall_time_seconds": round(time.perf_counter() - started, 3),
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()


def _safe_json(raw: bytes) -> object:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Streaming OpenAI responses are SSE, not one JSON document.  Keep
        # the final structured frame so usage remains measurable while the
        # proxy still avoids storing the full model stream.
        frames: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                frames.append(value)
        for value in reversed(frames):
            if isinstance(value.get("usage"), dict):
                return value
        if frames:
            return frames[-1]
        return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


class _Proxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream: str,
        timeout: float,
        temperature: float,
        max_tokens: int,
    ) -> None:
        super().__init__(address, _KoboldProxy)
        self.upstream = upstream.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.active_arm = ""
        self.records: list[dict[str, Any]] = []


def _event_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("tool_id") or row.get("toolCallId") or row.get("tool_call_id") or "")


def _row_tool(row: dict[str, Any]) -> str:
    return str(row.get("tool") or row.get("toolName") or row.get("tool_name") or "")


def _row_outcome(row: dict[str, Any]) -> bool | None:
    if type(row.get("ok")) is bool:
        return row["ok"]
    if type(row.get("isError")) is bool:
        return not row["isError"]
    status = row.get("status")
    if isinstance(status, str) and status in {"ok", "error"}:
        return status == "ok"
    return None


def _metrics(rows: list[dict[str, Any]], records: list[dict[str, Any]], verification: dict[str, Any], returncode: int) -> dict[str, Any]:
    starts: dict[str, dict[str, Any]] = {}
    anonymous_starts: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    terminal: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("type") or "")
        call_id = _row_id(row)
        if kind in {"tool_started", "tool_execution_start"}:
            if call_id:
                starts.setdefault(call_id, row)
            else:
                anonymous_starts.append(row)
        elif kind in {"tool", "tool_execution_end", "tool_result", "tool_execution_finished", "tool_finished"} and call_id:
            results[call_id] = row
        if kind in {"task_done", "agent_settled"}:
            terminal.append(row)
    tools = list(starts.values()) + anonymous_starts
    outcomes = [_row_outcome(results[key]) for key in starts if key in results]
    successes = sum(value is True for value in outcomes)
    failures = sum(value is False for value in outcomes)
    signatures = Counter(_tool_signature(_row_tool(row), row) for row in tools)
    mutations = Counter(
        _tool_signature(_row_tool(row), row) for key, row in starts.items()
        if _row_tool(row).lower() in {"edit", "write", "write_file"}
        and key in results and _row_outcome(results[key]) is True
    )
    usage = [record["response"]["usage"] for record in records
             if isinstance(record.get("response"), dict) and isinstance(record["response"].get("usage"), dict)]
    names = [_row_tool(row) for row in tools]
    verified = verification.get("passed") is True
    done = any(str(row.get("stop_reason") or "").lower() == "done" for row in terminal)
    return {
        "process_returncode": returncode,
        "task_success": type(returncode) is int and returncode == 0 and verified,
        "patch_correctness": verified,
        "tests_actually_passing": verified,
        "false_completion": done and not verified,
        "tool_calls": len(tools),
        "repeated_tool_calls": sum(count - 1 for count in signatures.values() if count > 1),
        "mutation_calls": sum(mutations.values()),
        "duplicate_mutation": any(count > 1 for count in mutations.values()),
        "recovery_success": None,
        "token_usage": sum(value["total_tokens"] for value in usage if type(value.get("total_tokens")) is int) or None,
        "terminal_event": terminal[-1] if terminal else None,
        "llm_rounds": sum(record.get("path") == "/v1/chat/completions" for record in records),
        "failed_tool_calls": failures,
        "successful_tool_calls": successes,
        "unknown_tool_calls": len(tools) - successes - failures,
        "read_calls": names.count("read_file") + names.count("read"),
        "edit_calls": names.count("edit"),
        "write_calls": names.count("write") + names.count("write_file"),
        "run_calls": names.count("run") + names.count("bash"),
        "protocol_errors": sum("protocol" in str(row.get("type") or "").lower() for row in rows),
        "policy_denials": sum("denied" in str(row.get("type") or "").lower() for row in rows),
        "input_tokens": sum(value["prompt_tokens"] for value in usage if type(value.get("prompt_tokens")) is int) or None,
        "output_tokens": sum(value["completion_tokens"] for value in usage if type(value.get("completion_tokens")) is int) or None,
    }


def _write_pi_config(
    config_dir: Path,
    base_url: str,
    model_id: str,
    *,
    max_tokens: int = 2048,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "providers": {
            "kobold": {
                "baseUrl": f"{base_url.rstrip('/')}/v1",
                "apiKey": "local",
                "api": "openai-completions",
                "models": [
                    {
                        "id": model_id,
                        "name": model_id,
                        "reasoning": False,
                        "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        "contextWindow": 32768,
                        "maxTokens": max_tokens,
                        "compat": {
                            "maxTokensField": "max_tokens",
                            "supportsStore": False,
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                            "supportsUsageInStreaming": False,
                            "supportsStrictMode": False,
                        },
                    }
                ],
            }
        }
    }
    (config_dir / "models.json").write_text(_json_line(payload), encoding="utf-8")


def _run_arm(
    arm: str,
    root: Path,
    run_dir: Path,
    proxy_url: str,
    proxy_records: list[dict[str, Any]],
    *,
    case: ExperimentCase,
    baseline_hashes: dict[str, str],
    max_turns: int,
    model_id: str,
    max_tokens: int,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / f"{arm}.stdout.log"
    stderr_path = run_dir / f"{arm}.stderr.log"
    rows_path = run_dir / f"{arm}.events.jsonl"
    env = os.environ.copy()
    env.update({"PYTHONUNBUFFERED": "1", "NATIVE_TOOLS": "1"})
    if arm == "codey":
        env.update({
            "LOCAL_OPENAI_BASE_URL": f"{proxy_url}/v1",
            "LOCAL_OPENAI_MODEL": model_id,
            "LOCAL_OPENAI_API_KEY": "local",
            "LOCAL_OPENAI_CONTEXT_WINDOW": "32768",
            "LOCAL_OPENAI_CONTEXT_RESERVE": "8192",
            "LOCAL_OPENAI_CONTEXT_KEEP": "12000",
        })
        command = [
            sys.executable,
            "-m",
            "codey",
            "agent",
            "--json",
            "--provider",
            "local",
            "--project",
            str(root),
            "--state-home",
            str(run_dir / "state"),
            "--max-turns",
            str(max_turns),
            case.task,
        ]
    else:
        node = shutil.which("node") or str(Path(r"E:\codex-lite\runtime\tools\node\node.exe"))
        pi_root = Path(__file__).resolve().parents[2] / "reference-projects" / "pi"
        pi_test = pi_root / "pi-test.ps1"
        if not Path(node).exists() or not pi_test.exists() or not (pi_root / "node_modules").exists():
            return {"arm": arm, "status": "environment_error", "error": "Pi Node/dependencies are unavailable"}
        config = run_dir / "pi-config"
        _write_pi_config(config, proxy_url, model_id, max_tokens=max_tokens)
        env.update({
            "PATH": str(Path(node).parent) + os.pathsep + env.get("PATH", ""),
            "PI_CODING_AGENT_DIR": str(config),
        })
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(pi_test),
            "--no-env",
            "--provider",
            "kobold",
            "--model",
            model_id,
            "--mode",
            "json",
            "--print",
            "--no-session",
            case.task,
        ]
    started = time.perf_counter()
    proc = subprocess.run(
        command,
        cwd=_working_directory(arm, root),
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
        check=False,
    )
    wall = round(time.perf_counter() - started, 3)
    stdout_path.write_text(proc.stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(proc.stderr, encoding="utf-8", errors="replace")
    rows = _event_rows(proc.stdout)
    rows_path.write_text("\n".join(_json_line(row) for row in rows) + ("\n" if rows else ""), encoding="utf-8")
    verification = _run_verification(root, case=case, baseline_hashes=baseline_hashes)
    arm_records = [record for record in proxy_records if record.get("arm") == f"{case.case_id}/{arm}"]
    return {
        "arm": arm,
        "status": "completed",
        "wall_time_seconds": wall,
        "verification": verification,
        "metrics": _metrics(rows, arm_records, verification, proc.returncode),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "events": str(rows_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--proxy-port", type=int, default=5017)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=2048)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    proxy = _Proxy(
        ("127.0.0.1", args.proxy_port),
        args.upstream,
        timeout=900.0,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    proxy_url = f"http://127.0.0.1:{args.proxy_port}"
    project_root = _new_project_root()
    result: dict[str, Any] = {
        "model": args.model,
        "task": TASK,
        "proxy": proxy_url,
        "project_root": str(project_root),
        "cases": [case.case_id for case in TASK_CASES],
        "arms": {},
    }
    try:
        for case in TASK_CASES:
            for arm in ("pi", "codey"):
                root = project_root / case.case_id / arm
                _fixture(root, case)
                baseline_hashes = _snapshot_files(root)
                proxy.active_arm = f"{case.case_id}/{arm}"
                arm_result = _run_arm(
                    arm,
                    root,
                    run_dir / case.case_id / arm,
                    proxy_url,
                    proxy_records=proxy.records,
                    case=case,
                    baseline_hashes=baseline_hashes,
                    max_turns=args.max_turns,
                    model_id=args.model,
                    max_tokens=args.max_tokens,
                )
                result["arms"].setdefault(arm, {})[case.case_id] = arm_result
        (run_dir / "proxy-records.json").write_text(_json_line(proxy.records), encoding="utf-8")
        for arm in ("pi", "codey"):
            records = [record for record in proxy.records if str(record.get("arm") or "").endswith(f"/{arm}")]
            result["arms"].setdefault(arm, {})["request_records"] = len(records)
            result["arms"][arm]["token_usage"] = sum(
                int(record["response"]["usage"]["total_tokens"])
                for record in records
                if isinstance(record.get("response"), dict)
                and isinstance(record["response"].get("usage"), dict)
                and type(record["response"]["usage"].get("total_tokens")) is int
            ) or None
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        proxy.shutdown()
        proxy.server_close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    case_results = [
        case_result
        for arm_result in result["arms"].values()
        if isinstance(arm_result, dict)
        for case_result in arm_result.values()
        if isinstance(case_result, dict) and "status" in case_result
    ]
    return 0 if case_results and all(_experiment_succeeded(item) for item in case_results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
