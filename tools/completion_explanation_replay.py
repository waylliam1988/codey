"""Replay historical first proposals through production normalization and receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import prepare_completion_output
from codey.operations.kernel_facts import project_execution_record, project_result_observation
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot, normalize_turn
from codey.operations.kernel_receipts import restore_result_receipt
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.ledger import ResearchLedger
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectProjection, RuntimeEffectSettlement
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string
from tools.completion_artifact_audit import artifacts
from tools.completion_explanation_ab import URL, final_checks, production_source_digest, write_json


def replay(case: Path, *, experimental_snapshot: bool = False) -> JsonObject:
    state = case / "state"
    original = artifacts(state)
    requests = [decode_json_object(body["payload"]) for _, body in original if body.get("kind") == "provider_request"]
    captured = decode_json_object(requests[0]["snapshot"])
    policy = TaskPolicy.from_payload(captured["policy"])
    request = decode_json_object(json.loads((case / "request.json").read_text(encoding="utf-8")))
    task = require_json_string(request["task"], "task")
    session = TaskSession(policy=policy, task_kind="chat", task_text=task, required_source_urls=frozenset({URL}))
    ledger = ResearchLedger()
    receipt_refs: list[JsonValue] = []
    with sqlite3.connect((state / "durable.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as connection:
        for row in connection.execute("SELECT intent, settlement FROM effects WHERE category='tool_call' "
                                      "AND settlement IS NOT NULL ORDER BY rowid"):
            raw_intent: object = row[0]
            raw_settlement: object = row[1]
            if not isinstance(raw_intent, str) or not isinstance(raw_settlement, str):
                raise ValueError("invalid stored effect")
            intent = RuntimeEffectIntent.from_payload(json.loads(raw_intent))
            settlement = RuntimeEffectSettlement.from_payload(json.loads(raw_settlement))
            result = restore_result_receipt(RuntimeEffectProjection(intent=intent, settlement=settlement),
                store=ManagedOutputStore(state), session_id=intent.session_id, run_id=intent.run_id)
            project_result_observation(session, result.call, result, ok=result.ok,
                                       exit_code=settlement.exit_code, research_ledger=ledger)
            project_execution_record(session, intent.effect_id, result.call, result,
                                     ok=result.ok, exit_code=settlement.exit_code)
            session._memory_results[intent.effect_id] = result
            receipt_refs.append({"result_ref": intent.effect_id, "tool": result.call.name, "ok": result.ok})
    proposal_path = case / "proposals.jsonl"
    first = decode_json_object(json.loads(proposal_path.read_text(encoding="utf-8").splitlines()[0]))
    raw = require_json_string(first["normalized_reply"], "normalized_reply")
    new_snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    outcomes: JsonObject = {}
    context = CompletionContext(task=task, research_ledger=ledger)
    decoded = decode_text_turn(raw)
    if not isinstance(decoded, TextRequests):
        raise ValueError("historical proposal must be a tool request")
    native = AssistantTurn(tool_calls=tuple(ProviderToolCall(str(index), item.tool, dict(item.args))
                                           for index, item in enumerate(decoded.items)))
    comparisons: list[tuple[str, TurnSnapshot, TextRequests | AssistantTurn]] = [
        ("new_contract", new_snapshot, decoded),
        ("new_contract_native_equivalent", build_turn_snapshot(session, native=True), native)]
    if not experimental_snapshot:
        comparisons.insert(0, ("original_contract", restore_snapshot(captured), decoded))
    for label, snapshot, turn in comparisons:
        plan = normalize_turn(turn, snapshot=snapshot)
        if plan.protocol_error or plan.control is None:
            outcomes[label] = {"complete": False, "protocol_error": plan.protocol_error}
            continue
        session.last_done_args = dict(plan.control_args)
        output = prepare_completion_output(session, plan.control.body, context)
        verdict = evaluate(session, output.text, context=context, observations=output.observations)
        checks = final_checks(output.text)
        outcomes[label] = {"complete": verdict.complete, "followup": verdict.followup,
                           "checks": checks, "explanation_preserved": checks.get("explanation") == first["raw_summary"],
                           "normalized_args": dict(plan.control_args)}
    return {"case": str(case), "original_snapshot": captured, "new_snapshot": snapshot_description(new_snapshot),
            "first_proposal": first, "proposal_file_sha256": hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
            "original_result": decode_json_object(json.loads((case / "summary.json").read_text(encoding="utf-8"))),
            "receipts": receipt_refs, "outcomes": outcomes}


class Options(argparse.Namespace):
    run_dir: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    options = parser.parse_args(namespace=Options())
    options.run_dir.mkdir(parents=True, exist_ok=False)
    rows: list[JsonValue] = []
    for batch in ("first", "final"):
        for case in sorted(Path(f".e2e-artifacts/production-explanation-20261111-{batch}").glob("*/B")):
            row = replay(case)
            rows.append(row)
            print(json.dumps({"case": str(case), "outcomes": row["outcomes"]}, ensure_ascii=True), flush=True)
    if len(rows) != 20:
        raise ValueError("expected both complete historical batches (20 first proposals)")
    write_json(options.run_dir / "replay.json", {"scope": "offline_original_proposals_and_validated_receipts",
               "production_source": production_source_digest(), "cases": rows})
    # Preserve the intermediate uncommitted v4 snapshot verbatim; it is not a
    # supported historical production schema and must not be silently migrated.
    claims_case = Path(".e2e-artifacts/source-gaps-fix-20261111-ab/04/B")
    write_json(options.run_dir / "execution-claims-04B.json", {
        "scope": "new_contract_replay_with_native_equivalent_not_original_native_transport",
        "original_contract_result": "retained_in_original_result_and_artifacts_not_reinterpreted",
        "case": replay(claims_case, experimental_snapshot=True)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
