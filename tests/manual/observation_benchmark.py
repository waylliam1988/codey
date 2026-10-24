"""Compare unchanged receipt paths on two checkouts; no model/network timing.

Run this identical file in each checkout, serially on an idle machine. Timings
include real SQLite settlement and managed receipt I/O. Long-body recovery may
fail on the old checkout; errors remain part of the recorded result.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from codey.operations.kernel_facts import record_facts_for_result
from codey.operations.recovery import rebuild_settled_tool_result
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.storage.managed_outputs import ManagedOutputStore
from tests.test_settled_delivery_recovery import _accept_and_mark, _new_log


def sample(root: Path, size: int) -> dict:
    _, mutations, durable = _new_log(root, "s", "r")
    _accept_and_mark(mutations, "s", "r", str(root))
    managed = ManagedOutputStore(root / "state")
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local",
                            managed_outputs=managed, durable=durable)
    url, body = "https://example.com/observed", "a" * size
    call = ToolCall("open_url", {"url": url}, call_id="source-1")
    started = time.perf_counter()
    ledger = ResearchLedger()
    ledger.record_open(url, url, "Benchmark", body)
    result = ToolResult(call, body[:512], ok=True,
                        canonical={"research_observation": ledger_observation(ledger, "open", url=url)})
    built = time.perf_counter()
    sink.begin_turn([("source-effect", call, 0)], turn=1)
    sink.settle("source-effect", True, result=result)
    settled = time.perf_counter()
    rows = RuntimeEffectStore(durable=durable).load_effects("s", "r")
    restored = rebuild_settled_tool_result(rows[0], managed_store=managed, session_id="s", run_id="r")
    fresh, recovered = TaskSession(policy=None), ResearchLedger()
    error = ""
    try:
        record_facts_for_result(fresh, restored.call, restored, ok=True)
        restore_ledger_observation(recovered, restored.canonical["research_observation"])
        assert recovered.source_text_for_url(url) == body
    except (ValueError, AssertionError) as exc:
        error = repr(exc)
    finished = time.perf_counter()
    return {"build_ms": (built - started) * 1000, "settle_ms": (settled - built) * 1000,
            "recover_ms": (finished - settled) * 1000, "total_ms": (finished - started) * 1000,
            "error": error, "managed_bytes": sum(p.stat().st_size for p in root.rglob("*")
                                                   if p.is_file() and p.suffix != ".sqlite3")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()
    report = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "cases": {}}
    with tempfile.TemporaryDirectory(prefix="codey-observation-benchmark-") as directory:
        for size in (512, 65536):
            rows = [sample(Path(directory) / f"{size}-{index}", size) for index in range(args.samples + 3)]
            rows = rows[3:]  # identical warmup in both checkouts
            report["cases"][str(size)] = {
                "samples": rows,
                "median": {key: statistics.median(row[key] for row in rows)
                           for key in ("build_ms", "settle_ms", "recover_ms", "total_ms", "managed_bytes")},
                "failed_recoveries": sum(bool(row["error"]) for row in rows),
            }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: {k: v for k, v in value.items() if k != "samples"}
                      for key, value in report["cases"].items()}, indent=2))


if __name__ == "__main__":
    main()
