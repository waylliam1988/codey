"""Minimal live probe for local-model ``done`` termination.

This intentionally runs one read-only Codey task and records the OpenAI
compatible request/response through the same local proxy as the Pi/Codey A/B
harness. It does not edit files or run a coding task.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.manual.codey_vs_pi_agent_stability_ab import MODEL_ID, _Proxy

PROMPT = (
    "This is a termination protocol probe. Do not read, edit, write, or run "
    "anything. The task is already complete. Call the native done tool now "
    "with a short summary. Do not output ordinary text."
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--proxy-port", type=int, default=5018)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=512)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise SystemExit(f"run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)
    project = Path(tempfile.mkdtemp(prefix="codey-done-probe-")).resolve()
    state_home = run_dir / "state"
    proxy = _Proxy(
        ("127.0.0.1", args.proxy_port),
        args.upstream,
        timeout=300.0,
    )
    proxy.active_arm = "done-probe"
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env.update({
        "PYTHONUNBUFFERED": "1",
        "NATIVE_TOOLS": "1",
        "LOCAL_OPENAI_BASE_URL": f"http://127.0.0.1:{args.proxy_port}/v1",
        "LOCAL_OPENAI_MODEL": args.model,
        "LOCAL_OPENAI_API_KEY": "local",
        "LOCAL_OPENAI_CONTEXT_WINDOW": "32768",
        "LOCAL_OPENAI_CONTEXT_RESERVE": str(args.max_tokens),
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
        str(project),
        "--state-home",
        str(state_home),
        "--max-turns",
        str(args.max_turns),
        PROMPT,
    ]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=360,
            env=env,
            check=False,
        )
        rows = [
            json.loads(line)
            for line in proc.stdout.splitlines()
            if line.lstrip().startswith("{")
        ]
        done_calls = []
        for record in proxy.records:
            response = record.get("response")
            if not isinstance(response, dict):
                continue
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            message = choices[0].get("message")
            if not isinstance(message, dict) or not isinstance(message.get("tool_calls"), list):
                continue
            if any(
                isinstance(call, dict)
                and str((call.get("function") or {}).get("name") or "") == "done"
                for call in message["tool_calls"]
            ):
                done_calls.append(record)
        result = {
            "model": args.model,
            "prompt": PROMPT,
            "project": str(project),
            "wall_time_seconds": round(time.perf_counter() - started, 3),
            "process_returncode": proc.returncode,
            "done_call_requests": len(done_calls),
            "task_done_events": [row for row in rows if row.get("type") == "task_done"],
            "events": rows,
            "proxy_records": proxy.records,
            "stdout": proc.stdout[-20000:],
            "stderr": proc.stderr[-20000:],
        }
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["done_call_requests"] == 1 else 2
    finally:
        proxy.shutdown()
        proxy.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
