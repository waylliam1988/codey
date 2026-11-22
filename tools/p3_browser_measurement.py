"""Measure Chromium observation and delivery on fixed production browser fixtures."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from collections import Counter
from pathlib import Path
from statistics import median
from unittest.mock import patch
from uuid import uuid4

from playwright._impl._connection import ChannelOwner, Connection, ProtocolCallback

from codey.automation.browser_contracts import BrowserAction, BrowserCommand, BrowserConfig, BrowserToolOutput
from codey.automation.browser_runtime import BrowserRuntime
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string
from tools.completion_explanation_ab import production_source_digest, write_json


async def action(runtime: BrowserRuntime, name: BrowserAction, args: JsonObject | None = None) -> BrowserToolOutput:
    packet = await runtime.dispatch(BrowserCommand(uuid4().hex, name, args or {}, "p3", "p3",
        uuid4().hex, time.monotonic() + 25))
    return BrowserToolOutput.read(packet.message["value"])


async def measure(repeats: int) -> JsonObject:
    html = ""

    async def route(_: JsonObject) -> JsonObject:
        return {"decision": "fulfill", "content_type": "text/html", "headers": {"content-type": "text/html"}, "body": html}

    runtime = BrowserRuntime(BrowserConfig(), network_policy=route)
    calls: Counter[str] = Counter()
    send = Connection._send_message_to_server

    def count(connection: Connection, target: ChannelOwner, method: str,
              params: dict[str, object], no_reply: bool = False) -> ProtocolCallback:
        protocol_method = params.get("method") if method == "send" else None
        calls["cdp:" + protocol_method if isinstance(protocol_method, str) else "playwright:" + method] += 1
        return send(connection, target, method, params, no_reply)

    results: list[JsonValue] = []
    scenarios = (("ordinary", 10), ("large", 150), ("hidden_form", 12), ("frame", 12), ("partially_covered", 1))
    try:
        for name, controls in scenarios:
            html = "".join(f'<section aria-label="Group {start}">' + "".join(
                f'<button onclick="window.hits=(window.hits||[]).concat({index})">Control {index}</button>'
                for index in range(start, min(controls, start + 25))) + "</section>" for start in range(0, controls, 25))
            if name == "hidden_form":
                html += '<form><label>Upload <input type="file" hidden></label><button>Choose upload</button></form>'
            if name == "frame":
                from html import escape

                html = '<iframe title="Fixture frame" srcdoc="' + escape(html, quote=True) + '"></iframe>'
            if name == "partially_covered":
                html = '<style>button{width:160px;height:90px}.cover{position:absolute;left:66px;top:36px;width:40px;height:30px;background:gray}</style>' + html + '<div class="cover"></div>'
            opened = await action(runtime, BrowserAction.NAVIGATE, {"url": "https://example.com/" + name})
            if not opened.ok:
                raise RuntimeError(opened.model_text)
            if name == "frame":
                frame_list = await action(runtime, BrowserAction.FRAME_LIST)
                frames = frame_list.observation.get("frames")
                if not isinstance(frames, list):
                    raise ValueError("frame identities are unavailable")
                frame = decode_json_object(frames[-1])
                selected = await action(runtime, BrowserAction.FRAME_SELECT,
                    {"frame_id": require_json_string(frame["frame_id"], "frame id")})
                if not selected.ok:
                    raise RuntimeError(selected.model_text)
            args: JsonObject = {"selector": 'section[aria-label="Group 100"]'} if name == "large" else {}
            target_index = 119 if name == "large" else controls - 1
            timings: list[float] = []
            samples: list[JsonValue] = []
            correct = 0
            last_ref = ""
            for _ in range(repeats):
                calls.clear()
                started = time.perf_counter()
                with patch.object(Connection, "_send_message_to_server", count):
                    snapshot = await action(runtime, BrowserAction.SNAPSHOT, args)
                seconds = time.perf_counter() - started
                if not snapshot.ok:
                    raise RuntimeError(snapshot.model_text)
                timings.append(seconds)
                samples.append({"seconds": seconds, "rpc": sum(calls.values()), "by_method": dict[str, JsonValue](calls)})
                elements = snapshot.observation.get("elements")
                if isinstance(elements, list):
                    matches = [row for row in elements if isinstance(row, dict) and row.get("name") == f"Control {target_index}"]
                    if len(matches) == 1:
                        last_ref = require_json_string(matches[0]["ref"], "control ref")
                        correct += 1
            clicked = await action(runtime, BrowserAction.CLICK, {"ref": last_ref}) if last_ref else None
            hits = await action(runtime, BrowserAction.EVALUATE, {"code": "window.hits || []"})
            delivered = hits.observation.get("value")
            timings.sort()
            results.append({"scenario": name, "controls": controls, "p50_ms": round(median(timings) * 1000, 2),
                "p95_ms": round(timings[math.ceil(.95 * len(timings)) - 1] * 1000, 2),
                "correct_observations": correct, "samples": samples, "clicked": bool(clicked and clicked.ok),
                "expected_hits": [target_index], "hits": delivered, "success": correct == repeats and delivered == [target_index]})
    finally:
        await runtime.close()
    return {"scenarios": results}


class Options(argparse.Namespace):
    directory: Path
    repeats: int


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(namespace=Options())
    if args.repeats < 1:
        parser.error("repeats must be positive")
    args.directory.mkdir(parents=True, exist_ok=False)
    before = production_source_digest()
    report = asyncio.run(measure(args.repeats))
    report.update(source_before=before, source_after=production_source_digest())
    write_json(args.directory / "summary.json", report)
    print(json.dumps(report, ensure_ascii=True))
    rows = report["scenarios"]
    return 0 if isinstance(rows, list) and all(decode_json_object(row)["success"] is True for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
