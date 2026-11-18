"""Frozen three-arm projection probe; raw state and replies stay outside Git."""
from __future__ import annotations

import argparse
import json
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from codey.app.context import AppContext
from codey.ghost.observation_index import HistoryView, render_retrieved_block, retrieve_relevant_observations
from codey.ghost.payload_types import decoded_object, object_fields
from codey.providers.base import ProviderToolDefinition
from codey.providers.token_accounting import ApiExchangeUsage, ReportedUsage
from tests.manual.ghost_memory_routing.portable_context_trial import CapturedProvider, SeedProvider, run_task, save
from tests.manual.ghost_memory_routing.portable_context_trial import cell as confirmed_cell
from tests.manual.ghost_memory_routing.project_experience_continuity import _models

FIELDS = ("answer", "reason", "user_accepted", "verified")
ARMS = ("off", "legacy", "projected")
TOOL = ProviderToolDefinition("evaluate", "Return the answer and its attribution for this experiment.", {
    "type": "object", "properties": {key: {"type": "string"} for key in FIELDS},
    "required": list(FIELDS), "additionalProperties": False,
})


@dataclass(frozen=True)
class Case:
    identity: str
    view: HistoryView
    user: str
    assistant: str
    query: str
    reference: str
    expected: str
    legacy: str

    def row(self) -> dict[str, object]:
        return {"run_id": self.identity, "mode": "chat" if self.view == "requests" else "project",
                "ts": "2026-11-18T00:00:00Z", "user_text": self.user, "assistant_text": self.assistant}


@dataclass
class Options:
    evidence: Path = Path(".")
    limit: int = 48
    protocol: str = "first"
    normal: bool = False
    summarize: bool = False
    confirmed: bool = False


class ProbeProvider(CapturedProvider):
    def __init__(self, home: Path, *, stream: bool) -> None:
        self.started = time.monotonic()
        self.first_event_seconds: float | None = None
        super().__init__(home, output=8192, stream=stream)

    def _parse_usage(self, event: dict[str, object]) -> ReportedUsage | None:
        if self.first_event_seconds is None:
            self.first_event_seconds = time.monotonic() - self.started
        return super()._parse_usage(event)


def ignore_usage(_record: ApiExchangeUsage) -> None:
    pass


class HistorySeed(SeedProvider):
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def send(self, text: str, timeout: float | None = None) -> str:
        return self.reply


def normal_cell(case: Case, stream: bool, output: Path) -> dict[str, object]:
    output.mkdir(parents=True)
    project, state_home = output / "project", output / "state"
    project.mkdir()
    with closing(AppContext(state_home, start_scheduler=False)) as state:
        seed = run_task(state, HistorySeed(case.assistant), project, task=case.user, identity="source", intent="chat")
        if seed.get("stop_reason") != "done":
            raise ValueError("synthetic source failed")
    task = (
        f"Fixture reference (not a website read): {case.reference}\nTask: {case.query}\n"
        "Do not inspect files or use tools other than done. Call done once. The summary must be a STRING "
        "containing only valid JSON with exactly four string fields: answer, reason, user_accepted, verified. "
        "Use labels without explanations. answer: 09:00, 11:00, quartzstore, opallog, unknown; "
        "reason: offline durability, unknown; user_accepted: yes, unknown; verified: yes, unknown. "
        "Choose from the evidence; when no explicit acceptance or verification exists use unknown. "
        "No changes are required."
    )
    provider = ProbeProvider(output, stream=stream)
    try:
        with closing(AppContext(state_home, start_scheduler=False)) as state:
            terminal = run_task(state, provider, project, task=task, identity="next",
                                intent="chat" if case.view == "requests" else "project")
        save(output / "terminal.json", terminal)
        if terminal.get("stop_reason") != "done":
            save(output / "failure.json", {"terminal": terminal, "requests": provider.requests})
            raise RuntimeError("normal-entry run did not complete; inspect terminal and raw receipts before retry")
        answer: dict[str, object] = {}
        events = (output / "response-events.jsonl").read_text(encoding="utf-8").splitlines()
        # Normal-entry outcome is checked below via the durable terminal summary.
        summary = terminal.get("summary")
        if isinstance(summary, str):
            try:
                answer = decoded_object(summary)
            except ValueError:
                answer = {}
        result: dict[str, object] = {"id": case.identity, "stream": stream, "terminal": terminal,
            "score": score(case, answer), "answer": answer, "requests": provider.requests,
            "seconds": time.monotonic() - provider.started, "first_event_seconds": provider.first_event_seconds,
            "response_events": len(events), "usage": [record.to_payload() for record in provider.usage_records]}
        save(output / "normal-result.json", result)
        return result
    finally:
        provider.close()


def cases() -> tuple[Case, ...]:
    value: object = json.loads(Path(__file__).with_name("history_projection_cases.json").read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("cases must be a list")
    entries: list[object] = value
    result: list[Case] = []
    for entry in entries:
        if not object_fields(entry):
            raise ValueError("invalid case")
        fields: dict[str, str] = {}
        for key in ("id", "kind", "user", "assistant", "query", "reference", "expected", "legacy"):
            field = entry.get(key)
            if not isinstance(field, str):
                raise ValueError(f"invalid case field: {key}")
            fields[key] = field
        view = fields["kind"]
        if view not in {"requests", "dialogue"}:
            raise ValueError("invalid history view")
        result.append(Case(fields["id"], "requests" if view == "requests" else "dialogue",
                           fields["user"], fields["assistant"], fields["query"], fields["reference"],
                           fields["expected"], fields["legacy"]))
    return tuple(result)


def score(case: Case, answer: dict[str, object]) -> dict[str, bool]:
    valid = set(answer) == set(FIELDS) and all(isinstance(value, str) for value in answer.values())
    correct = answer.get("answer") == case.expected
    reason = case.view == "requests" or answer.get("reason") == "offline durability"
    attribution = answer.get("user_accepted") == "unknown" and answer.get("verified") == "unknown"
    return {"format_valid": valid, "answer_correct": correct, "reason_correct": reason,
            "attribution_correct": attribution, "task_correct": valid and correct and reason and attribution,
            "old_error": case.view == "requests" and answer.get("answer") == "09:00"}


def summarize(home: Path) -> dict[str, object]:
    value: object = json.loads((home / "results.json").read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("invalid results")
    rows: list[object] = value
    groups: list[dict[str, object]] = []
    for kind in ("requests", "dialogue"):
        for arm in ARMS:
            for stream in (False, True):
                selected = [row for row in rows if object_fields(row) and row.get("kind") == kind
                            and row.get("arm") == arm and row.get("stream") is stream]
                scores = [row["score"] for row in selected if object_fields(row.get("score"))]
                durations = [seconds for row in selected if isinstance(seconds := row.get("seconds"), (int, float))]
                first_events = [seconds for row in selected if isinstance(seconds := row.get("first_event_seconds"), (int, float))]
                chars = [size for row in selected if type(size := row.get("history_chars")) is int]
                reported, inputs, outputs = 0, 0, 0
                for row in selected:
                    records = row.get("usage")
                    if not isinstance(records, list):
                        continue
                    usage_records: list[object] = records
                    for record in usage_records:
                        if not object_fields(record) or not object_fields(usage := record.get("usage")):
                            continue
                        before, after = usage.get("input_tokens"), usage.get("output_tokens")
                        if type(before) is int and type(after) is int:
                            reported += 1
                            inputs += before
                            outputs += after
                groups.append({"kind": kind, "arm": arm, "stream": stream, "n": len(selected),
                    "task_correct": sum(object_fields(score) and score.get("task_correct") is True for score in scores),
                    "old_errors": sum(object_fields(score) and score.get("old_error") is True for score in scores),
                    "attribution_correct": sum(object_fields(score) and score.get("attribution_correct") is True for score in scores),
                    "format_valid": sum(object_fields(score) and score.get("format_valid") is True for score in scores),
                    "mean_seconds": round(sum(durations) / len(durations), 4) if durations else None,
                    "mean_first_json_seconds": round(sum(first_events) / len(first_events), 4) if first_events else None,
                    "mean_history_chars": sum(chars) / len(chars) if chars else None,
                    "http_requests": sum(count for row in selected if type(count := row.get("requests")) is int),
                    "reported_usage_calls": reported, "input_tokens": inputs if reported else None,
                    "output_tokens": outputs if reported else None})
    return {"groups": groups, "n": len(rows)}


def cell(case: Case, arm: str, stream: bool, output: Path, *, protocol: str) -> dict[str, object]:
    output.mkdir(parents=True)
    history = ""
    if arm == "legacy":
        history = case.legacy
    elif arm == "projected":
        picked = retrieve_relevant_observations([case.row()], case.query, view=case.view)
        history = render_retrieved_block(picked, view=case.view) if picked else ""
    prompt = (
        "This is an isolated fixture evaluation, not a real website read. Use only the supplied reference "
        "and labeled historical context. Call evaluate exactly once. Do not call any other tool. "
        "Use the exact supported text for answer/reason, or unknown if unavailable. "
        "For user_accepted and verified use yes only with explicit acceptance/proof, otherwise unknown.\n\n"
        f"Current fixture reference:\n{case.reference}\n\n{history}\n\nTask: {case.query}"
    )
    if protocol == "labels-v2":
        prompt += (
            "\nReturn labels only, without sentences or explanations. EXACT allowed strings: "
            "answer: 09:00, 11:00, quartzstore, opallog, unknown; "
            "reason: offline durability, unknown; user_accepted: yes, unknown; verified: yes, unknown. "
            "Choose based on the evidence, not merely because a label appears in this list."
        )
    save(output / "prompt.json", {"prompt": prompt, "history_chars": len(history)})
    provider = ProbeProvider(output, stream=stream)
    provider.bind_usage("history-projection", ignore_usage)
    started = time.monotonic()
    try:
        turn = provider.send_turn(prompt, [TOOL], require_tool_call=True)
        answer: dict[str, object] = {}
        if len(turn.tool_calls) == 1 and turn.tool_calls[0].name == TOOL.name:
            answer = dict(turn.tool_calls[0].arguments)
        result: dict[str, object] = {"id": case.identity, "kind": case.view, "arm": arm, "stream": stream,
            "answer": answer, "score": score(case, answer), "requests": provider.requests,
            "seconds": time.monotonic() - started, "history_chars": len(history), "finish": turn.finish.value,
            "usage": [record.to_payload() for record in provider.usage_records], "protocol": protocol,
            "first_event_seconds": provider.first_event_seconds}
        save(output / "result.json", result)
        return result
    except Exception as exc:
        save(output / "failure.json", {"type": type(exc).__name__, "message": str(exc),
                                      "requests": provider.requests, "seconds": time.monotonic() - started})
        raise
    finally:
        provider.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=48)
    parser.add_argument("--protocol", choices=("first", "labels-v2"), default="first")
    parser.add_argument("--normal", action="store_true")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--confirmed", action="store_true")
    args = parser.parse_args(namespace=Options())
    if args.limit < 1:
        raise ValueError("invalid arguments")
    home, limit = args.evidence, args.limit
    home.mkdir(parents=True, exist_ok=True)
    if args.summarize:
        report = summarize(home)
        save(home / "metrics.json", report)
        print(json.dumps(report, indent=2))
        return
    _models(home, "models-before")
    results: list[dict[str, object]] = []
    try:
        if args.confirmed:
            for stream in (False, True):
                result = confirmed_cell(home / f"confirmed-{stream}", output=8192, stream=stream,
                                        arm="on", protocol="labels-v3")
                results.append(result)
                save(home / "confirmed-results.json", results)
                print(json.dumps(result), flush=True)
            return
        if args.normal:
            for case in (cases()[0], cases()[4]):
                for stream in (False, True):
                    result = normal_cell(case, stream, home / f"normal-{case.identity}-{stream}")
                    results.append(result)
                    save(home / "normal-results.json", results)
                    print(json.dumps(result), flush=True)
            return
        for index, case in enumerate(cases()):
            for stream in (False, True):
                for offset in range(3):
                    arm = ARMS[(index + offset) % len(ARMS)]
                    directory = home / f"{case.identity}-{arm}-{'stream' if stream else 'plain'}"
                    if (directory / "result.json").exists():
                        results.append(decoded_object((directory / "result.json").read_text(encoding="utf-8")))
                    else:
                        result = cell(case, arm, stream, directory, protocol=args.protocol)
                        results.append(result)
                        progress: dict[str, object] = {key: result[key] for key in ("id", "arm", "stream", "score")}
                        print(json.dumps(progress), flush=True)
                    save(home / "results.json", results)
                    if len(results) >= limit:
                        return
    finally:
        _models(home, "models-after")


if __name__ == "__main__":
    main()
