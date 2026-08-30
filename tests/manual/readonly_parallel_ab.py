"""A/B probe for a narrow read-only tool concurrency slice.

The deterministic arm keeps the old serial execution shape as a local baseline
and compares it with a script-local read/ls/search concurrent prototype. Codey
does not enable that prototype by default: the production Agent keeps serial
tool events because visible step-by-step progress is part of its local
developer-tool feel. Use this probe to preserve the evidence for that tradeoff.

The optional live smoke asks a real web provider to use read_files or parallel
with bounded provider timeouts. It checks whether providers actually emit
batch calls and whether provider blockers occur; it is not required for local
production concurrency validation.
"""

# ruff: noqa: E402 - direct script execution must add the repository root first.

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.agents import runner as agent
from codey.agents.request import AgentRequest
from codey.providers import controls as provider_controls
from codey.runtime.models import ToolCall
from codey.protocols.json_codec import JsonToolCodec
from codey.providers.registry import connect_provider, provider_ids
from codey.toolchain.runtime import ToolOutcome

DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-readonly-parallel-ab.json"
PARALLEL_READONLY_TOOL_NAMES = frozenset({"read", "ls", "search"})
DEFAULT_WORKERS = 4
ARMS = ("serial", "concurrent")
LIVE_MARKERS = ("MARKER_ALPHA", "MARKER_BRAVO", "MARKER_CHARLIE", "MARKER_DELTA")


def run_agent(provider, project, task, **kwargs):
    return agent.run(AgentRequest(provider=provider, project=Path(project), task=task, **kwargs))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, OSError):
    pass


@dataclass(frozen=True)
class ToolRunRecord:
    index: int
    call: ToolCall
    outcome: ToolOutcome
    start: float
    end: float
    worker: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class DeterministicCase:
    name: str
    calls: tuple[ToolCall, ...]
    existing_files: frozenset[str] = frozenset()


class SleepyToolFns:
    """Deterministic local tool doubles with visible latency."""

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def _record(self, name: str, detail: str) -> None:
        time.sleep(self.delay)
        with self.lock:
            self.calls.append((name, detail))

    def read_file(self, _root: Path, rel: str, **_options: object) -> ToolOutcome:
        self._record("read", rel)
        return ToolOutcome(f"{rel}: VALUE = {rel.replace('.', '_')}\n", True)

    def list_directory(self, _root: Path, rel: str) -> ToolOutcome:
        self._record("ls", rel)
        return ToolOutcome("a.py\nb.py\nc.py\nd.py", True)

    def search_files(self, _root: Path, rel: str, query: str) -> ToolOutcome:
        self._record("search", f"{rel}:{query}")
        return ToolOutcome(f"{rel}/a.py:1: {query}", True)

    def find_references(self, _root: Path, rel: str, symbol: str) -> ToolOutcome:
        self._record("references", f"{rel}:{symbol}")
        return ToolOutcome(f"References for {symbol} under {rel}:", True)


class TimeoutCountingProvider:
    """Provider wrapper that applies default timeouts even when Agent passes none."""

    def __init__(
        self,
        provider,
        *,
        send_timeout: float,
        new_chat_timeout: float,
    ) -> None:
        self.provider = provider
        self.name = provider.name
        self.send_timeout = send_timeout
        self.new_chat_timeout = new_chat_timeout
        self.sends = 0
        self.sent_chars = 0
        self.reply_chars = 0
        self.provider_seconds = 0.0

    @property
    def location(self) -> str:
        return getattr(self.provider, "location", "")

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def new_chat(self, timeout: float | None = None) -> None:
        effective = self.new_chat_timeout if timeout is None else timeout
        self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout: float | None = None) -> str:
        effective = self.send_timeout if timeout is None else timeout
        self.sends += 1
        self.sent_chars += len(text or "")
        started = time.monotonic()
        reply = self.provider.send(text, timeout=effective)
        self.provider_seconds += time.monotonic() - started
        self.reply_chars += len(reply or "")
        return reply

    def close(self) -> None:
        self.provider.close()


def _parse_payload(payload: str) -> tuple[ToolCall, ...]:
    plan = JsonToolCodec().parse(payload)
    if plan.protocol_error:
        raise ValueError(plan.protocol_error)
    return tuple(plan.calls)


def deterministic_cases() -> tuple[DeterministicCase, ...]:
    return (
        DeterministicCase(
            "read_files_4",
            _parse_payload(
                '{"tool":"read_files","args":{"paths":["a.py","b.py","c.py","d.py"]}}'
            ),
        ),
        DeterministicCase(
            "parallel_mixed",
            _parse_payload(
                '{"tool":"parallel","args":{"calls":['
                '{"tool":"read_file","args":{"path":"a.py"}},'
                '{"tool":"list_dir","args":{"path":"."}},'
                '{"tool":"grep","args":{"query":"MARKER","path":"."}}'
                ']}}'
            ),
        ),
        DeterministicCase(
            "flush_before_edit",
            (
                ToolCall("read", {"path": "app.py"}),
                ToolCall("read", {"path": "helper.py"}),
                ToolCall(
                    "edit",
                    {
                        "path": "app.py",
                        "replacements": [{"search": "old", "replace": "new"}],
                    },
                ),
            ),
            existing_files=frozenset({"app.py", "helper.py"}),
        ),
        DeterministicCase(
            "references_boundary",
            (
                ToolCall("search", {"path": ".", "query": "SessionStore"}),
                ToolCall("references", {"path": ".", "symbol": "SessionStore"}),
                ToolCall("read", {"path": "session.py"}),
            ),
        ),
    )


class DeterministicBatchRunner:
    """Script-local prototype for the read-only concurrency tradeoff."""

    def __init__(
        self,
        *,
        arm: str,
        root: Path,
        tools: SleepyToolFns,
        existing_files: frozenset[str],
        max_workers: int,
    ) -> None:
        if arm not in ARMS:
            raise ValueError(f"unknown arm: {arm}")
        self.arm = arm
        self.root = root
        self.tools = tools
        self.existing_files = existing_files
        self.max_workers = max(1, max_workers)
        self.known_files: set[str] = set()

    def run(self, calls: tuple[ToolCall, ...]) -> list[ToolRunRecord]:
        records: list[ToolRunRecord] = []
        pending: list[tuple[int, ToolCall]] = []

        def flush() -> None:
            if not pending:
                return
            batch = tuple(pending)
            pending.clear()
            if self.arm == "concurrent" and len(batch) > 1:
                batch_records = self._run_parallel_batch(batch)
            else:
                batch_records = [self._run_one(index, call) for index, call in batch]
            for record in batch_records:
                records.append(record)
                self._apply_ordered_effect(record)

        for index, call in enumerate(calls):
            if call.name in PARALLEL_READONLY_TOOL_NAMES:
                pending.append((index, call))
                continue
            flush()
            record = self._run_one(index, call)
            records.append(record)
            self._apply_ordered_effect(record)
        flush()
        return records

    def _run_parallel_batch(
        self,
        batch: tuple[tuple[int, ToolCall], ...],
    ) -> list[ToolRunRecord]:
        workers = min(self.max_workers, len(batch))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            records = list(executor.map(lambda item: self._run_one(*item), batch))
        return sorted(records, key=lambda item: item.index)

    def _run_one(self, index: int, call: ToolCall) -> ToolRunRecord:
        start = time.monotonic()
        try:
            outcome = self._execute(call)
        except Exception as exc:  # pragma: no cover - defensive parity with Agent.
            outcome = ToolOutcome.error(str(exc))
        end = time.monotonic()
        return ToolRunRecord(
            index=index,
            call=call,
            outcome=outcome,
            start=start,
            end=end,
            worker=threading.current_thread().name,
        )

    def _execute(self, call: ToolCall) -> ToolOutcome:
        path = str(call.args.get("path") or ".")
        if call.name == "read":
            read_options = {
                name: call.args[name]
                for name in ("offset", "limit")
                if name in call.args
            }
            return self.tools.read_file(self.root, path, **read_options)
        if call.name == "ls":
            return self.tools.list_directory(self.root, path)
        if call.name == "search":
            return self.tools.search_files(
                self.root,
                path,
                str(call.args.get("query") or ""),
            )
        if call.name == "references":
            return self.tools.find_references(
                self.root,
                path,
                str(call.args.get("symbol") or ""),
            )
        if call.name == "edit":
            if path in self.existing_files and path not in self.known_files:
                return ToolOutcome.error(
                    f"read_file required before editing existing file: {path}"
                )
            return ToolOutcome(f"edited {path} (1 replacement)", True, changed=True)
        return ToolOutcome.error(f"unsupported probe tool: {call.name}")

    def _apply_ordered_effect(self, record: ToolRunRecord) -> None:
        if record.call.name == "read" and record.outcome.ok:
            self.known_files.add(str(record.call.args.get("path") or "."))
        if record.call.name == "edit" and record.outcome.ok and record.outcome.changed:
            self.known_files.add(str(record.call.args.get("path") or "."))


def _overlaps(left: ToolRunRecord, right: ToolRunRecord, *, epsilon: float = 0.002) -> bool:
    return max(left.start, right.start) + epsilon < min(left.end, right.end)


def _case_flags(case: DeterministicCase, records: list[ToolRunRecord]) -> dict[str, bool]:
    by_name = {record.call.name: record for record in records}
    flags = {
        "result_order_ok": [record.index for record in records] == list(range(len(records))),
        "all_outcomes_ok": all(record.outcome.ok for record in records),
        "read_unlock_ok": True,
        "references_non_overlapping": True,
    }
    if case.name == "flush_before_edit":
        edit = by_name.get("edit")
        flags["read_unlock_ok"] = bool(edit and edit.outcome.ok)
    reference_records = [record for record in records if record.call.name == "references"]
    if reference_records:
        reference = reference_records[0]
        flags["references_non_overlapping"] = not any(
            other is not reference and _overlaps(reference, other)
            for other in records
        )
    return flags


def _run_deterministic_once(
    case: DeterministicCase,
    arm: str,
    *,
    delay: float,
    max_workers: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codey-readonly-parallel-ab-") as td:
        root = Path(td)
        tools = SleepyToolFns(delay)
        runner = DeterministicBatchRunner(
            arm=arm,
            root=root,
            tools=tools,
            existing_files=case.existing_files,
            max_workers=max_workers,
        )
        started = time.monotonic()
        records = runner.run(case.calls)
        elapsed = time.monotonic() - started
    total_tool_seconds = sum(record.duration for record in records)
    trace = [
        {
            "index": record.index,
            "tool": record.call.name,
            "args": record.call.args,
            "ok": record.outcome.ok,
            "start_ms": round((record.start - started) * 1000, 2),
            "end_ms": round((record.end - started) * 1000, 2),
            "duration_ms": round(record.duration * 1000, 2),
            "worker": record.worker,
        }
        for record in records
    ]
    return {
        "case": case.name,
        "arm": arm,
        "elapsed_seconds": elapsed,
        "sum_tool_seconds": total_tool_seconds,
        "overlap_seconds": max(0.0, total_tool_seconds - elapsed),
        "flags": _case_flags(case, records),
        "trace": trace,
    }


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def run_deterministic(
    *,
    repeats: int = 5,
    delay: float = 0.2,
    max_workers: int = DEFAULT_WORKERS,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    cases = deterministic_cases()
    for case in cases:
        for arm in ARMS:
            samples = [
                _run_deterministic_once(
                    case,
                    arm,
                    delay=delay,
                    max_workers=max_workers,
                )
                for _ in range(max(1, repeats))
            ]
            rows.append({
                "case": case.name,
                "arm": arm,
                "runs": len(samples),
                "median_elapsed_seconds": round(
                    _median([float(item["elapsed_seconds"]) for item in samples]),
                    4,
                ),
                "median_overlap_seconds": round(
                    _median([float(item["overlap_seconds"]) for item in samples]),
                    4,
                ),
                "flags": {
                    key: all(bool(item["flags"][key]) for item in samples)
                    for key in samples[0]["flags"]
                },
                "sample_trace": samples[-1]["trace"],
            })

    by_case_arm = {(row["case"], row["arm"]): row for row in rows}
    comparisons = []
    for case in cases:
        serial = by_case_arm[(case.name, "serial")]
        concurrent = by_case_arm[(case.name, "concurrent")]
        serial_elapsed = float(serial["median_elapsed_seconds"])
        concurrent_elapsed = float(concurrent["median_elapsed_seconds"])
        improvement = (
            1.0 - (concurrent_elapsed / serial_elapsed)
            if serial_elapsed > 0
            else 0.0
        )
        comparisons.append({
            "case": case.name,
            "serial_median_seconds": serial_elapsed,
            "concurrent_median_seconds": concurrent_elapsed,
            "improvement_ratio": round(improvement, 3),
            "meaningful_speedup": improvement >= 0.35
            if case.name in {"read_files_4", "parallel_mixed", "flush_before_edit"}
            else True,
            "correctness_ok": all(concurrent["flags"].values()),
        })
    return {
        "kind": "deterministic",
        "delay_seconds": delay,
        "repeats": repeats,
        "max_workers": max_workers,
        "rows": rows,
        "comparisons": comparisons,
        "ok": all(item["correctness_ok"] and item["meaningful_speedup"] for item in comparisons),
    }


def _write_live_project(root: Path) -> None:
    for name, marker in (
        ("a.py", "MARKER_ALPHA"),
        ("b.py", "MARKER_BRAVO"),
        ("c.py", "MARKER_CHARLIE"),
        ("d.py", "MARKER_DELTA"),
    ):
        (root / name).write_text(f'VALUE = "{marker}"\n', encoding="utf-8")


def _live_task(case: str) -> str:
    if case == "parallel":
        return (
            "Smoke test read-only batching. First call exactly one parallel tool "
            "containing read_file for a.py, list_dir for ., and grep for MARKER. "
            "After the local tool results arrive, summarize the marker values. "
            "Do not edit files and do not run commands."
        )
    if case == "read_files":
        return (
            "Smoke test read-only batching. First call exactly one read_files tool "
            "for a.py, b.py, c.py, and d.py. After the local tool results arrive, "
            "summarize MARKER_ALPHA, MARKER_BRAVO, MARKER_CHARLIE, and MARKER_DELTA. "
            "Do not edit files and do not run commands."
        )
    raise ValueError(f"unknown live case: {case}")


def _tool_trace(events: list[agent.RunEvent]) -> list[dict[str, Any]]:
    trace = []
    for event in events:
        if event.kind != "tool" or event.call is None or event.outcome is None:
            continue
        trace.append({
            "turn": event.turn,
            "tool": event.call.name,
            "args": event.call.args,
            "ok": event.outcome.ok,
            "truncated": event.outcome.truncated,
            "first_line": event.outcome.presentation_result(120),
        })
    return trace


def _live_expected_order(case: str) -> list[tuple[str, str]]:
    if case == "parallel":
        return [("read", "a.py"), ("ls", "."), ("search", ".")]
    return [("read", "a.py"), ("read", "b.py"), ("read", "c.py"), ("read", "d.py")]


def _live_order_ok(case: str, trace: list[dict[str, Any]]) -> bool:
    expected = _live_expected_order(case)
    observed = [
        (str(item["tool"]), str(item["args"].get("path") or "."))
        for item in trace[: len(expected)]
    ]
    return observed == expected


def run_live_provider(
    provider_id: str,
    *,
    live_case: str,
    port: int,
    max_turns: int,
    send_timeout: float,
    new_chat_timeout: float,
    open_if_missing: bool,
) -> dict[str, Any]:
    provider_controls.begin_task_context(f"readonly-parallel-ab:{provider_id}:{live_case}")
    raw = None
    events = []
    started = time.monotonic()
    try:
        raw = connect_provider(
            provider_id,
            port=port,
            open_if_missing=open_if_missing,
            bring_to_front=open_if_missing,
        )
        provider = TimeoutCountingProvider(
            raw,
            send_timeout=send_timeout,
            new_chat_timeout=new_chat_timeout,
        )
        with tempfile.TemporaryDirectory(prefix="codey-readonly-parallel-live-") as td:
            root = Path(td)
            _write_live_project(root)
            result = run_agent(
                provider,
                root,
                _live_task(live_case),
                max_turns=max_turns,
                on_event=events.append,
                fresh_chat=True,
                project_map="",
                provider_id=provider_id,
            )
        trace = _tool_trace(events)
        elapsed = time.monotonic() - started
        return {
            "provider": provider_id,
            "case": live_case,
            "ok": result.stop_reason == "done" and _live_order_ok(live_case, trace),
            "eligible": len(trace) >= len(_live_expected_order(live_case)),
            "stop_reason": result.stop_reason,
            "summary_has_all_markers": all(
                marker in result.summary for marker in LIVE_MARKERS
            ),
            "order_ok": _live_order_ok(live_case, trace),
            "elapsed_seconds": round(elapsed, 3),
            "provider_seconds": round(provider.provider_seconds, 3),
            "local_plus_overhead_seconds": round(
                max(0.0, elapsed - provider.provider_seconds),
                3,
            ),
            "sends": provider.sends,
            "sent_chars": provider.sent_chars,
            "reply_chars": provider.reply_chars,
            "trace": trace,
            "summary": result.summary[:800],
        }
    except Exception as exc:
        trace = _tool_trace(events)
        failure = getattr(exc, "failure", None)
        failure_payload = (
            failure.to_dict()
            if failure is not None and hasattr(failure, "to_dict")
            else None
        )
        return {
            "provider": provider_id,
            "case": live_case,
            "ok": False,
            "eligible": len(trace) >= len(_live_expected_order(live_case)),
            "order_ok": _live_order_ok(live_case, trace),
            "summary_has_all_markers": False,
            "error_type": type(exc).__name__,
            "error": str(exc)[:800],
            "provider_failure": failure_payload,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "trace": trace,
        }
    finally:
        try:
            if raw is not None:
                raw.close()
        finally:
            provider_controls.end_task_context()


def run_live(
    providers: tuple[str, ...],
    *,
    live_case: str,
    port: int,
    max_turns: int,
    send_timeout: float,
    new_chat_timeout: float,
    open_if_missing: bool,
) -> dict[str, Any]:
    rows = [
        run_live_provider(
            provider_id,
            live_case=live_case,
            port=port,
            max_turns=max_turns,
            send_timeout=send_timeout,
            new_chat_timeout=new_chat_timeout,
            open_if_missing=open_if_missing,
        )
        for provider_id in providers
    ]
    return {
        "kind": "live",
        "case": live_case,
        "providers": list(providers),
        "send_timeout": send_timeout,
        "new_chat_timeout": new_chat_timeout,
        "open_if_missing": open_if_missing,
        "rows": rows,
        "ok": all(bool(row.get("ok")) for row in rows),
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    existing: list[Any] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                existing = loaded
        except (OSError, ValueError):
            existing = []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([*existing, report], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _selected_providers(value: str) -> tuple[str, ...]:
    if value == "all":
        return provider_ids()
    selected = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    known = set(provider_ids())
    unknown = [item for item in selected if item not in known]
    if unknown:
        raise ValueError(f"unknown provider(s): {', '.join(unknown)}")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-deterministic", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--provider", default="all")
    parser.add_argument("--live-case", choices=("read_files", "parallel"), default="read_files")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--send-timeout", type=float, default=75.0)
    parser.add_argument("--new-chat-timeout", type=float, default=25.0)
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--deterministic-repeats", type=int, default=5)
    parser.add_argument("--deterministic-delay", type=float, default=0.2)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--strict-live", action="store_true")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "probe": "readonly_parallel_ab",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    exit_ok = True
    if not args.skip_deterministic:
        deterministic = run_deterministic(
            repeats=args.deterministic_repeats,
            delay=args.deterministic_delay,
            max_workers=args.workers,
        )
        report["deterministic"] = deterministic
        exit_ok = exit_ok and bool(deterministic["ok"])
    if args.live:
        live = run_live(
            _selected_providers(args.provider),
            live_case=args.live_case,
            port=args.port,
            max_turns=args.max_turns,
            send_timeout=args.send_timeout,
            new_chat_timeout=args.new_chat_timeout,
            open_if_missing=args.open_if_missing,
        )
        report["live"] = live
        if args.strict_live:
            exit_ok = exit_ok and bool(live["ok"])

    _write_report(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if exit_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
