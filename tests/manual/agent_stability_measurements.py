"""Normalize observations without substituting external tests for agent work."""

from __future__ import annotations

import json
from collections import Counter

STARTS = {"tool_started", "tool_execution_start"}
ENDS = {"tool", "tool_execution_end", "tool_result", "tool_execution_finished", "tool_finished"}


def event_rows(text):
    rows = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def row_id(row):
    return str(row.get("tool_id") or row.get("toolCallId") or row.get("tool_call_id") or "")


def row_tool(row):
    return str(row.get("tool_name") or row.get("toolName") or row.get("tool") or "")


def row_outcome(row):
    if type(row.get("ok")) is bool:
        return row["ok"]
    if type(row.get("isError")) is bool:
        return not row["isError"]
    if isinstance(row.get("status"), str) and row["status"] in {"ok", "error"}:
        return row["status"] == "ok"
    return None


def tool_signature(tool, payload):
    args = payload.get("args")
    if not isinstance(args, dict):
        if not payload.get("command"):
            return None  # Bounded UI events omit offsets/content; those calls are incomparable.
        args = {key: payload[key] for key in ("command", "cwd") if key in payload}
    name = {"shell": "run", "bash": "run", "read_file": "read", "write_file": "write"}.get(tool, tool)
    return json.dumps({"tool": name, "args": args}, sort_keys=True, ensure_ascii=False)


def explicit_status(text):
    for offset, char in enumerate(text):
        if char == "{":
            try:
                candidate, _ = json.JSONDecoder().raw_decode(text[offset:])
                if isinstance(candidate, dict) and candidate.get("status") in {"blocked", "completed"}:
                    return candidate["status"]
            except json.JSONDecodeError:
                continue
    return None


def terminal_state(rows):
    terminals = [r for r in rows if r.get("type") in {"task_done", "agent_settled"}]
    codey = [r for r in terminals if r.get("type") == "task_done"]
    if codey:
        reason = codey[-1].get("stop_reason")
        blocked = reason == "blocked" or (reason == "done" and explicit_status(str(codey[-1].get("summary", ""))) == "blocked")
        return {"completed": reason == "done" and not blocked, "blocked": blocked,
                "stopped": reason == "stopped", "event": codey[-1], "reason": reason}
    assistants = [r["message"] for r in rows if r.get("type") == "message_end"
                  and isinstance(r.get("message"), dict) and r["message"].get("role") == "assistant"]
    message = assistants[-1] if assistants else {}
    stop = message.get("stopReason")
    text = "\n".join(c.get("text", "") for c in message.get("content", []) if c.get("type") == "text")
    explicit = explicit_status(text)
    latest_start = max((i for i, r in enumerate(rows) if r.get("type") == "agent_start"), default=0)
    current = rows[latest_start:]
    settled = any(r.get("type") == "agent_settled" for r in current)
    abort_acknowledged = any(r.get("type") == "response" and r.get("command") == "abort"
                             and r.get("success") is True for r in current)
    stopped = settled and (stop == "aborted" or abort_acknowledged)
    return {"completed": settled and not stopped and stop == "stop" and explicit != "blocked",
            "blocked": settled and not stopped and stop == "stop" and explicit == "blocked", "stopped": stopped,
            "event": terminals[-1] if terminals else None, "reason": stop}


def usage_totals(records):
    exchanges = [r for r in records if r.get("path") == "/v1/chat/completions" and not r.get("synthetic")]
    result = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"),
                           ("total_tokens", "token_usage")):
        values = []
        for record in exchanges:
            response = record.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            value = usage.get(source) if isinstance(usage, dict) else None
            values.append(value)
        known = [v for v in values if type(v) is int and v >= 0]
        result[target] = sum(known) if values and len(known) == len(values) else None
        result[f"known_{target}"] = sum(known) if known else None
    result["usage_complete"] = bool(exchanges) and result["token_usage"] is not None
    result["usage_observed_exchanges"] = sum(isinstance(r.get("response"), dict)
                                             and isinstance(r["response"].get("usage"), dict) for r in exchanges)
    result["physical_generation_exchanges"] = len(exchanges)
    return result


def metrics(rows, records, verification, returncode):
    starts, results, anonymous = {}, {}, []
    for row in rows:
        kind, identity = row.get("type"), row_id(row)
        if kind in STARTS:
            if identity:
                starts.setdefault(identity, row)
            else:
                anonymous.append(row)
        elif kind in ENDS and identity:
            results[identity] = {**results.get(identity, {}), **row}
    tools = list(starts.values()) + anonymous
    outcomes = [row_outcome(results[key]) for key in starts if key in results]
    signatures = Counter(sig for r in tools if (sig := tool_signature(row_tool(r), r)) is not None)
    mutations, repetition = Counter(), Counter()
    unknown_changes = 0
    for key, row in starts.items():
        result = results.get(key, {})
        sig = tool_signature(row_tool(row), row)
        changed = result.get("observed_changed", result.get("changed"))
        if row_outcome(result) is True and changed is True:
            mutations[(sig if sig is not None else key, str(row.get("observed_workspace")))] += 1
        elif row_tool(row) in {"edit", "write", "write_file", "shell", "run", "bash"} and changed is None:
            unknown_changes += 1
        if sig is not None and row.get("observed_workspace") is not None and result.get("observed_result") is not None and changed is False:
            repetition[(sig, str(row["observed_workspace"]), str(result["observed_result"]))] += 1
    terminal = terminal_state(rows)
    verified = verification.get("passed") is True
    fresh = verification.get("agent_verification_fresh") is True
    names = [row_tool(r) for r in tools]
    codes = [str(r.get("stop_reason") or r.get("error_code") or "") for r in rows]
    return {"process_returncode": returncode,
            "task_success": type(returncode) is int and returncode == 0 and verified and fresh and terminal["completed"],
            "patch_correctness": verification.get("patch_correctness", verified) is True,
            "tests_actually_passing": verification.get("visible_tests", verified) is True,
            "agent_verification_fresh": fresh,
            "false_completion": terminal["completed"] and not (verified and fresh),
            "normal_completion": terminal["completed"], "blocked": terminal["blocked"], "stopped": terminal["stopped"],
            "tool_calls": len(tools), "repeated_tool_calls": sum(n - 1 for n in signatures.values() if n > 1),
            "unknown_argument_calls": sum(tool_signature(row_tool(r), r) is None for r in tools),
            "unproductive_repeated_calls": sum(n - 1 for n in repetition.values() if n > 1),
            "mutation_calls": sum(mutations.values()), "duplicate_mutation": any(n > 1 for n in mutations.values()),
            "unknown_mutation_calls": unknown_changes, "recovery_success": None,
            "terminal_event": terminal["event"], "terminal_reason": terminal["reason"],
            "llm_rounds": sum(r.get("path") == "/v1/chat/completions" for r in records),
            "failed_tool_calls": sum(v is False for v in outcomes),
            "successful_tool_calls": sum(v is True for v in outcomes),
            "unknown_tool_calls": len(tools) - sum(v is not None for v in outcomes),
            "read_calls": names.count("read") + names.count("read_file"), "edit_calls": names.count("edit"),
            "write_calls": names.count("write") + names.count("write_file"),
            "run_calls": sum(names.count(n) for n in ("run", "bash", "shell")),
            "protocol_errors": sum(c == "protocol_error" for c in codes),
            "policy_denials": sum(r.get("type") in {"shell_rejected", "policy_denied"} for r in rows),
            **usage_totals(records)}


def stored_output_was_read(records, marker):
    for record in records:
        request = record.get("request")
        if not isinstance(request, dict):
            continue
        calls = {}
        for message in request.get("messages", []):
            for call in message.get("tool_calls", []):
                calls[call.get("id")] = call.get("function", {})
            if message.get("role") != "tool" or marker not in str(message.get("content", "")):
                continue
            function = calls.get(message.get("tool_call_id"), {})
            try:
                args = json.loads(function.get("arguments", "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            name = function.get("name")
            if name == "read_tool_result":
                return True
            if name in {"read", "read_file"} and args.get("path") and str(args["path"]).replace("\\", "/").split("/")[-1] not in {"app.py", "test_app.py"}:
                return True
            if name in {"bash", "shell", "run"} and args.get("command") and "unittest" not in args["command"] and "test_app.py" not in args["command"]:
                return True
    return False


def paired_summary(rows):
    grouped = {}
    for row in rows:
        key = row["seed"], row["case"]
        pair = grouped.setdefault(key, {})
        if row["arm"] in pair:
            raise ValueError(f"duplicate experiment key: {key}/{row['arm']}")
        pair[row["arm"]] = row
    pairs, excluded = [], []
    for (seed, case), arms in sorted(grouped.items()):
        if set(arms) != {"codey", "pi"} or any(r.get("backend_isolation_error") or r.get("status") in {"environment_error", "harness_error"}
                                               for r in arms.values()):
            excluded.append({"seed": seed, "case": case, "reason": "missing arm or setup/isolation/observer failure"})
            continue
        codey, pi = arms["codey"], arms["pi"]
        a, b = [r.get("metrics", {}).get("scenario_success") is True for r in (codey, pi)]
        outcome = "both" if a and b else "codey_only" if a else "pi_only" if b else "neither"
        time_delta, token_delta = None, None
        if a and b:
            left, right = codey.get("wall_time_seconds"), pi.get("wall_time_seconds")
            if type(left) in (int, float) and type(right) in (int, float) and right > 0:
                time_delta = round(100 * (left / right - 1), 2)
            counts = [r.get("metrics", {}) for r in (codey, pi)]
            if all(c.get("usage_complete") is True and type(c.get("token_usage")) is int for c in counts):
                left, right = [c["token_usage"] for c in counts]
                if left >= 0 and right > 0:
                    token_delta = round(100 * (left / right - 1), 2)
        pairs.append({"seed": seed, "case": case, "outcome": outcome,
                      "codey_time_delta_percent": time_delta, "codey_token_delta_percent": token_delta})
    return {"comparable_pairs": len(pairs), "outcomes": dict(Counter(p["outcome"] for p in pairs)),
            "pairs": pairs, "excluded_pairs": excluded}
