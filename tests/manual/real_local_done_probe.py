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
    "anything. The task is already complete. Use the declared done protocol now "
    "with a short summary. Do not output ordinary text."
)


def execution_observations(durable, outputs, session_id: str, run_id: str) -> dict[str, object]:
    """Score saved execution replies; a final SSE chunk is not a full reply."""
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.operations.provider_receipts import RESPONSE_KIND, read_provider_artifact
    from codey.providers.base import AssistantTurn
    from codey.providers.response_record import decode_reply
    effects = durable.effects(run_id)
    done_calls = 0
    for artifact in durable.artifacts_for_task(run_id):
        if artifact.metadata.get("kind") != RESPONSE_KIND:
            continue
        if not artifact.owner_effect_id:
            raise ValueError("saved provider response has no owning effect")
        reply = decode_reply(read_provider_artifact(outputs, artifact, kind=RESPONSE_KIND,
            session_id=session_id, run_id=run_id, effect_id=artifact.owner_effect_id))
        if isinstance(reply, AssistantTurn):
            done_calls += int(any(call.name == "done" for call in reply.tool_calls))
        else:
            try:
                value = json.loads(reply)
            except ValueError:
                continue
            done_calls += int(isinstance(value, dict) and value.get("tool") == "done")
    state = operation_from_checkpoint(durable.task(run_id), session_id)
    return {"done_call_requests": done_calls, "operation_leaf": state.leaf if state is not None else "",
        "provider_actions": [{"method": effect.intent.get("display_ref"),
            "sent_state": effect.settlement.get("sent_state") if effect.settlement is not None else None}
            for effect in effects if effect.category == "provider_send"],
        "pending_effects": [effect.effect_id for effect in effects if effect.settlement is None],
        "tool_effects": [effect.effect_id for effect in effects if effect.category == "tool_call"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--proxy-port", type=int, default=5018)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--protocol", choices=("native", "text"), default="native")
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
        "NATIVE_TOOLS": "1" if args.protocol == "native" else "0",
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
        "--readonly",
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
        starts = [row for row in rows if row.get("type") == "task_start"]
        observations = {}
        if len(starts) == 1:
            from codey.durable import DurableSession, SQLiteDurableStore
            from codey.storage.managed_outputs import ManagedOutputStore

            durable = DurableSession(SQLiteDurableStore(state_home / "durable.sqlite3"))
            try:
                observations = execution_observations(durable, ManagedOutputStore(state_home),
                    starts[0]["session_id"], starts[0]["run_id"])
            finally:
                durable.close()
        result = {
            "model": args.model,
            "protocol": args.protocol,
            "prompt": PROMPT,
            "project": str(project),
            "wall_time_seconds": round(time.perf_counter() - started, 3),
            "process_returncode": proc.returncode,
            **observations,
            "task_done_events": [row for row in rows if row.get("type") == "task_done"],
            "events": rows,
            "proxy_records": proxy.records,
            "stdout": proc.stdout[-20000:],
            "stderr": proc.stderr[-20000:],
        }
        (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        passed = (proc.returncode == 0 and result.get("done_call_requests") == 1
                  and result.get("operation_leaf") == "terminal" and not result.get("pending_effects")
                  and not result.get("tool_effects") and len(result["task_done_events"]) == 1
                  and result["task_done_events"][0].get("stop_reason") == "done")
        return 0 if passed else 2
    finally:
        proxy.shutdown()
        proxy.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
