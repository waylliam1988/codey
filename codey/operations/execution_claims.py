"""Validate model execution conclusions against verified task receipts."""
from __future__ import annotations

import json

from codey.completion.contract import CompletionCheck
from codey.operations.task_session import TaskSession
from codey.runtime.core.execution_receipt import ExecutionReceipt, execution_receipt
from codey.types.json_value import JsonObject, decode_json_object


def observed_claim(ref: str, receipt: ExecutionReceipt) -> JsonObject:
    claim: JsonObject = {"result_ref": ref, "status": receipt.status, "exit_code_scope": receipt.exit_code_scope}
    if receipt.exit_code is not None:
        claim["exit_code"] = receipt.exit_code
    return claim


def validate_execution_claims(session: TaskSession) -> CompletionCheck | None:
    receipts = {ref: receipt for ref, result in session._memory_results.items()
                if (receipt := execution_receipt(result)) is not None}
    raw = session.last_done_args.get("execution_claims")
    if raw is None:
        return CompletionCheck("execution_claims", "pass", "verified_execution_receipts") if receipts else None
    if not isinstance(raw, list) or len(raw) > 64:
        raise ValueError("done.execution_claims must list result_ref, status, exit_code_scope and observed exit_code; "
                         "omit exit_code when unknown. Read the existing receipt; do not repeat execution")
    selected: set[str] = set()
    for item in raw:
        claim = decode_json_object(item)
        if (not {"result_ref", "status", "exit_code_scope"} <= claim.keys()
                or claim.keys() - {"result_ref", "status", "exit_code_scope", "exit_code"}):
            raise ValueError("execution_claims fields must be result_ref, status, exit_code_scope and optional exit_code")
        ref = claim["result_ref"]
        if not isinstance(ref, str) or ref not in receipts or ref in selected:
            available = json.dumps([observed_claim(key, receipt) for key, receipt in receipts.items()], ensure_ascii=False)
            raise ValueError("execution_claims.result_ref must identify a distinct execution receipt from this task. "
                             "Eligible shell/process receipts: " + available + ". Browser actions are operation receipts; "
                             "omit this optional claim if no eligible receipt exists")
        receipt = receipts[ref]
        if (claim["status"] != receipt.status or claim["exit_code_scope"] != receipt.exit_code_scope
                or (receipt.exit_code is None and "exit_code" in claim)
                or (receipt.exit_code is not None and (type(claim.get("exit_code")) is not int
                                                      or claim["exit_code"] != receipt.exit_code))):
            expected = json.dumps(observed_claim(ref, receipt), ensure_ascii=False)
            raise ValueError(f"execution_claims disagrees with {ref}. Observed execution facts: {expected}. "
                             "Correct this optional claim and its explanation using the receipt; do not execute again")
        selected.add(ref)
    return CompletionCheck("execution_claims", "pass", "verified_execution_receipts")
