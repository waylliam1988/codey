"""Validate model execution conclusions against verified task receipts."""
from __future__ import annotations

import json
import re

from codey.completion.contract import CompletionCheck
from codey.operations.task_session import TaskSession
from codey.runtime.core.execution_receipt import ExecutionReceipt, execution_receipt
from codey.types.json_value import JsonObject, decode_json_object

_REPORT_STATUS = re.compile(r"退出码|退出状态|执行状态|exit[ _-]*code|exit status|execution status", re.IGNORECASE)


def observed_claim(ref: str, receipt: ExecutionReceipt) -> JsonObject:
    claim: JsonObject = {"result_ref": ref, "status": receipt.status, "exit_code_scope": receipt.exit_code_scope}
    if receipt.exit_code is not None:
        claim["exit_code"] = receipt.exit_code
    return claim


def validate_execution_claims(session: TaskSession) -> CompletionCheck | None:
    receipts = {ref: receipt for ref, result in session._memory_results.items()
                if (receipt := execution_receipt(result)) is not None}
    required = bool(receipts) and _REPORT_STATUS.search(session.task_text) is not None
    raw = session.last_done_args.get("execution_claims")
    if raw is None and not required:
        return None
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
            raise ValueError("execution_claims.result_ref must identify a distinct execution receipt from this task")
        receipt = receipts[ref]
        if (claim["status"] != receipt.status or claim["exit_code_scope"] != receipt.exit_code_scope
                or (receipt.exit_code is None and "exit_code" in claim)
                or (receipt.exit_code is not None and (type(claim.get("exit_code")) is not int
                                                      or claim["exit_code"] != receipt.exit_code))):
            expected = json.dumps(observed_claim(ref, receipt), ensure_ascii=False)
            raise ValueError(f"execution_claims disagrees with {ref}. Copy this observed claim verbatim: {expected}. "
                             "status is the execution state from the receipt, not completed/success/failed. "
                             "Correct the claim and summary using the receipt, not the command text; do not execute again")
        selected.add(ref)
    if required and selected != receipts.keys():
        raise ValueError("done.execution_claims must cover the observed execution receipts: " + ", ".join(receipts))
    return CompletionCheck("execution_claims", "pass", "verified_execution_receipts")
