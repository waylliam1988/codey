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
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=30)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "gate@test"], check=True, timeout=10)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "gate"], check=True, timeout=10)
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, timeout=30)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, timeout=30)
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
            connect_reviewer=lambda provider_id: attempts.make_provider(target, directory)
            if provider_id == "local" else (_ for _ in ()).throw(RuntimeError("gate pins local reviewer")),
        )
        dt = round(time.perf_counter() - t0, 1)
        after = {
            p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts
        }
        review_events = [r for r in rows if r.get("type") == "review"]
        task_done = next((r for r in reversed(rows) if r.get("type") == "task_done"), None)
        review = (task_done or {}).get("review")
        provider_path = directory / "provider.jsonl"
        provider_rows = [json.loads(line) for line in provider_path.read_text(encoding="utf-8").splitlines()] if provider_path.exists() else []
        requests = [row for row in provider_rows if row.get("type") == "request"]
        from codey.reviews.persistence import ReviewArtifactStore, load_review_artifact
        from codey.runs.ledger import RunLedgerStore
        from codey.runs.ledger_projection import load_run_projection

        projection = load_run_projection(RunLedgerStore(state_home), result.session_id, result.run_id)
        durable = projection.review if projection is not None else None
        restored = None
        if durable is not None and durable.artifact_sha256:
            restored = load_review_artifact(
                ReviewArtifactStore(state_home), session_id=result.session_id, run_id=result.run_id,
                attempt_id=durable.attempt_id, expected_sha256=durable.artifact_sha256,
            )
        ok = (
            task_done is not None and result.exit_code == 0
            and task_done.get("stop_reason") == "done"
            and bool(requests) and all(row["payload"].get("model") == target.model for row in requests)
            and isinstance(review, dict) and review.get("status") == "complete"
            and restored is not None and restored.is_complete
            and durable is not None and review.get("attempt_id") == durable.attempt_id
            and review.get("verdict") == restored.verdict
            and review.get("finding_count") == len(restored.findings)
            and review.get("artifact_sha256") == durable.artifact_sha256
            and after == baseline
            and not any(row.get("type") in {"file_changed", "command_verified"} for row in rows)
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
            "review_requests": len(requests),
            "review_status": review.get("status") if isinstance(review, dict) else "missing",
            "failure_stage": "" if ok else "review_smoke",
        }
    except Exception as exc:  # noqa: BLE001
        data = {"case": "review", "ok": False, "failure_stage": "agent_exception",
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        (directory / "events.jsonl").touch(exist_ok=True)
    return data
