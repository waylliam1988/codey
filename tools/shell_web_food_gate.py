"""Live ordinary-chat food lookup; validate actual opened evidence, never snippets.

Run Gemma first. Solar requires an existing passing Gemma report via --local-report.
Artifacts contain model payloads and source text, but no authorization headers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codey.types.json_value import JsonObject, decode_json_object
from tools import local_model_gate_attempts as attempts
from tools.shell_web_diagnostic_gate import parse_json_reply, select_target

OPEN_TOOLS = {"open_url", "open_result", "open_hit", "reopen_source"}
TASK = (
    "你帮我上网看看福州有什么好吃的。"
    "这是普通聊天联网验收：先实际 web_search，保持福州和美食原意；"
    "选择结果中的具体美食文章，用 open_url 读取正文。一个可读来源足够，不扩展研究。"
    "从最后一次成功打开的正文里逐字复制三个独立美食名称；已有三个就直接结束。"
    "web_search 摘要和 source_search 定位预览都不是打开后的正文。"
    "不要选择只在摘要、定位预览或常识中出现、实际打开段落里没有显示的名菜。"
    '最后用 done.summary 返回裸 JSON：{"foods":["原文名称1","原文名称2","原文名称3"],'
    '"sources":["实际读取的URL"],"reason":"依据正文的简短中文推荐理由"}。'
    "不要运行 Shell、修改文件、启动浏览器或进行知识库写入。"
)


def opened_bodies(directory: Path) -> list[str]:
    """Only tool-result bodies actually delivered in subsequent model requests."""
    bodies: list[str] = []
    for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") != "request":
            continue
        for message in row.get("payload", {}).get("messages", []):
            content = message.get("content", "")
            if not isinstance(content, str):
                continue
            candidates: list[tuple[str, str]] = []
            if message.get("role") == "tool":
                metadata = re.search(r'Result metadata:\s*(\{[^\n]+\})', content)
                if metadata:
                    candidates.append((str(json.loads(metadata[1]).get("tool", "")), content))
            elif message.get("role") == "user":
                blocks = re.split(r"\[result: ([a-z_]+)\]\n", content)
                candidates.extend((blocks[i], blocks[i + 1]) for i in range(1, len(blocks) - 1, 2))
            for name, body in candidates:
                if name in OPEN_TOOLS and '"ok":false' not in body.replace(" ", ""):
                    bodies.append(body.split("Result metadata:", 1)[0])
    return list(dict.fromkeys(bodies))


def analyze(directory: Path) -> JsonObject:
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    terminal = next((row for row in reversed(events) if row.get("type") == "task_done"), {})
    actual: JsonObject = {}
    format_error = ""
    if terminal.get("stop_reason") == "done":
        try:
            actual = decode_json_object(parse_json_reply(terminal.get("summary", "")))
        except ValueError as exc:
            format_error = str(exc)
    queries = [str(row.get("path", "")) for row in events
               if row.get("type") == "tool" and row.get("tool_name") == "web_search" and row.get("ok") is True]
    query_ok = bool(queries) and all(
        re.search(r"福州|fuzhou", q, re.I) and re.search(r"美食|小吃|好吃|food|cuisine", q, re.I) for q in queries)
    opens = [str(row.get("path", "")) for row in events
             if row.get("type") == "tool" and row.get("tool_name") in OPEN_TOOLS and row.get("ok") is True]
    bodies = opened_bodies(directory)
    pages: dict[str, str] = {}
    for body in bodies:
        if match := re.search(r"^URL: ([^\n]+)", body, re.MULTILINE):
            url = match[1].strip()
            pages[url] = pages.get(url, "") + "\n" + body[match.end():]
    foods, sources = actual.get("foods"), actual.get("sources")
    sources_ok = (isinstance(sources, list) and bool(sources) and bool(opens)
                  and all(isinstance(url, str) and url in pages for url in sources))
    evidence = "\n".join(pages[str(url)] for url in sources) if sources_ok and isinstance(sources, list) else ""
    foods_ok = (isinstance(foods, list) and len(foods) == 3 and all(isinstance(f, str) and f for f in foods)
                and len({str(f) for f in foods}) == 3 and all(str(f) in evidence for f in foods))
    ok = terminal.get("stop_reason") == "done" and query_ok and foods_ok and sources_ok
    return {"ok": ok, "scope": "production_ordinary_chat_food_lookup", "stop_reason": terminal.get("stop_reason"),
            "query_preserved": query_ok, "queries": queries, "opened_urls": opens,
            "food_names_in_delivered_page_body": foods_ok, "sources_match_opened_pages": sources_ok,
            "delivered_open_bodies": len(bodies), "actual": actual, "format_error": format_error,
            "limits": "One live lookup; checks exact food names and opened URLs, not all natural-language claims."}


def run(target: attempts.GateTarget, directory: Path) -> JsonObject:
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.env_names import NATIVE_TOOLS_ENV

    def observe(row: dict) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def connect(provider_id: str, **kwargs):
        if provider_id != target.provider_id:
            raise ValueError("gate cannot switch provider")
        return attempts.make_provider(target, directory)

    selection = {"model": target.model}
    if target.provider_id == "local":
        selection["base_url"] = target.base_url
    with (
        tempfile.TemporaryDirectory(prefix="codey-food-gate-") as temporary,
        patch.dict(os.environ, {NATIVE_TOOLS_ENV: "1"}),
        attempts.isolate_gate_secondary_models(target),
        attempts.record_api_generations(directory / "provider.jsonl"),
    ):
        run_headless(HeadlessRequest(
            project=None, task=TASK, intent="chat", provider_id=target.provider_id, max_turns=8,
            state_home=Path(temporary), model_selection=selection, requested_capabilities=(),
            project_changes_required=False), emit_jsonl=observe, connect_provider=connect)
    return analyze(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--model", choices=("gemma", "solar"), default="gemma")
    parser.add_argument("--local-report", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--reanalyze", type=Path)
    args = parser.parse_args()
    if args.reanalyze:
        manifest = json.loads((args.reanalyze / "manifest.json").read_text(encoding="utf-8"))
        report = {**analyze(args.reanalyze), "model_label": manifest["model_label"], "model": manifest["model"]}
        (args.reanalyze / "summary.reanalyzed.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        if args.run_dir is None:
            parser.error("--run-dir is required")
        if args.model == "solar":
            if args.local_report is None:
                parser.error("Solar requires --local-report after local Gemma passes")
            local = json.loads(args.local_report.read_text(encoding="utf-8"))
            if local.get("ok") is not True or local.get("model_label") != "gemma":
                parser.error("local food gate must pass before Solar")
        directory = args.run_dir.resolve()
        directory.mkdir(parents=True, exist_ok=False)
        target = select_target(args.model, args.timeout, "http://127.0.0.1:5001/v1")
        attempts.write_json(directory / "manifest.json", {
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "model": target.model, "model_label": args.model, "task": TASK,
        })
        report = {**run(target, directory), "model_label": args.model, "model": target.model}
        attempts.write_json(directory / "summary.json", report)
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
