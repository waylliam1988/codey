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


def _fixture(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in FIXTURE_FILES.items():
        (root / name).write_text(content, encoding="utf-8")


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


def _run_verification(root: Path) -> dict[str, Any]:
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
        checks = {
            "trim_and_collapse": module.normalize_name("  Hello   World  ") == "hello-world",
            "punctuation": module.normalize_name(" Hello, World! ") == "hello-world",
        }
    except Exception as exc:  # verifier output must remain machine-readable
        checks = {"import_error": f"{type(exc).__name__}: {exc}"}
    return {
        "returncode": proc.returncode,
        "passed": proc.returncode == 0 and all(value is True for value in checks.values()),
        "checks": checks,
        "stdout": _safe_text(proc.stdout),
        "stderr": _safe_text(proc.stderr),
        "wall_time_seconds": round(time.perf_counter() - started, 3),
    }


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


def _metrics(rows: list[dict[str, Any]], records: list[dict[str, Any]], verification: dict[str, Any], returncode: int) -> dict[str, Any]:
    calls: list[str] = []
    mutation_signatures: list[str] = []
    started: dict[str, str] = {}
    terminal: list[dict[str, Any]] = []

    def row_tool(row: dict[str, Any]) -> str:
        return str(row.get("tool") or row.get("toolName") or row.get("tool_name") or "")

    def row_id(row: dict[str, Any]) -> str:
        return str(row.get("tool_id") or row.get("toolCallId") or row.get("tool_call_id") or "")

    def row_succeeded(row: dict[str, Any]) -> bool:
        if type(row.get("ok")) is bool:
            return row["ok"]
        if type(row.get("isError")) is bool:
            return not row["isError"]
        return row.get("status") == "ok"

    for row in rows:
        kind = str(row.get("type") or "")
        if kind in {"tool_started", "tool_execution_start"}:
            tool = row_tool(row)
            signature = _tool_signature(tool, row)
            calls.append(signature)
            if row_id(row):
                started[row_id(row)] = signature
        elif kind in {"tool", "tool_execution_end", "tool_result", "tool_execution_finished", "tool_finished"}:
            tool = row_tool(row)
            if tool.lower() in {"edit", "write", "write_file"} and row_succeeded(row):
                mutation_signatures.append(started.get(row_id(row), _tool_signature(tool, row)))
        if kind in {"task_done", "agent_settled"}:
            terminal.append(row)
    for record in records:
        response = record.get("response")
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            usage = response["usage"]
            if type(usage.get("total_tokens")) is int:
                calls.append(f"__usage__:{usage['total_tokens']}")
    counts = Counter(calls)
    usage_total = sum(int(item.split(":", 1)[1]) for item in calls if item.startswith("__usage__:"))
    repeated = sum(count - 1 for item, count in counts.items() if not item.startswith("__usage__:") and count > 1)
    mutation_counts = Counter(mutation_signatures)
    done = any(str(row.get("stop_reason") or "").lower() == "done" for row in terminal)
    return {
        "process_returncode": returncode,
        "task_success": returncode == 0 and verification["passed"],
        "patch_correctness": verification["passed"],
        "tests_actually_passing": verification["passed"],
        "false_completion": done and not verification["passed"],
        "tool_calls": len([item for item in calls if not item.startswith("__usage__:")]),
        "repeated_tool_calls": repeated,
        "mutation_calls": len(mutation_signatures),
        "duplicate_mutation": any(count > 1 for count in mutation_counts.values()),
        "recovery_success": None,
        "token_usage": usage_total or None,
        "terminal_event": terminal[-1] if terminal else None,
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
    *,
    max_turns: int,
    model_id: str,
    max_tokens: int,
) -> dict[str, Any]:
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
            TASK,
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
            TASK,
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
    verification = _run_verification(root)
    return {
        "arm": arm,
        "status": "completed",
        "wall_time_seconds": wall,
        "verification": verification,
        "metrics": _metrics(rows, [], verification, proc.returncode),
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
        "arms": {},
    }
    try:
        for arm in ("pi", "codey"):
            root = project_root / arm
            _fixture(root)
            proxy.active_arm = arm
            result["arms"][arm] = _run_arm(
                arm,
                root,
                run_dir / arm,
                proxy_url,
                max_turns=args.max_turns,
                model_id=args.model,
                max_tokens=args.max_tokens,
            )
        (run_dir / "proxy-records.json").write_text(_json_line(proxy.records), encoding="utf-8")
        for arm in ("pi", "codey"):
            records = [record for record in proxy.records if record.get("arm") == arm]
            result["arms"].setdefault(arm, {})["request_records"] = len(records)
            result["arms"][arm].setdefault("metrics", {})["token_usage"] = sum(
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
    return 0 if all(item.get("status") == "completed" for item in result["arms"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
