"""Check saved Research submissions against today's contract and original read receipts.

This is an offline submission-boundary check, not a new browser run, semantic
review, or publication. Historical inputs and durable stores remain read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import TextRequests, decode_text_turn
from codey.research.answer_contract import answer_scope, submit_answer
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object, require_json_string
from tools.completion_artifact_audit import artifacts, tool_receipts


def replay(case: Path) -> JsonObject:
    state = case / "state"
    inputs = parse_json_object((case / "input.json").read_text(encoding="utf-8"))
    task = require_json_string(inputs["task"], "task")
    stored = artifacts(state)
    request = next(body for _, body in stored if body.get("kind") == "provider_request")
    payload = decode_json_object(request["payload"])
    snapshot = decode_json_object(payload["snapshot"])
    session = TaskSession(policy=TaskPolicy.from_payload(snapshot["policy"]), task_kind="research", task_text=task)
    provider = case / "provider.jsonl"
    original_paths = (case / "input.json", provider, case / "summary.json", state / "durable.sqlite3")
    hashes: JsonObject = {path.as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in original_paths}
    wire_args: list[JsonObject] = []
    for line in provider.read_text(encoding="utf-8").splitlines():
        row = parse_json_object(line)
        if row.get("type") != "response":
            continue
        response = decode_json_object(row["payload"])
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = decode_json_object(choices[0])
        message = decode_json_object(choice["message"])
        content = message.get("content")
        turn = decode_text_turn(content) if isinstance(content, str) else None
        if isinstance(turn, TextRequests):
            wire_args.extend(item.args for item in turn.items if item.tool == "submit_research_answer")
    submissions: list[JsonValue] = []
    for receipt in tool_receipts(state):
        result = receipt.result
        if result.call.name == "submit_research_answer":
            scope = answer_scope(session)
            args = dict(result.call.args)
            if args not in wire_args:
                raise ValueError(f"No identical text-protocol submission in original wire: {receipt.identity}")
            record: JsonObject = {
                "receipt": receipt.identity, "original_tool_ok": result.ok,
                "original_args": args, "matches_original_wire_args": args in wire_args,
                "read_aliases": [list(pair) for pair in scope.aliases],
                "scope": "current_submission_boundary_with_original_receipts",
            }
            try:
                accepted = submit_answer(scope, args)
            except ValueError as exc:
                record.update(accepted=False, reason=str(exc))
            else:
                record.update(accepted=True, markdown_preserved=accepted["markdown"] == args.get("markdown"),
                              declared_state=accepted["state"])
            invalid = {**args, "markdown": "Unknown evidence control [e999999]."}
            try:
                submit_answer(scope, invalid)
            except ValueError as exc:
                record["unknown_citation_control"] = str(exc)
            else:
                raise AssertionError("unknown citation was accepted")
            submissions.append(record)
        session._memory_results[receipt.identity] = result
    if not submissions:
        raise ValueError(f"No saved Research submission in {case}")
    if any(hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest for path, digest in hashes.items()):
        raise RuntimeError("Historical input changed during replay")
    return {"case": case.as_posix(), "original": parse_json_object((case / "summary.json").read_text(encoding="utf-8")),
            "source_sha256": hashes, "new_model_calls": 0, "historical_files_unchanged": True,
            "submissions": submissions}


class Options(argparse.Namespace):
    output: Path
    cases: list[Path]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("cases", nargs="+", type=Path)
    options = parser.parse_args(namespace=Options())
    rows: list[JsonValue] = [replay(case) for case in options.cases]
    with options.output.open("x", encoding="utf-8") as stream:
        json.dump({"scope": "offline_submission_contract_only", "new_model_calls": 0, "cases": rows},
                  stream, ensure_ascii=False, indent=2)
    print(f"Replayed {len(rows)} saved cases without model calls or publication.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
