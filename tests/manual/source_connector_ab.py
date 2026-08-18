"""Live A/B probe for PubMed/arXiv connector-aware Research search.

The probe runs the production ResearchRunner against live web providers. The
baseline arm uses the browser search provider directly; the connector arm wraps
it with ConnectorAwareSearchProvider. Progress is written atomically after each
case/arm row so missing samples can be resumed without rerunning completed
provider traffic. An optional trace file records each prompt/reply pair
atomically so repeated done attempts can be replayed later.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.knowledge.store import KnowledgeStore
from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.proof_quality import review_research_proof
from codey.research.protocols import extract_json_objects
from codey.research.runner import ResearchRunner
from codey.providers.registry import connect_provider, provider_ids

RESULTS_DIR = Path(__file__).resolve().parent / "results"
WEB_PROVIDERS = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
ARMS = ("baseline", "connector")
MAX_RESULT_BYTES = 1024 * 1024
MAX_TRACE_BYTES = 1024 * 1024
TRACE_PROMPT_CHARS = 6000
TRACE_REPLY_CHARS = 6000


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    target_hosts: tuple[str, ...]
    expected_terms: tuple[str, ...]


CASES = {
    "pubmed": Case(
        name="pubmed",
        question=(
            "Research the current biomedical evidence on immune checkpoint "
            "inhibitor hepatotoxicity clinical management. Use opened sources, "
            "save exact evidence, then produce a concise cited report."
        ),
        target_hosts=("pubmed.ncbi.nlm.nih.gov",),
        expected_terms=("hepatotoxicity", "immune", "checkpoint"),
    ),
    "arxiv": Case(
        name="arxiv",
        question=(
            "Research arXiv or preprint evidence about retrieval augmented "
            "generation evaluation methods. Use opened sources, save exact "
            "evidence, then produce a concise cited report."
        ),
        target_hosts=("arxiv.org",),
        expected_terms=("retrieval", "augmented", "generation"),
    ),
    "open_guard": Case(
        name="open_guard",
        question=(
            "Research whether a citable source supports the claim that opened "
            "web sources are required before evidence can be saved. Do not cite "
            "or save evidence unless the source was opened in this run."
        ),
        target_hosts=(),
        expected_terms=("opened", "source", "evidence"),
    ),
}


class TimedProvider:
    def __init__(self, provider, *, send_timeout: float, new_chat_timeout: float) -> None:
        self.provider = provider
        self.send_timeout = max(1.0, float(send_timeout))
        self.new_chat_timeout = max(1.0, float(new_chat_timeout))
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")

    def new_chat(self, timeout=None) -> None:
        effective = self.new_chat_timeout if timeout is None else timeout
        return self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout=None) -> str:
        effective = self.send_timeout if timeout is None else timeout
        return self.provider.send(text, timeout=effective)

    def close(self) -> None:
        return self.provider.close()


class LiveTrace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.monotonic()
        self.started_at = _timestamp()
        self.events: list[dict[str, Any]] = []

    def record(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.events.append(payload)
        self.flush()

    def record_run_start(
        self,
        *,
        run_id: str,
        provider: str,
        trace_output: str,
        cases: tuple[str, ...],
        arms: tuple[str, ...],
        max_turns: int,
    ) -> None:
        self.record({
            "event": "run_start",
            "run_id": run_id,
            "provider": provider,
            "trace_output": trace_output,
            "cases": list(cases),
            "arms": list(arms),
            "max_turns": max_turns,
        })

    def record_case_start(
        self,
        *,
        run_id: str,
        provider: str,
        case: str,
        arm: str,
        question: str,
    ) -> None:
        self.record({
            "event": "case_start",
            "run_id": run_id,
            "provider": provider,
            "case": case,
            "arm": arm,
            "question": _clip(question, 1200),
        })

    def record_send_start(
        self,
        *,
        run_id: str,
        provider: str,
        provider_name: str,
        case: str,
        arm: str,
        turn: int,
        prompt: str,
    ) -> None:
        self.record({
            "event": "send_start",
            "run_id": run_id,
            "provider": provider,
            "provider_name": provider_name,
            "case": case,
            "arm": arm,
            "turn": turn,
            "prompt_chars": len(prompt or ""),
            "prompt": _clip(prompt, TRACE_PROMPT_CHARS),
        })

    def record_reply(
        self,
        *,
        run_id: str,
        provider: str,
        provider_name: str,
        case: str,
        arm: str,
        turn: int,
        prompt: str,
        reply: str,
    ) -> None:
        self.record({
            "event": "reply",
            "run_id": run_id,
            "provider": provider,
            "provider_name": provider_name,
            "case": case,
            "arm": arm,
            "turn": turn,
            "prompt_chars": len(prompt or ""),
            "reply_chars": len(reply or ""),
            "prompt": _clip(prompt, TRACE_PROMPT_CHARS),
            "reply": _clip(reply, TRACE_REPLY_CHARS),
        })

    def record_case_complete(
        self,
        *,
        run_id: str,
        provider: str,
        case: str,
        arm: str,
        row: dict[str, Any],
    ) -> None:
        self.record({
            "event": "case_complete",
            "run_id": run_id,
            "provider": provider,
            "case": case,
            "arm": arm,
            "ok": bool(row.get("ok")),
            "stop_reason": str(row.get("stop_reason") or row.get("error") or ""),
            "turns": int(row.get("turns") or 0),
            "score": row.get("score"),
            "seconds": row.get("seconds"),
            "summary": _clip(row.get("summary_preview") or row.get("error") or "", 1200),
        })

    def record_run_complete(self, *, run_id: str, provider: str, rows: int) -> None:
        self.record({
            "event": "run_complete",
            "run_id": run_id,
            "provider": provider,
            "rows": rows,
        })

    def flush(self) -> None:
        payload = {
            "probe": "source_connector_ab_trace",
            "started_at": self.started_at,
            "updated_at": _timestamp(),
            "updated_elapsed_seconds": round(time.monotonic() - self.started, 3),
            "event_count": len(self.events),
            "events": self.events,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text.encode("utf-8")) > MAX_TRACE_BYTES:
            raise ValueError("source connector trace exceeded bounded size")
        _write_json_atomic(self.path, payload)


class TracingProvider:
    def __init__(
        self,
        provider,
        *,
        trace: LiveTrace,
        run_id: str,
        provider_id: str,
        provider_name: str,
        case: str,
        arm: str,
    ) -> None:
        self.provider = provider
        self.trace = trace
        self.run_id = run_id
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.case = case
        self.arm = arm
        self.send_index = 0
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")
        self.thread_safe_send = getattr(provider, "thread_safe_send", False)

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def new_chat(self, timeout=None) -> None:
        return self.provider.new_chat(timeout=timeout)

    def send(self, text: str, timeout=None) -> str:
        self.send_index += 1
        turn = self.send_index
        self.trace.record_send_start(
            run_id=self.run_id,
            provider=self.provider_id,
            provider_name=self.provider_name,
            case=self.case,
            arm=self.arm,
            turn=turn,
            prompt=text,
        )
        try:
            reply = self.provider.send(text, timeout=timeout)
        except Exception as exc:
            self.trace.record({
                "event": "send_error",
                "run_id": self.run_id,
                "provider": self.provider_id,
                "provider_name": self.provider_name,
                "case": self.case,
                "arm": self.arm,
                "turn": turn,
                "error": f"{type(exc).__name__}: {exc}",
            })
            raise
        self.trace.record_reply(
            run_id=self.run_id,
            provider=self.provider_id,
            provider_name=self.provider_name,
            case=self.case,
            arm=self.arm,
            turn=turn,
            prompt=text,
            reply=str(reply or ""),
        )
        return reply

    def close(self) -> None:
        return self.provider.close()


def run_case(
    provider,
    *,
    provider_id: str,
    case: Case,
    arm: str,
    max_turns: int,
    run_id: str,
    trace: LiveTrace | None,
) -> dict[str, Any]:
    started = time.time()
    tool_calls: list[dict[str, Any]] = []
    model_actions: list[dict[str, Any]] = []
    infos: list[str] = []
    provider_name = str(getattr(provider, "name", "") or "")
    with tempfile.TemporaryDirectory(prefix="codey-source-connector-ab-") as td:
        store = KnowledgeStore(Path(td))
        search = _search_provider_for_arm(arm)
        try:
            if trace is not None:
                trace.record_case_start(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    question=case.question,
                )
            run_provider = provider
            if trace is not None:
                run_provider = TracingProvider(
                    provider,
                    trace=trace,
                    run_id=run_id,
                    provider_id=provider_id,
                    provider_name=provider_name,
                    case=case.name,
                    arm=arm,
                )
            runner = ResearchRunner(
                run_provider,
                search,
                store,
                max_turns=max_turns,
                session_id=f"source-connector-ab-{provider_id}-{case.name}-{arm}",
                project="",
                run_id=run_id,
            )
            for event in runner.run(case.question):
                if event.kind == "turn":
                    model_actions.extend(_safe_model_actions(event.turn, event.reply)[:3])
                if event.kind == "info":
                    infos.append(str(event.message or "")[:240])
                if event.kind == "tool" and event.call is not None:
                    tool_calls.append({
                        "turn": event.turn,
                        "name": event.call.name,
                        "args": _safe_args(event.call.args),
                        "ok": bool(event.outcome.ok) if event.outcome is not None else False,
                        "status": event.outcome.presentation_status() if event.outcome is not None else "",
                    })
            if runner.result is None:
                raise RuntimeError("research finished without result")
            result = runner.result
            proof = (
                review_research_proof(result.research_record, question=case.question)
                if result.research_record is not None
                else None
            )
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "ok": True,
                "seconds": round(time.time() - started, 3),
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "sources_read": result.sources_read,
                "opened_urls": result.source_urls[:12],
                "opened_target_host": _opened_target_host(result.source_urls, case.target_hosts),
                "evidence_count": len(result.evidence_items),
                "notes_created": len(result.notes_created),
                "connector_errors": list(getattr(search, "last_connector_errors", []))[:8],
                "proof_ok": bool(proof.ok) if proof is not None else False,
                "proof_answer_status": proof.answer_status if proof is not None else "",
                "proof_missing_evidence": list(proof.missing_evidence[:8]) if proof is not None else [],
                "expected_terms_present": _expected_terms_present(result.summary, case.expected_terms),
                "summary_chars": len(result.summary or ""),
                "summary_preview": _clip(result.summary, 1200),
                "model_actions": model_actions[:40],
                "used_controller_open_action": any(
                    item.get("tool") in {"open_result", "reopen_source", "open_hit"}
                    for item in model_actions
                ),
                "used_legacy_open_url_id_action": any(
                    item.get("tool") == "open_url"
                    and any(key in item.get("args", {}) for key in ("result_id", "source_id", "hit_id"))
                    for item in model_actions
                ),
                "tool_calls": tool_calls[:40],
                "info": infos[:12],
            }
            row["score"] = _score_row(row)
            if trace is not None:
                trace.record_case_complete(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    row=row,
                )
            return row
        except Exception as exc:
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "ok": False,
                "seconds": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "model_actions": model_actions[:40],
                "tool_calls": tool_calls[:40],
                "info": infos[:12],
            }
            if trace is not None:
                trace.record_case_complete(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    row=row,
                )
            return row
        finally:
            try:
                search.close()
            except Exception:
                pass
            store.close()


def _search_provider_for_arm(arm: str):
    base = BrowserSearchProvider(isolated=False, bring_to_front=False)
    if arm == "connector":
        return ConnectorAwareSearchProvider(base)
    return base


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"query", "url", "source_id", "result_id", "hit_id", "pages", "type", "title"}:
            safe[str(key)] = _clip(value, 240)
        elif key in {"offset", "limit"}:
            safe[str(key)] = value
    return safe


def _safe_model_actions(turn: int, reply: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for obj in extract_json_objects(reply or ""):
        tool = str(obj.get("tool") or obj.get("name") or "").strip().lower()
        args = obj.get("args")
        if not isinstance(args, dict):
            args = {key: value for key, value in obj.items() if key not in ("tool", "name")}
        actions.append({
            "turn": int(turn or 0),
            "tool": _clip(tool, 80),
            "args": _safe_args(args),
        })
    if not actions and str(reply or "").strip():
        actions.append({"turn": int(turn or 0), "tool": "<no_json>", "args": {}})
    return actions


def _opened_target_host(urls: list[str], target_hosts: tuple[str, ...]) -> bool:
    if not target_hosts:
        return True
    targets = {host.lower().removeprefix("www.") for host in target_hosts}
    for url in urls:
        try:
            host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        except ValueError:
            host = ""
        if host in targets:
            return True
    return False


def _expected_terms_present(summary: str, expected_terms: tuple[str, ...]) -> bool:
    text = str(summary or "").casefold()
    return all(term.casefold() in text for term in expected_terms)


def _score_row(row: dict[str, Any]) -> int:
    return (
        (3 if row.get("stop_reason") == "done" else 0)
        + (3 if row.get("opened_target_host") else 0)
        + (2 if int(row.get("evidence_count") or 0) > 0 else 0)
        + (2 if row.get("proof_ok") else 0)
        + (1 if row.get("expected_terms_present") else 0)
    )


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"rows": len(rows), "by_case": {}}
    for case in sorted({str(row.get("case") or "") for row in rows if row.get("case")}):
        case_rows = [row for row in rows if row.get("case") == case]
        arms = {str(row.get("arm") or ""): row for row in case_rows}
        baseline = arms.get("baseline", {})
        connector = arms.get("connector", {})
        summary["by_case"][case] = {
            "baseline_score": baseline.get("score"),
            "connector_score": connector.get("score"),
            "delta": (
                int(connector.get("score") or 0) - int(baseline.get("score") or 0)
                if baseline and connector
                else None
            ),
            "connector_opened_target_host": bool(connector.get("opened_target_host")),
            "connector_proof_ok": bool(connector.get("proof_ok")),
        }
    return summary


def run_provider(
    provider_id: str,
    *,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
    port: int,
    output: Path,
    max_turns: int,
    send_timeout: float,
    new_chat_timeout: float,
    open_if_missing: bool,
    rerun_failed: bool,
    trace: LiveTrace | None,
    run_id: str,
) -> dict[str, Any]:
    payload = _load_or_new_payload(output, provider_id=provider_id, cases=cases, arms=arms)
    payload["trace_output"] = str(trace.path) if trace is not None else ""
    existing = {
        (str(row.get("case") or ""), str(row.get("arm") or ""))
        for row in payload["rows"]
        if row.get("ok") or not rerun_failed
    }
    _write_payload(output, payload)
    if trace is not None:
        trace.record_run_start(
            run_id=run_id,
            provider=provider_id,
            trace_output=str(trace.path),
            cases=tuple(case.name for case in cases),
            arms=arms,
            max_turns=max_turns,
        )
    provider_controls.begin_task_context(f"source-connector-ab:{provider_id}")
    provider = None
    try:
        provider = TimedProvider(
            connect_provider(
                provider_id,
                port=port,
                open_if_missing=open_if_missing,
                bring_to_front=open_if_missing,
            ),
            send_timeout=send_timeout,
            new_chat_timeout=new_chat_timeout,
        )
        for case in cases:
            for arm in arms:
                key = (case.name, arm)
                if key in existing:
                    continue
                row = run_case(
                    provider,
                    provider_id=provider_id,
                    case=case,
                    arm=arm,
                    max_turns=max_turns,
                    run_id=run_id,
                    trace=trace,
                )
                payload["rows"].append(row)
                payload["summary"] = summarize(payload["rows"])
                payload["updated_at"] = _timestamp()
                _write_payload(output, payload)
                print(
                    f"[{provider_id} {case.name} {arm}] "
                    f"ok={row.get('ok')} score={row.get('score')} stop={row.get('stop_reason', row.get('error', ''))}",
                    flush=True,
                )
        payload["complete"] = True
        payload["summary"] = summarize(payload["rows"])
        payload["updated_at"] = _timestamp()
        _write_payload(output, payload)
        if trace is not None:
            trace.record_run_complete(
                run_id=run_id,
                provider=provider_id,
                rows=len(payload["rows"]),
            )
        return payload
    finally:
        provider_controls.end_task_context()
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass


def _load_or_new_payload(output: Path, *, provider_id: str, cases: tuple[Case, ...], arms: tuple[str, ...]) -> dict[str, Any]:
    if output.exists():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                payload["complete"] = False
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "probe": "source_connector_ab",
        "provider": provider_id,
        "started_at": _timestamp(),
        "updated_at": _timestamp(),
        "trace_output": "",
        "cases": [case.name for case in cases],
        "arms": list(arms),
        "complete": False,
        "rows": [],
        "summary": {},
    }


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("source connector A/B result exceeded bounded size")
    _write_json_atomic(path, payload)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"source_connector_ab-{provider_id}-{stamp}.json"


def _trace_output_path(output: Path) -> Path:
    if output.suffix:
        return output.with_name(f"{output.stem}.trace.json")
    return output.with_name(f"{output.name}.trace.json")


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _parse_names(raw: list[str] | None, choices: dict[str, Any] | tuple[str, ...], label: str) -> tuple[str, ...]:
    if not raw:
        return tuple(choices)
    known = set(choices)
    names: list[str] = []
    for item in raw:
        for part in str(item or "").split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in known:
                raise SystemExit(f"unknown {label}: {name}")
            if name not in names:
                names.append(name)
    return tuple(names)


def _self_test() -> None:
    rows = [
        {"case": "pubmed", "arm": "baseline", "score": 4},
        {"case": "pubmed", "arm": "connector", "score": 9, "opened_target_host": True, "proof_ok": True},
    ]
    summary = summarize(rows)
    assert summary["by_case"]["pubmed"]["delta"] == 5
    assert _opened_target_host(["https://pubmed.ncbi.nlm.nih.gov/123/"], ("pubmed.ncbi.nlm.nih.gov",))
    assert _expected_terms_present("Retrieval augmented generation", ("retrieval", "generation"))
    assert _trace_output_path(Path("tests/manual/results/source_connector_ab-deepseek.json")).name == "source_connector_ab-deepseek.trace.json"
    with tempfile.TemporaryDirectory(prefix="codey-source-connector-ab-self-") as td:
        trace = LiveTrace(Path(td) / "trace.json")
        trace.record({"event": "probe"})
        payload = json.loads((Path(td) / "trace.json").read_text(encoding="utf-8"))
        assert payload["probe"] == "source_connector_ab_trace"
        assert payload["event_count"] == 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Live A/B for Research source connectors")
    parser.add_argument("--provider", choices=(*WEB_PROVIDERS, "all"), default="deepseek")
    parser.add_argument("--case", action="append", help="case name or comma list; defaults to pubmed,arxiv,open_guard")
    parser.add_argument("--arms", default="baseline,connector")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-output", type=Path, default=None, help="trace path; default is next to output with a .trace.json suffix")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--send-timeout", type=float, default=120)
    parser.add_argument("--new-chat-timeout", type=float, default=60)
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--no-live-trace", action="store_true", help="disable incremental atomic trace writes")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0

    selected_cases = tuple(CASES[name] for name in _parse_names(args.case, CASES, "case"))
    selected_arms = _parse_names([args.arms], ARMS, "arm")
    providers = WEB_PROVIDERS if args.provider == "all" else (args.provider,)
    run_id = f"source-connector-ab-{int(time.time())}"
    for provider_id in providers:
        output = args.output or _default_output(provider_id)
        if args.provider == "all" and args.output is not None:
            output = args.output.with_name(f"{args.output.stem}-{provider_id}{args.output.suffix}")
        trace: LiveTrace | None = None
        if not args.no_live_trace:
            if args.trace_output is not None:
                trace_output = args.trace_output
                if args.provider == "all":
                    trace_output = args.trace_output.with_name(f"{args.trace_output.stem}-{provider_id}{args.trace_output.suffix}")
            else:
                trace_output = _trace_output_path(output)
            trace = LiveTrace(trace_output)
        run_provider(
            provider_id,
            cases=selected_cases,
            arms=selected_arms,
            port=args.port,
            output=output,
            max_turns=max(1, args.max_turns),
            send_timeout=args.send_timeout,
            new_chat_timeout=args.new_chat_timeout,
            open_if_missing=args.open_if_missing,
            rerun_failed=args.rerun_failed,
            trace=trace,
            run_id=run_id,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
