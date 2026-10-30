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
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.wire.read_text(encoding="utf-8").splitlines()]
    payload = copy.deepcopy([row["payload"] for row in rows if row["event"] == "admitted_payload"][-1])
    payload.update(stream=False, max_tokens=2048)
    payload.pop("stream_options", None)
    payload["chat_template_kwargs"]["enable_thinking"] = False
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
    print(json.dumps({"seconds": time.monotonic() - started, "response": body}, ensure_ascii=False))


if __name__ == "__main__":
    main()
