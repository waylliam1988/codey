"""Batch 2: observed host facts followed by one bounded model interpretation.

This supplements real HTTP approval tests; it does not claim a model chose
or executed these commands. Run Gemma before Solar. No settings are changed.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.policies.command_line import resolve_shell
from codey.runtime.core import cancellation
from codey.runtime.core.execution_receipt import ExecutionReceipt
from codey.storage.execution_output_store import ExecutionOutputObserver, ExecutionOutputStore
from codey.types.json_value import JsonObject, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import parse_json_reply, select_target


def host_facts(root: Path) -> tuple[JsonObject, JsonObject]:
    shell = resolve_shell()
    facts: JsonObject = {"shell": shell.to_payload()}
    expected: JsonObject = {}
    command = 'python -c "import sys; print(\'EXECUTION_PROOF\'); sys.exit(7)"'
    cases: list[tuple[str, str | list[str], str, int]] = [
        ("native", [sys.executable, "-c", "import sys;print('EXECUTION_PROOF');sys.exit(7)"], "process", 7),
        ("shell", shell.invocation(command), "shell", 1 if shell.kind == "powershell" else 7),
    ]
    if shell.kind == "powershell":
        cases.extend([
            ("trailing_output", shell.invocation(command + "; Write-Output after"), "shell", 0),
            ("explicit_exit", shell.invocation(command + "; exit $LASTEXITCODE"), "shell", 7),
        ])
    for name, argv, scope, code in cases:
        result = cancellation.run_process(argv, cwd=root, timeout=20, capture_limit_bytes=4096)
        if result.returncode != code:
            raise AssertionError(f"{name}: observed {result.returncode}, expected {code}")
        receipt = ExecutionReceipt.from_payload({"status": "exit", "exit_code": result.returncode,
            "output": result.stdout + result.stderr, "exit_code_scope": scope}).bounded()
        facts[name] = receipt.render()
        expected[name] = {"exit_code": code, "exit_code_scope": scope}
    store = ExecutionOutputStore(root, max_bytes=4096)
    store.create("encoding", session_id="gate", run_id="gate")
    observer = ExecutionOutputObserver(store, "encoding")
    script = "import sys;sys.stdout.buffer.write('中文输出'.encode('utf-8'));sys.stderr.buffer.write('中文错误'.encode('gbk'))"
    result = cancellation.run_process([sys.executable, "-c", script], cwd=root, timeout=20,
        capture_limit_bytes=4096, observer=observer)
    snapshot = store.settled("encoding", result.returncode)
    if result.stdout != snapshot["stdout"]["text"] or result.stderr != snapshot["stderr"]["text"]:
        raise AssertionError("capture/store decoding mismatch")
    if "中文输出" not in result.stdout or "中文错误" not in result.stderr:
        raise AssertionError("host code page did not decode Chinese; inspect host encoding before model use")
    facts["encoding"] = {"stdout": result.stdout, "stderr": result.stderr}
    expected["chinese_error"] = "中文错误"
    store.close()
    return facts, expected


def analyze_reply(facts: JsonObject, actual: JsonObject, expected: JsonObject) -> JsonObject:
    encoding = decode_json_object(facts["encoding"])
    # An encoding annotation copied from the actual stderr is still factual.
    # Track the requested presentation separately from execution correctness.
    expected_codes = {key: value for key, value in expected.items() if key != "chinese_error"}
    actual_codes = {key: value for key, value in actual.items() if key != "chinese_error"}
    codes_match = json.dumps(actual_codes, sort_keys=True) == json.dumps(expected_codes, sort_keys=True)
    chinese_matches = actual.get("chinese_error") in (expected["chinese_error"], encoding["stderr"])
    return {"ok": codes_match and chinese_matches, "exact_format": actual == expected,
            "execution_facts_match": codes_match, "chinese_facts_match": chinese_matches}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--model", choices=("gemma", "solar"), required=True)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="codey-encoding-") as temporary:
        facts, expected = host_facts(Path(temporary))
    attempts.write_json(args.run_dir / "host.json", facts)
    target = select_target(args.model, 180, "http://127.0.0.1:5001/v1")
    provider = attempts.make_provider(target, args.run_dir)
    prompt = ("以下是生产执行器的实测回执，不是执行请求。只按观察值返回裸JSON。"
        "对native、shell、trailing_output、explicit_exit中出现的每项，返回exit_code和exit_code_scope；"
        "另外chinese_error返回stderr实际中文错误正文，不含编码说明。不要从命令猜测退出码。\n"
        + json.dumps(facts, ensure_ascii=False))
    try:
        with attempts.record_api_generations(args.run_dir / "provider.jsonl"):
            reply = provider.send(prompt, timeout=180, output_token_limit=512)
    finally:
        provider.close()
    (args.run_dir / "reply.txt").write_text(reply, encoding="utf-8")
    actual = decode_json_object(parse_json_reply(reply))
    analysis = analyze_reply(facts, actual, expected)
    attempts.write_json(args.run_dir / "summary.json", {**analysis, "model": args.model,
        "scope": "production host facts and controlled interpretation", "expected": expected, "actual": actual})
    print(json.dumps({**analysis, "model": args.model, "run_dir": str(args.run_dir)}))
    return 0 if analysis["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
