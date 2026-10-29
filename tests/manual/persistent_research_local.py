"""Local model research/recall probe through production headless task entry.

Uses a frozen copy of public SQLite documentation to isolate model behavior
from search ranking. Each stage is a separate process and AppContext. This is
a small capability probe, not a general research-quality benchmark.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import urlopen

URLS = ("https://www.sqlite.org/wal.html", "https://www.sqlite.org/isolation.html")


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def stage(directory, name, upstream, model, native=False):
    from unittest.mock import patch

    from codey.app import headless_runner
    from codey.providers import local_config
    from codey.providers.api_provider import ApiProvider
    from codey.providers.local_response_codec import normalize_local_reply
    from codey.providers.model_preferences import ModelPreferences

    documents = json.loads((directory / "sources.json").read_text(encoding="utf-8"))
    events = directory / f"{name}-events.jsonl"

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
        return ApiProvider(upstream + "/v1", model, temperature=0.2, thinking_enabled=False,
                           output_tokens=2500, context_reserve_tokens=3000, native_tools=native,
                           text_decoder=normalize_local_reply,
                           configure_request=lambda payload: payload.update(seed=73))

    tasks = {
        "first": (
            "研究 SQLite WAL 在并发读写时的限制，重点是为什么多个读者可并发、写者仍会竞争。"
            "按顺序调用web_search、open_result、knowledge_write，最后done。"
            "knowledge_write.sources必须是已读取的URL字符串数组，不能填工具回执引用。"
            "使用提供的 SQLite 官方文档；先读取来源，再用 knowledge_write 保存准确引文、条件和限制，"
            "必须同时保存读写并发和仅单一写者两个结论的直接证据。"
            "evidence.claim用中文概述，evidence.excerpt保持官方英文原文；报告沿用已保存的中文主张。"
            "标签至少含 SQLite 和 写者串行化，别名含 嵌入式数据文件。"
            "不要保存只有报告摘要的笔记。最后给出简短、带来源的研究报告，明确何时不能外推。"
            "报告不超过500字，使用结论、关键证据、反证与限制、来源质量、搜索覆盖、来源六个标题。"
            "报告只概述证据并引用[1]，无需重复带双引号的英文引文；引用表使用[1]和实际URL。"
            "JSON summary中用单次JSON编码的换行，不要把字面的反斜杠n当段落。"
            "knowledge_refs使用工具返回的结构化对象id，不是note_id。"
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

    with (patch.object(headless_runner, "HeadlessAppContext", Context),
          patch.object(local_config, "DEFAULT_STATE_HOME", directory / "state"),
          patch.dict(os.environ, {"LOCAL_OPENAI_BASE_URL": upstream + "/v1", "LOCAL_OPENAI_MODEL": model}),
          patch("codey.research.search_factory.default_research_search_provider", Search),
          patch("codey.app.task_services.default_research_search_provider", Search, create=True)):
        local_config.save_local_config(local_config.LocalProviderConfig(base_url=upstream + "/v1", model=model,
            native_tools_mode="on" if native else "off", thinking_enabled=False, connection_revision="persistent-research-probe"))
        request = headless_runner.HeadlessRequest(project=None, task=tasks[name], provider_id="local",
            max_turns=12, session_id="research-first" if name == "first" else "research-later",
            state_home=directory / "state", research_store_root=directory / "knowledge",
            intent="research" if name == "first" else "hybrid", sources_open_required=False,
            requested_capabilities=("knowledge.read", "knowledge.write", "web.read"))
        result = headless_runner.run_headless(request, emit_jsonl=emit, connect_provider=connect,
                                             connect_reviewer=connect if name != "first" else None)
    (directory / f"{name}-result.json").write_text(json.dumps(vars(result), ensure_ascii=False, indent=2), encoding="utf-8")
    if result.exit_code:
        raise RuntimeError(f"{name} failed: {vars(result)}")
    from codey.knowledge.store import KnowledgeStore

    store = KnowledgeStore(directory / "knowledge")
    try:
        rows = store.index._read_conn().execute(
            "SELECT v.id,v.revision FROM knowledge_versions v JOIN knowledge_heads h "
            "ON h.id=v.id AND h.revision=v.revision WHERE v.kind=?",
            ("claim" if name == "first" else "inference",)).fetchall()
        versions = [store.repository.read(row[0], row[1]) for row in rows]
        if name == "first":
            accepted = len([v for v in versions if any(r.relation == "supported_by" for r in v.value.references)]) >= 2
        elif name == "recall":
            accepted = any(v.value.status == "candidate" and any(r.relation == "depends_on" for r in v.value.references)
                           for v in versions)
        else:
            accepted = any(v.revision >= 2 and v.value.conditions.get("workload", {}).get("state") == "not_extracted"
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
    parser.add_argument("--stage", choices=("first", "recall", "revise"))
    parser.add_argument("--native", action="store_true", help="Use the server's structured tool protocol")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with urlopen(args.upstream + "/v1/models", timeout=5) as response:
        model = json.load(response)["data"][0]["id"]
    if args.stage:
        stage(directory, args.stage, args.upstream, model, args.native)
        return
    documents = []
    for url in URLS:
        with urlopen(url, timeout=30) as response:
            html = response.read().decode("utf-8")
        parsed = TextParser()
        parsed.feed(html)
        text = "\n".join(parsed.parts)
        documents.append({"url": url, "title": parsed.parts[0], "text": text,
                          "sha256": hashlib.sha256(text.encode()).hexdigest()})
    (directory / "sources.json").write_text(json.dumps(documents, ensure_ascii=False), encoding="utf-8")
    (directory / "control.json").write_text(json.dumps({"model": model, "temperature": 0.2, "seed": 73,
        "native_tools": args.native, "source_mode": "frozen public documentation", "processes": ["first", "recall", "revise"]}, indent=2), encoding="utf-8")
    for name in ("first", "recall", "revise"):
        subprocess.run([sys.executable, "-X", "utf8", "-m", "tests.manual.persistent_research_local",
                        "--directory", str(directory), "--upstream", args.upstream, "--stage", name,
                        *(["--native"] if args.native else [])], check=True, timeout=420)


if __name__ == "__main__":
    main()
