"""Compare model transcription with program assembly over shared Chromium receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch

from codey.policies.output_requirement import declaration, evaluate
from codey.policies.tool_requirement import decode_answer
from codey.providers.request_context import clock_context
from codey.types.json_value import (
    JsonObject,
    JsonValue,
    decode_json_object,
    decode_json_value,
    require_json_integer,
    require_json_string,
)
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import (
    ACQUISITION,
    Arm,
    ExperimentProvider,
    Target,
    production_source_digest,
    provider_metrics,
    service_state,
    write_json,
)
from tools.protocol_surface import script_body

FIELDS = ("page_url", "token", "amount", "unit", "code")
RECORD = "window.__record"
TOKEN = "window.__contractToken"
ACQUIRE = ACQUISITION.replace("return {text,token:evaluated.value,page_url:tabs.page_url};",
    "const record=unwrap(await tools.browser_eval({code:'window.__record'}));"
    "return {text,token:evaluated.value,page_url:tabs.page_url,record:record.value};")
COMMON = ("请根据共享的浏览器工具回执交付福州样本记录。"
          "explanation 用一句中文说明页面如何加载出该记录，请提到福州。"
          "最终答案由 explanation、page_url、token、amount（整数净重）、unit（单位）、code（校验码）组成。"
          "只返回一个 JSON 对象，无 Markdown。")
COPY = "你的 JSON 必须包含全部六个字段，从回执逐字填写精确值。"
ASSEMBLE = "你的 JSON 只包含 explanation，程序会从同一份回执填入其他五个字段。"
SCHEMA: JsonObject = {"type": "object", "properties": {
    name: {"type": "integer"} if name == "amount" else {"type": "string", "minLength": 1}
    for name in ("explanation", *FIELDS)},
    "required": ["explanation", *FIELDS], "additionalProperties": False}
OUTPUT_CHECK = declaration("json", json.dumps(SCHEMA))


def fixture(index: int) -> tuple[str, str]:
    if index == 1:
        return gate.URL, gate.HTML.replace("amount:173,code:", "amount:173,unit:'克',code:")
    digest = hashlib.sha256(f"record-{index:02d}".encode()).hexdigest()
    url = f"https://example.com/production-browser-record-{index:02d}?batch=west-2&ref={digest[:12]}"
    token = f"SCHEMA_TOKEN_{index:02d}_{digest[12:36]}"
    code = f"QUARTZ-{index:02d}_{digest[36:52].upper()}"
    amount = 137 + index * 29
    html = (gate.HTML.replace("amount:173,code:'QUARTZ_851'", f"amount:{amount},unit:'克',code:'{code}'")
            .replace("SCHEMA_TOKEN_74", token))
    return url, html


def shared_evidence(receipts: list[EffectReceipt]) -> JsonObject:
    parent, = [r for r in receipts if r.result.call.name == "codemode" and r.result.ok]
    child_ids = parent.result.canonical["child_effect_ids"]
    if not isinstance(child_ids, list):
        raise ValueError("CodeMode receipt lacks its settled child references")
    leaves = [r for r in receipts if r.identity in child_ids and r.result.ok and not r.result.truncated]
    evidence: JsonObject = {}
    for key, tool, code in (("page", "browser_tab_list", ""), ("token", "browser_eval", TOKEN),
                            ("record", "browser_eval", RECORD), ("body", "browser_read", "")):
        receipt, = [r for r in leaves if r.result.call.name == tool and (not code or r.result.call.args.get("code") == code)]
        value = receipt.result.canonical.get("value")
        if code:
            data = decode_json_object(value)
            if data.get("truncated") is not False:
                raise ValueError(f"{key} evaluation is incomplete")
        evidence[key] = {"receipt_ref": receipt.identity, "tool": tool,
                         "value": receipt.result.model_text if key == "body" else decode_json_value(value)}
    return evidence


def fields(evidence: JsonObject) -> JsonObject:
    page = decode_json_object(decode_json_object(evidence["page"])["value"])
    token = decode_json_object(decode_json_object(evidence["token"])["value"])
    record = decode_json_object(decode_json_object(decode_json_object(evidence["record"])["value"])["value"])
    return {"page_url": require_json_string(page["page_url"], "page_url"),
            "token": require_json_string(token["value"], "token"),
            "amount": require_json_integer(record["amount"], "amount"),
            "unit": require_json_string(record["unit"], "unit"),
            "code": require_json_string(record["code"], "code")}


def acquire(directory: Path, target: Target, index: int) -> JsonObject:
    url, html = fixture(index)
    actions = iter([
        {"tool": "codemode", "args": {"code": ACQUIRE, "inputs": {"url": url, "city": "福州", "wait_text": "鱼丸"}}},
        {"tool": "done", "args": {"summary": "真实浏览器采集结束。"}},
    ])

    def scripted(endpoint: str, payload: dict[str, object], headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        observe = kwargs["observe"]
        assert callable(observe)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        return script_body(decode_json_object(next(actions)))

    with (patch.object(gate, "URL", url), patch.object(gate, "HTML", html),
          patch("codey.providers.api_generation.generate", scripted),
          patch("tools.completion_explanation_ab.service_state", return_value={})):
        row = gate.run_case(directory, "chat", target, structured=True, max_turns=2)
    if row["stop_reason"] != "done":
        raise ValueError(f"Chromium acquisition failed: {row['stop_reason']}")
    evidence = shared_evidence(tool_receipts(directory / "state"))
    values = fields(evidence)
    body = require_json_string(decode_json_object(evidence["body"])["value"], "browser_read")
    if values["page_url"] != url or not all(str(values[key]) in body for key in ("amount", "unit", "code")):
        raise ValueError("structured record disagrees with the navigated page or visible body")
    write_json(directory / "shared-receipts.json", evidence)
    return evidence


def score(answer: JsonObject, expected: JsonObject) -> JsonObject:
    return {key: "missing" if key not in answer else "incorrect"
            if type(answer[key]) is not type(value) or answer[key] != value else "exact"
            for key, value in expected.items()}


def assemble(raw: str, arm: Arm, expected: JsonObject) -> tuple[JsonObject, list[str]]:
    try:
        submitted = decode_json_object(decode_answer(raw))
    except (TypeError, ValueError):
        return {}, ["返回一个合法且没有重复键的 JSON 对象"]
    explanation = submitted.get("explanation")
    issues = []
    if not isinstance(explanation, str) or "福州" not in explanation:
        issues.append("explanation 应用中文说明福州页面记录如何加载")
    allowed = {"explanation", *FIELDS} if arm == "A" else {"explanation"}
    if submitted.keys() - allowed:
        issues.append("只提交本组要求的字段")
    answer = submitted if arm == "A" else {"explanation": explanation, **expected}
    wrong = [key for key, status in score(answer, expected).items() if status != "exact"]
    if wrong:
        issues.append("核对回执字段 " + ", ".join(wrong))
    if not issues and not evaluate(OUTPUT_CHECK, json.dumps(answer))[0]:
        issues.append("最终字段必须满足各自的类型和格式")
    return answer, issues


def run_arm(directory: Path, target: Target, arm: Arm, evidence: JsonObject, max_calls: int) -> JsonObject:
    directory.mkdir()
    expected = fields(evidence)
    shared = json.dumps(evidence, ensure_ascii=False, sort_keys=True, allow_nan=False)
    messages: list[dict[str, object]] = [
        {"role": "system", "content": COMMON + (COPY if arm == "A" else ASSEMBLE)},
        {"role": "user", "content": shared},
    ]
    attempts: list[JsonValue] = []
    published: JsonObject = {}
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    error = ""
    try:
        for _ in range(max_calls):
            try:
                reply = provider._generate(messages, output_token_limit=1024)
                choices = reply["choices"]
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError("expected exactly one model response")
                message = decode_json_object(decode_json_object(choices[0])["message"])
                raw = require_json_string(message["content"], "response content")
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            answer, issues = assemble(raw, arm, expected)
            attempts.append({"raw": raw, "fields": score(answer, expected), "issues": [issue for issue in issues]})
            if not issues:
                published = answer
                break
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "；".join(issues)})
    finally:
        provider.close()
    log = directory / "provider.jsonl"
    lines: list[str] = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    rows = [decode_json_object(json.loads(line)) for line in lines]
    requests = [decode_json_object(row["payload"]) for row in rows if row.get("phase") == "request"]
    for request in requests:
        wire_messages = request["messages"]
        if not isinstance(wire_messages, list) or sum(decode_json_object(m).get("content") == shared for m in wire_messages) != 1:
            raise ValueError("wire request did not carry exactly one shared receipt bundle")
    metrics = provider_metrics(log)
    result: JsonObject = {"arm": arm, "passed": bool(published), "answer": published, "attempts": attempts,
                         "first_fields": decode_json_object(attempts[0])["fields"] if attempts else score({}, expected),
                         "final_fields": score(published, expected), "http_requests": len(requests),
                         "evidence_sha256": hashlib.sha256(shared.encode()).hexdigest(), "provider_error": error, **metrics}
    write_json(directory / "summary.json", result)
    return result


def aggregate(rows: list[JsonObject]) -> JsonObject:
    summary: JsonObject = {"runs": len(rows), "passed": sum(row["passed"] is True for row in rows),
                          "model_calls": sum(require_json_integer(row["model_calls"], "model_calls") for row in rows),
                          "provider_failures": sum(bool(row["provider_error"]) for row in rows)}
    for phase in ("first_fields", "final_fields"):
        counts = [value for row in rows for value in decode_json_object(row[phase]).values()]
        summary[phase] = {"total": len(counts), "incorrect": counts.count("incorrect"), "missing": counts.count("missing")}
    for key in ("http_requests", "prompt_tokens", "completion_tokens"):
        values = [row.get(key) for row in rows]
        summary[key] = sum(require_json_integer(value, key) for value in values) if all(type(v) is int for v in values) else None
    return summary


def wait_for_idle(base_url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = service_state(base_url)
        if state.get("idle") == 1 and state.get("queue") == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("local model service did not become idle")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=12)
    parser.add_argument("--max-calls", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    if min(args.pairs, args.max_calls, args.timeout) <= 0:
        parser.error("pairs, max-calls and timeout must be positive")
    root: Path = args.run_dir
    root.mkdir(parents=True)
    target = gate.select_target(args.timeout, args.base_url)
    source = production_source_digest()
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    request_clock = clock_context()
    write_json(root / "manifest.json", {"model": target.model, "seed": target.seed, "pairs": args.pairs,
        "max_calls": args.max_calls, "production_digest": source, "script_sha256": script_hash, "temperature": 0,
        "request_clock": request_clock})
    rows: list[JsonObject] = []
    for index in range(1, args.pairs + 1):
        pair = root / f"{index:02d}"
        pair.mkdir()
        evidence = acquire(pair / "acquisition", target, index)
        order: tuple[Arm, Arm] = ("A", "B") if index % 2 else ("B", "A")
        for arm in order:
            wait_for_idle(target.base_url)
            print(f"pair {index:02d} {arm}", flush=True)
            with patch("codey.providers.request_context.clock_context", return_value=request_clock):
                rows.append({"pair": index, **run_arm(pair / arm, target, arm, evidence, args.max_calls)})
            write_json(root / "progress.json", rows)
        if rows[-1]["evidence_sha256"] != rows[-2]["evidence_sha256"]:
            raise ValueError("arms received different receipts")
    unchanged = source == production_source_digest()
    stable_script = script_hash == hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result = {"production_unchanged": unchanged, "script_unchanged": stable_script, "rows": rows,
              "arms": {arm: aggregate([row for row in rows if row["arm"] == arm]) for arm in ("A", "B")}}
    write_json(root / "summary.json", result)
    return 0 if unchanged and stable_script and not any(row["provider_error"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
