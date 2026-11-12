"""Compare autonomous acquisition with one runtime-scheduled required source attempt."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import patch

from codey.operations import kernel_protocol
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_protocol import KernelSessionView, TurnSnapshot
from codey.operations.source_gaps import task_source_coverage
from codey.operations.task_session import TaskSession
from codey.policies.network import check_fetch_url
from codey.runtime.core import cancellation
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import browser_production_live_gate as gate
from tools import protocol_surface
from tools import receipt_bound_edit_ab as edit
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import Target, provider_metrics


def eligible_source(session: TaskSession, snapshot: TurnSnapshot) -> str | None:
    if (session.task_kind != "chat" or session.policy.strict_research or not session.policy.sources_open_required
            or not session.policy.allows("web.read") or len(session.required_source_urls) != 1
            or "codemode" not in snapshot.tool_names or "open_url" not in snapshot.nested_tool_names):
        return None
    url = next(iter(session.required_source_urls))
    if check_fetch_url(url, resolve=False) or task_source_coverage(session).missing != (source_url_key(url),):
        return None
    return url


def first_attempt(current_url: Callable[[], str | None]) -> Callable[[list[EffectReceipt]], JsonObject | None]:
    checked = False

    def schedule(receipts: list[EffectReceipt]) -> JsonObject | None:
        nonlocal checked
        if checked:
            return None
        checked = True
        url = current_url()
        if url is None:
            return None
        if any(row.result.call.name == "open_url" and isinstance(previous := row.result.call.args.get("url"), str)
               and source_url_key(previous) == source_url_key(url) for row in receipts):
            return None
        return {"tool": "codemode", "args": {"code": "return await tools.open_url({url:inputs.url});",
                                             "inputs": {"url": url}}}

    return schedule


def run_arm(directory: Path, arm: str, status: int, target: Target, *, max_turns: int) -> JsonObject:
    before = edit.production_source_digest()
    current: str | None = None
    build = kernel_protocol.build_turn_snapshot

    def capture(session: KernelSessionView, *, native: bool = False) -> TurnSnapshot:
        nonlocal current
        snapshot = build(session, native=native)
        if isinstance(session, TaskSession):
            current = eligible_source(session, snapshot)
        return snapshot

    schedule = first_attempt(lambda: current) if arm == "B" else None
    with (patch.object(kernel_protocol, "build_turn_snapshot", capture),
          patch.object(KernelContext, "prefetch_required_sources", return_value=False),
          protocol_surface.prototype(directory, "source_failure", "A", runtime_action=schedule),
          patch("codey.app.provider_services.provider_tab_availability", return_value={}),
          patch("codey.app.review_service.run_review", return_value=None)):
        row = decode_json_object(gate.run_case(directory, "chat", target, max_turns=max_turns, source_failure=True,
            source_attempt_only=True, source_status=status,
            task_transform=lambda task: task + "若读取成功，请在 done 中报告正文里 Required source record 的实际值。"))

    receipts = tool_receipts(directory / "state")
    calls = [receipt for receipt in receipts if receipt.result.call.name == "open_url"]
    provider_log = directory / "provider.jsonl"
    log = [decode_json_object(json.loads(line)) for line in provider_log.read_text(encoding="utf-8").splitlines()]
    actions = [item for item in log if item["type"] == "runtime_action"]
    requests = [item for item in log if item.get("phase") == "request"]
    first_wire = json.dumps(requests[0]["payload"], ensure_ascii=False) if requests else ""
    marker = gate.SOURCE_SUCCESS_MARKER if status == 200 else "HTTP 403"
    # Both the specific receipt reference and the actual content/error must reach the model.
    first_delivered = len(calls) == 1 and calls[0].identity in first_wire and marker in first_wire
    # Later delivery applies to autonomous reads, which happen after the first request.
    delivered = len(calls) == 1 and any(
        calls[0].identity in wire and marker in wire
        for request in requests if (wire := json.dumps(request["payload"], ensure_ascii=False)))
    stable = before == edit.production_source_digest()
    urls: list[JsonValue] = [str(receipt.result.call.args.get("url")) for receipt in calls]
    row.update(arm=arm, http_status=status, source=before, source_stable=stable,
               source_attempts=len(calls), source_urls=urls,
               source_results_delivered=delivered, first_request_has_source_result=first_delivered,
               runtime_actions=len(actions), **provider_metrics(provider_log))
    row["passed"] = (row["passed"] is True and stable and delivered and len(calls) == 1
                     and calls[0].result.call.args.get("url") == gate.URL and calls[0].result.ok == (status == 200)
                     and (arm == "A" or (len(actions) == 1 and first_delivered and actions[0]["exchange"] == 1)))
    edit.write_json(directory / "summary.json", row)
    return row


def run_worker(directory: Path, arm: str, status: int, target: Target, *, budget: float, max_turns: int) -> JsonObject:
    command = [sys.executable, "-m", "tools.required_source_prefetch_ab", "--run-dir", str(directory),
               "--arm", arm, "--status", str(status), "--expected-model", target.model, "--timeout", str(target.timeout),
               "--kobold-base-url", target.base_url, "--max-turns", str(max_turns)]
    process, owner = cancellation.start_process(command, cwd=edit.ROOT)
    error, output = "", ""
    try:
        completed = cancellation.wait_process(process, owner, command, budget, capture_limit_bytes=100_000)
        output = completed.stdout + completed.stderr
        if completed.returncode:
            error = f"worker exited {completed.returncode}"
    except TimeoutExpired:
        error = f"worker exceeded {budget}s"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "worker.log").write_text(output + error, encoding="utf-8")
    summary = directory / "summary.json"
    if not error and summary.exists():
        return decode_json_object(json.loads(summary.read_text(encoding="utf-8")))
    return {"arm": arm, "http_status": status, "passed": False, "error": error or "missing worker summary"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--budget", type=float, default=300)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--arm", choices=("A", "B"), help=argparse.SUPPRESS)
    parser.add_argument("--status", type=int, choices=(200, 403), default=403, help=argparse.SUPPRESS)
    parser.add_argument("--expected-model", help=argparse.SUPPRESS)
    args = parser.parse_args()
    target = gate.select_target(args.timeout, args.kobold_base_url)
    root = args.run_dir.resolve()
    if args.arm:
        if args.expected_model != target.model:
            parser.error("worker model differs from the matrix model")
        run_arm(root, args.arm, args.status, target, max_turns=args.max_turns)
        return 0
    root.mkdir(parents=True)
    source = edit.production_source_digest()
    paths = (Path(__file__), Path(gate.__file__), Path(protocol_surface.__file__))
    hashes: JsonObject = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    manifest: JsonObject = {"model": target.model, "seed": target.seed, "temperature": 0, "source": source,
                           "harness_sha256": hashes, "max_turns": args.max_turns, "timeout": args.timeout,
                           "budget": args.budget}
    edit.write_json(root / "manifest.json", manifest)
    rows: list[JsonObject] = []
    tasks: list[bytes] = []
    for index, (status, order) in enumerate(((403, "AB"), (403, "BA"), (200, "AB")), 1):
        for arm in order:
            directory = root / f"{index:02d}-{status}-{arm}"
            print(f"Starting {index}/{status}/{arm}", flush=True)
            row = run_worker(directory, arm, status, target, budget=args.budget, max_turns=args.max_turns)
            rows.append({"pair": index, **row})
            task = directory / "task.txt"
            if task.exists():
                tasks.append(task.read_bytes())
            print(f"{index}/{status}/{arm}: passed={row['passed']} stop={row.get('stop_reason')} "
                  f"calls={row.get('model_calls')} error={row.get('error')}", flush=True)
            edit.write_json(root / "progress.json", list(rows))
    same_task = len(tasks) == len(rows) and len(set(tasks)) == 1
    stable = (source == edit.production_source_digest() and all(row.get("source_stable") is True for row in rows)
              and hashes == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})
    edit.write_json(root / "summary.json", {**manifest, "runs": list(rows), "same_task": same_task, "sources_stable": stable})
    return 0 if stable and same_task and all(row["passed"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
