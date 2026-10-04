"""Local-model review smoke: real headless review chain (no model quality assert)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path


def run_review_case(target, directory: Path) -> dict:
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from tools import local_model_gate_attempts as attempts

    config = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    root = Path(config["project"])
    state_home = Path(config["state"])
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=False, timeout=30)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "gate@test"], check=False, timeout=10)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "gate"], check=False, timeout=10)
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=False, timeout=30)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=False, timeout=30)
    (root / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    baseline = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts
    }
    rows: list[dict] = []

    def _record(row):
        rows.append(row)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    (directory / "events.jsonl").touch(exist_ok=True)
    request = HeadlessRequest(
        project=root,
        task="Review the current diff for correctness.",
        provider_id="local",
        max_turns=6,
        intent="review",
        state_home=state_home,
    )
    t0 = time.perf_counter()
    try:
        result = run_headless(
            request,
            emit_jsonl=_record,
            connect_provider=lambda provider_id, **kwargs: attempts.make_provider(target, directory)
            if provider_id == "local" else (_ for _ in ()).throw(RuntimeError("gate pins local")),
        )
        dt = round(time.perf_counter() - t0, 1)
        after = {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts
        }
        review_events = [r for r in rows if r.get("type") == "review"]
        task_done = next((r for r in reversed(rows) if r.get("type") == "task_done"), None)
        ok = (
            task_done is not None
            and result.exit_code in (0, 1)
            and bool(review_events)
            and after == baseline
            and all("review" in r for r in review_events if r.get("review"))
        )
        # The smoke proves wiring (real Reviewer call, contract, read-only,
        # persistence hooks); it never asserts the model found a specific bug.
        data = {
            "case": "review",
            "ok": bool(ok and task_done),
            "seconds": dt,
            "stop_reason": str((task_done or {}).get("stop_reason") or result.stop_reason),
            "run_id": result.run_id,
            "session_id": result.session_id,
            "jsonl_rows": len(rows),
            "review_events": len(review_events),
            "failure_stage": "" if ok else "review_smoke",
        }
    except Exception as exc:  # noqa: BLE001
        data = {"case": "review", "ok": False, "failure_stage": "agent_exception",
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        (directory / "events.jsonl").touch(exist_ok=True)
    return data
