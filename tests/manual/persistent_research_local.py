"""Local model research/recall probe through production headless task entry.

Uses a frozen copy of public SQLite documentation to isolate model behavior
from search ranking. Each stage is a separate process and AppContext. This is
a small capability probe, not a general research-quality benchmark.
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from urllib.request import urlopen

URLS = ("https://www.sqlite.org/wal.html", "https://www.sqlite.org/isolation.html")


def stage(directory, name, upstream, model, native=False, live_web=False, question="", max_turns=12, thinking=False,
          temperature=0.3, seed=73, diagnostic_stacks=False, reasoning_effort=None, session_id=None,
          provider_timeout=600):
    from unittest.mock import patch

    from codey.app import headless_runner
    from codey.providers import api_transport, local_config
    from codey.providers.api_provider import ApiProvider
    from codey.providers.local_response_codec import normalize_local_reply
    from codey.providers.local_tokens import admitted_counter, counter_for
    from codey.providers.local_usage import configure_usage, parser_for
    from codey.providers.model_preferences import ModelPreferences

    documents = [] if live_web else json.loads((directory / "sources.json").read_text(encoding="utf-8"))
    session_id = session_id or f"research-{name}"
    (directory / f"{name}-control.json").write_text(json.dumps({
        "model": model, "temperature": temperature, "seed": seed, "native_tools": native,
        "stream": native, "thinking": thinking, "live_web": live_web, "max_turns": max_turns,
        "question": question,
        "diagnostic_stacks": diagnostic_stacks,
        "reasoning_effort": reasoning_effort,
        "session_id": session_id,
        "provider_timeout": provider_timeout,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    events = directory / f"{name}-events.jsonl"
    wire = directory / f"{name}-wire.jsonl"
    stacks = (directory / f"{name}-stacks.txt").open("a", encoding="utf-8")
    if diagnostic_stacks:
        faulthandler.dump_traceback_later(60, repeat=True, file=stacks)

    def record(row):
        with wire.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.time(), **row}, ensure_ascii=False, default=str) + "\n")

    decode_json = api_transport._json_object

    def observed_json(raw):
        record({"event": "response_bytes", "bytes": len(raw), "text": raw.decode("utf-8", "replace")[:16000]})
        return decode_json(raw)

    class ObservedProvider(ApiProvider):
        def _generate(self, messages, tools=None, **kwargs):
            record({"event": "generation_start", "messages": messages, "tools": [t.name for t in tools or ()]})
            started = time.monotonic()
            try:
                body = super()._generate(messages, tools, **kwargs)
            except Exception as exc:
                record({"event": "generation_failure", "error": repr(exc), "cause": repr(exc.__cause__),
                        "seconds": time.monotonic() - started})
                raise
            record({"event": "generation_response", "body": body, "seconds": time.monotonic() - started})
            return body

    class Search:
        def search(self, query, limit=8):
            return [{"title": doc["title"], "url": doc["url"], "snippet": doc["text"][:160]}
                    for doc in documents]

        def fetch(self, url):
            return next({"status": "ok", "truncated": False, **doc} for doc in documents if doc["url"] == url)

        def close(self):
            pass

    class Context(headless_runner.HeadlessAppContext):
        def __init__(self, state_home, *args, **kwargs):
            preferences = ModelPreferences(state_home)
            snapshot = preferences.snapshot()
            sources = {key: {"enabled": False, "models": []} for key in snapshot["sources"]}
            sources["local"] = {"enabled": True, "models": [model]}
            preferences.save(sources, base_revision=snapshot["revision"])
            super().__init__(state_home, *args, **kwargs)

    def connect(provider_id, **kwargs):
        if provider_id != "local":
            raise ValueError("probe permits local model only")
        def configure(payload):
            configure_usage(payload)
            payload.update(seed=seed)
            record({"event": "admitted_payload", "payload": payload})

        counter, window = admitted_counter(upstream + "/v1", "", "openai-completions", 32768)
        record({"event": "connection_budget", "counter": counter, "window": window})
        return ObservedProvider(upstream + "/v1", model, timeout=provider_timeout, temperature=temperature,
                           thinking_enabled=thinking, reasoning_effort=reasoning_effort,
                           output_tokens=5000 if thinking or reasoning_effort else 2500, context_reserve_tokens=5500, native_tools=native,
                           stream=native, context_window_tokens=window,
                           request_counter=counter_for(counter, upstream + "/v1", model, "", window),
                           usage_parser=parser_for("openai-completions"),
                           budget_source="server_and_configuration" if counter == "koboldcpp" else "configuration",
                           text_decoder=normalize_local_reply,
                           configure_request=configure)

    tasks = {
        "research": question,
        "first": (
            "研究 SQLite WAL：为什么读写可并发，而写者仍竞争？搜索并打开官方文档。"
            "分别用两次knowledge_write保存两条中文主张，每条配各自的官方英文原文：读写互不阻塞；同一时刻只有一个写者。"
            "找不到单写者原文就source_search查writer，并open_hit读取相应段落。"
            '每次knowledge_write都必须包含 aliases=["嵌入式数据文件"]、tags=["SQLite","写者串行化"]，'
            '以及 conditions={"mode":{"state":"reported","value":"WAL"},'
            '"workload":{"state":"not_extracted"}}，因为实际工作负载尚未测量。'
            "然后给不超过500字的中文报告，明确外推边界。使用done.report_sections，summary只填结论，"
            "其余五部分各用字符串数组。每句分别引用[1]，报告沿用已存中文主张，来源表填官方URL。"
            "knowledge_refs引用工具返回的claim对象id和revision。完成前检查两个条件都已保存。"
        ),
        "recall": (
            "一个队列系统增加了处理者，但所有提交都要经过唯一闸门。我想借鉴以前研究过的嵌入式数据文件："
            "这两种情况是否可能共享瓶颈机制？请先检索长期知识、读取相关知识版本，说明为什么召回它，"
            "再解释类比的边界。无需新的网页；不要把类比当成经过验证的队列性能结论。"
            "保存一个有前提引用的候选推论（knowledge_commit，kind=inference，origin=model_inference，"
            "status=candidate，references 使用 depends_on），并在 done 的 knowledge_refs 引用实际采用的原知识版本。"
        ),
        "revise": (
            "检索刚才关于队列唯一闸门的候选推论，读取其当前版本。我们尚未测量实际队列的工作负载，"
            "请保留其身份、内容和已有引用，把 conditions 中 workload 标为 not_extracted，并增加"
            "provenance 中 interpretation='analogy_requires_queue_measurement'。用 knowledge_commit 提交修订，"
            "expected_revision 必须来自读取结果。不要编造实验，最后说明这只是适用条件修订。"
        ),
    }

    def emit(payload):
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    with ExitStack() as stack:
        stack.enter_context(patch.object(headless_runner, "HeadlessAppContext", Context))
        stack.enter_context(patch.object(api_transport, "_json_object", observed_json))
        stack.enter_context(patch.object(local_config, "DEFAULT_STATE_HOME", directory / "state"))
        stack.enter_context(patch.dict(os.environ, {"LOCAL_OPENAI_BASE_URL": upstream + "/v1", "LOCAL_OPENAI_MODEL": model}))
        if not live_web:
            stack.enter_context(patch("codey.research.search_factory.default_research_search_provider", Search))
            stack.enter_context(patch("codey.app.task_services.default_research_search_provider", Search, create=True))
        local_config.save_local_config(local_config.LocalProviderConfig(base_url=upstream + "/v1", model=model,
            native_tools_mode="on" if native else "off", thinking_enabled=thinking, connection_revision="persistent-research-probe"))
        request = headless_runner.HeadlessRequest(project=None, task=tasks[name], provider_id="local",
            max_turns=max_turns, session_id=session_id,
            state_home=directory / "state", research_store_root=directory / "knowledge",
            intent="research" if name in {"first", "research"} else "hybrid", sources_open_required=live_web,
            required_tools=("knowledge_read", "knowledge_commit") if name in {"recall", "revise"} else (),
            requested_capabilities=("knowledge.read", "knowledge.write", "web.read"))
        result = headless_runner.run_headless(request, emit_jsonl=emit, connect_provider=connect,
                                             connect_reviewer=connect if name in {"recall", "revise"} else None)
    faulthandler.cancel_dump_traceback_later()
    stacks.close()
    (directory / f"{name}-result.json").write_text(json.dumps(vars(result), ensure_ascii=False, indent=2), encoding="utf-8")
    events_seen = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    final = next((row for row in reversed(events_seen) if row.get("type") == "task_done"), {})
    (directory / f"{name}-report.txt").write_text(final.get("summary", ""), encoding="utf-8")
    from codey.knowledge.store import KnowledgeStore

    store = KnowledgeStore(directory / "knowledge")
    try:
        rows = store.index._read_conn().execute(
            "SELECT v.id,v.revision FROM knowledge_versions v JOIN knowledge_heads h "
            "ON h.id=v.id AND h.revision=v.revision WHERE v.kind=?",
            ("claim" if name in {"first", "research"} else "inference",)).fetchall()
        versions = [store.repository.read(row[0], row[1]) for row in rows]
        snapshot = []
        for version in versions:
            row = version.to_dict()
            row["referenced_versions"] = [
                target.to_dict() for ref in version.value.references
                if (target := store.repository.read(ref.target, ref.revision)) is not None
            ]
            snapshot.append(row)
        (directory / f"{name}-knowledge.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.exit_code:
            raise RuntimeError(f"{name} failed: {vars(result)}")
        observed = {row.get("tool_name") for row in events_seen if row.get("type") == "tool" and row.get("ok")}
        if name == "research":
            if (not {"web_search", "knowledge_write"} <= observed
                    or not observed.intersection({"open_url", "open_result", "open_hit"})
                    or not any(any(r.relation == "supported_by" for r in v.value.references) for v in versions)):
                raise RuntimeError("live research must actually search, open a page and persist sourced claims")
            return
        if name == "first":
            excerpts = [" ".join(ref["content"].casefold().split()) for row in snapshot for ref in row["referenced_versions"]
                        if ref["kind"] == "evidence"]
            accepted = (len([v for v in versions if any(r.relation == "supported_by" for r in v.value.references)]) >= 2
                        and any("readers do not block writers" in text for text in excerpts)
                        and any(any(phrase in text for phrase in (
                            "only one writer", "single writer", "there can only be one writer at a time"))
                            for text in excerpts)
                        and any("嵌入式数据文件" in v.value.aliases for v in versions)
                        and any(v.value.conditions.get("workload", {}).get("state") == "not_extracted" for v in versions))
        elif name == "recall":
            # The shared context already performs scoped retrieval; an extra
            # search tool is optional when it supplied the relevant versions.
            accepted = {"knowledge_read", "knowledge_commit"} <= observed and any(
                           v.value.status == "candidate" and any(r.relation == "depends_on" for r in v.value.references)
                           for v in versions)
        else:
            accepted = {"knowledge_read", "knowledge_commit"} <= observed and any(
                           v.revision >= 2 and v.value.conditions.get("workload", {}).get("state") == "not_extracted"
                           and v.value.provenance.get("interpretation") == "analogy_requires_queue_measurement"
                           for v in versions)
        if not accepted:
            raise RuntimeError(f"{name} returned a final answer but did not persist its required knowledge changes")
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--stage", choices=("first", "recall", "revise", "research"))
    parser.add_argument("--session-id", help="Use a fresh conversation for a diagnostic retry on the same knowledge store")
    parser.add_argument("--native", action="store_true", help="Use the server's structured tool protocol")
    parser.add_argument("--thinking", action="store_true", help="Enable the model's reasoning mode")
    parser.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high"),
                         help="Set the reasoning budget; combine with --thinking to enable model reasoning")
    parser.add_argument("--diagnostic-stacks", action="store_true", help="Opt in to periodic CPython thread dumps")
    parser.add_argument("--live-web", action="store_true", help="Use production search and browser page acquisition")
    parser.add_argument("--question-file", type=Path)
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--stage-timeout", type=int, default=900)
    parser.add_argument("--provider-timeout", type=float, default=600,
                        help="Per-request deadline; defaults to the production ApiProvider budget")
    parser.add_argument("--temperature", type=float, default=0.3, help="Local production default; recorded per stage")
    parser.add_argument("--seed", type=int, default=73, help="Fixed sampling seed for reproducible diagnostics")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with urlopen(args.upstream + "/v1/models", timeout=5) as response:
        model = json.load(response)["data"][0]["id"]
    if args.stage:
        question = args.question_file.read_text(encoding="utf-8") if args.question_file else ""
        if args.stage == "research" and (not args.live_web or not question.strip()):
            parser.error("research requires --live-web and --question-file")
        stage(directory, args.stage, args.upstream, model, args.native, args.live_web, question, args.max_turns,
              args.thinking, args.temperature, args.seed, args.diagnostic_stacks, args.reasoning_effort, args.session_id,
              args.provider_timeout)
        return
    documents = []
    from codey.research.extract import extract_text, extract_title

    for url in URLS:
        with urlopen(url, timeout=30) as response:
            html = response.read().decode("utf-8")
        text = extract_text(html)
        documents.append({"url": url, "title": extract_title(html), "text": text,
                          "sha256": hashlib.sha256(text.encode()).hexdigest()})
    (directory / "sources.json").write_text(json.dumps(documents, ensure_ascii=False), encoding="utf-8")
    (directory / "control.json").write_text(json.dumps({"model": model, "temperature": args.temperature, "seed": args.seed,
        "native_tools": args.native, "thinking": args.thinking, "reasoning_effort": args.reasoning_effort,
        "source_mode": "frozen public documentation",
        "processes": ["first", "recall", "revise"]}, indent=2), encoding="utf-8")
    for name in ("first", "recall", "revise"):
        subprocess.run([sys.executable, "-X", "utf8", "-m", "tests.manual.persistent_research_local",
                        "--directory", str(directory), "--upstream", args.upstream, "--stage", name,
                        "--temperature", str(args.temperature), "--seed", str(args.seed),
                        "--provider-timeout", str(args.provider_timeout),
                        "--max-turns", str(args.max_turns), *(["--native"] if args.native else []),
                        *(["--diagnostic-stacks"] if args.diagnostic_stacks else []),
                        *(["--reasoning-effort", args.reasoning_effort] if args.reasoning_effort else []),
                        *(["--thinking"] if args.thinking else [])],
                       check=True, timeout=args.stage_timeout)


if __name__ == "__main__":
    main()
