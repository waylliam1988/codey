"""Compare frozen failed request envelopes without executing any model tools."""
from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wire", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--merge-clock", action="store_true")
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--request-number", type=int, default=-1)
    parser.add_argument("--phase-guidance", action="store_true")
    parser.add_argument("--max-output", type=int, default=2048)
    parser.add_argument("--capture-native-generation", action="store_true")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.wire.read_text(encoding="utf-8").splitlines()]
    payloads = [row["payload"] for row in rows if row["event"] == "admitted_payload"]
    payload = copy.deepcopy(payloads[args.request_number - 1 if args.request_number > 0 else args.request_number])
    payload.update(stream=False, max_tokens=args.max_output)
    payload.pop("stream_options", None)
    payload["chat_template_kwargs"]["enable_thinking"] = False
    if args.phase_guidance:
        import ast
        import subprocess

        from codey.operations.kernel_prompt import _native_done_guidance
        from codey.operations.kernel_protocol import build_turn_snapshot
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy

        old_source = subprocess.check_output(["git", "show", "922cf231:codey/operations/kernel_prompt.py"], text=True)
        old_rule = next(ast.literal_eval(node.value) for node in ast.parse(old_source).body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_NATIVE_FINAL_RULE"
                                                   for target in node.targets))
        snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True),
                                                   task_kind="research"), native=True)
        done = next(spec for spec in snapshot.model_specs if spec.name == "done")
        changed = 0
        for message in payload["messages"]:
            content = message.get("content")
            if isinstance(content, str) and old_rule in content:
                message["content"] = content.replace(old_rule, _native_done_guidance(done))
                changed += 1
        if not changed:
            raise ValueError("frozen request did not contain the original completion rule")
    if args.merge_clock:
        messages = payload["messages"]
        assert messages[-1]["content"].startswith("<request_context>")
        assert messages[-2]["role"] == messages[-1]["role"] == "user"
        messages[-2]["content"] = messages[-1]["content"] + "\n\n" + messages[-2]["content"]
        messages.pop()
    args.directory.mkdir(parents=True, exist_ok=False)
    (args.directory / "request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    started = time.monotonic()
    request = Request(args.upstream + "/v1/chat/completions", json.dumps(payload).encode(),
                      {"Content-Type": "application/json"})
    with urlopen(request, timeout=300) as response:
        body = json.load(response)
    (args.directory / "response.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.capture_native_generation:
        check = Request(args.upstream + "/api/extra/generate/check", b"{}", {"Content-Type": "application/json"})
        with urlopen(check, timeout=10) as response:
            raw = json.load(response)
        (args.directory / "native-generation.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"seconds": time.monotonic() - started, "response": body}, ensure_ascii=False))


if __name__ == "__main__":
    main()
