"""Real local model on frozen official passages through the production kernel.

Fixture planning/reads are labeled setup, not model actions. The model generates
all findings and completion selections; the ordinary knowledge and publication
transactions remain active. This probe never grades its own semantic results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from tests.manual.persistent_research_local import stage

QUESTIONS = {
    "coexistence_only": "SQLite WAL 为什么仍存在单写者限制？",
    "writer_limit": "SQLite WAL 为什么仍存在单写者限制？",
    "versions": "SQLite WAL 的引入版本和只读打开的支持版本分别是什么？只读打开需要满足哪些条件？",
    "conditions": "writer 回绕复用 WAL 的条件是否也是 checkpoint 转移数据的必要条件？请保留各动作的前提和例外。",
    "measurement": "这些文档片段能否证明本机使用 WAL 比回滚日志更快，或者证明整页没有本机测量？",
    "readonly_conditions": "SQLite 的 WAL 数据库在只读媒体或缺乏数据库写权限时，什么条件下仍可读取？",
    "checkpoint_constraints": "SQLite WAL 的 checkpoint 与读事务并行时，何时必须停止，之后如何继续？",
    "checkpoint_stop": "SQLite WAL 的 checkpoint 与读事务并行时，何时必须停止，为什么？",
    "checkpoint_resume": "SQLite WAL 的 checkpoint 暂停后，如何记录进度，并在之后继续转移数据？",
}
PASSAGES = {
    "overview": ("There are advantages and disadvantages", "Transactions that involve"),
    "introduced": ("## 1. Overview", "WAL uses many fewer fsync()"),
    "writer": ("Writers merely append", "Thus a long-running read transaction"),
    "conditions": ("A checkpoint operation takes content", "## 2.3. Performance Considerations"),
    "readonly": ("## 5. Read-Only Databases", "## 6. Avoiding Excessively Large WAL Files"),
    "bug": ("## 11. The WAL-Reset Bug", "## 11.1. Bug Details"),
}
INPUTS = {
    "given": {
        "coexistence_only": ("overview",), "writer_limit": ("writer",),
        "versions": ("introduced", "readonly"), "conditions": ("conditions",), "measurement": ("overview",),
        "readonly_conditions": ("readonly",), "checkpoint_constraints": ("conditions",),
        "checkpoint_stop": ("conditions",), "checkpoint_resume": ("conditions",),
    },
    "candidates": {
        "coexistence_only": ("overview", "readonly"), "writer_limit": ("overview", "conditions", "writer"),
        "versions": ("bug", "introduced", "readonly"),
        "conditions": ("readonly", "conditions", "writer"), "measurement": ("overview", "readonly"),
        "readonly_conditions": ("introduced", "readonly"),
        "checkpoint_constraints": ("readonly", "conditions", "writer"),
        "checkpoint_stop": ("readonly", "conditions", "writer"),
        "checkpoint_resume": ("readonly", "conditions", "writer"),
    },
}

RELATION_TARGETS = {
    "versions": ["SQLite WAL 从哪个版本引入？", "SQLite 何时支持只读打开 WAL 数据库？保留支持版本、全部三个替代条件、或的关系和例外。"],
    "checkpoint_constraints": ["checkpoint 与读事务并行时何时必须停止，为什么？", "checkpoint 暂停后如何记录进度并在之后继续转移？"],
}


def candidate_refresh_calls(passage):
    """Scripted setup for the old-finding reread boundary, not model work."""
    from codey.runtime.core.models import ToolCall

    claims = ["由于只有一个 WAL 文件，因此同一时刻只能有一个写者。",
        "写者将新内容追加到 WAL 文件末尾。", "检查点将 WAL 文件中的内容转移到原数据库文件。",
        "检查点遇到越过当前读者 end mark 的页面时必须停止。",
        "检查点进度记录在 wal-index 中。", "后续检查点调用可以从记录的进度继续转移数据。"]
    return [*[ToolCall("save_finding", {"question_ref": "q1", "passage_ref": passage, "claim": claim})
              for claim in claims], ToolCall("knowledge_read", {"question_ref": "q1", "id": "f1"})]


def fixture_windows(text, case, layer, order_seed):
    """Candidate order is independent of the model sampling seed."""
    selectors = list(INPUTS[layer][case])
    random.Random(order_seed).shuffle(selectors)
    windows = []
    for name in selectors:
        start_text, end_text = PASSAGES[name]
        start = text.index(start_text)
        end = text.index(end_text, start)
        if end - start < 500:
            raise ValueError("fixture window must exceed the production minimum read size")
        windows.append({"selector": name, "offset": start, "limit": end - start,
                        "sha256": hashlib.sha256(text[start:end].encode()).hexdigest()})
    return windows


def main():
    from urllib.request import urlopen

    from codey.operations.kernel_facts import bind_source_identity, project_result_observation
    from codey.operations.research_continuation import ResearchContinuation
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research.task_requirements import coverage_requirements
    from codey.runtime.core.models import ToolCall

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-file", type=Path, required=True)
    parser.add_argument("--case", choices=QUESTIONS, required=True)
    parser.add_argument("--layer", choices=INPUTS, required=True)
    parser.add_argument("--seed", type=int, default=74)
    parser.add_argument("--order-seed", type=int, default=0)
    parser.add_argument("--split", action="store_true")
    parser.add_argument("--full-history", action="store_true",
                        help="Experiment only: retain full request history with the same local completion contract")
    parser.add_argument("--candidate-refresh-probe", action="store_true",
                        help="Script six candidate findings then reread f1 before real model selection")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--provider-timeout", type=float, default=120)
    args = parser.parse_args()
    if args.candidate_refresh_probe and (args.case != "writer_limit" or args.layer != "given" or args.split):
        parser.error("candidate refresh probe requires unsplit writer_limit with given evidence")
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(args.source_file, directory / "sources.json")
    documents = json.loads(args.source_file.read_text(encoding="utf-8"))
    source = next(row for row in documents if row["url"] == "https://www.sqlite.org/wal.html")
    if hashlib.sha256(source["text"].encode()).hexdigest() != source["sha256"]:
        raise ValueError("frozen source hash mismatch")
    windows = fixture_windows(source["text"], args.case, args.layer, args.order_seed)
    manifest = {"layer": args.layer, "case": args.case, "seed": args.seed,
                "order_seed": args.order_seed,
                "split": args.split,
                "full_history": args.full_history,
                "candidate_refresh_probe": args.candidate_refresh_probe,
                "source_sha256": source["sha256"], "windows": windows,
                "setup_is_model_generated": False, "semantic_grading": "manual_required"}
    (directory / "fixture-control.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    original = ResearchContinuation.kernel

    def seeded_kernel(history, session, request, *, invocation):
        tools = request.execution.research_tools
        delegate = ExecutionDelegate(session=session, research_tools=tools)
        receipts = []
        detail = {"task_refs": [row["task_ref"] for row in coverage_requirements(session.task_text)]}
        if args.split:
            detail["work_units"] = RELATION_TARGETS[args.case]
        calls = [ToolCall("research_plan", {"questions": [QUESTIONS[args.case]], "question_details": [detail]}), *[
            ToolCall("open_url", {"question_ref": "q1", "url": source["url"],
                                  "offset": row["offset"], "limit": row["limit"]}) for row in windows]]
        for index, call in enumerate(calls):
            result, ok, _ = delegate._execute_research(call)
            if not ok:
                raise RuntimeError("fixture setup failed: " + result.model_text)
            result = bind_source_identity(session, call, result, ok=ok)
            project_result_observation(session, call, result, ok=ok, research_ledger=tools.ledger)
            session._memory_results[f"fixture-{index}"] = result
            receipts.append({"origin": "fixture_setup", "tool": call.name, "args": call.args,
                             "ok": result.ok, "model_text": result.model_text, "canonical": dict(result.canonical)})
        (directory / "fixture-receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.candidate_refresh_probe:
            probe_receipts = []
            for index, call in enumerate(candidate_refresh_calls(next(iter(tools.ledger.read_passages)))):
                result, ok, _ = delegate._execute_research(call)
                if not ok:
                    raise RuntimeError("candidate refresh setup failed: " + result.model_text)
                project_result_observation(session, call, result, ok=ok, research_ledger=tools.ledger)
                session._memory_results[f"candidate-refresh-{index}"] = result
                probe_receipts.append({"origin": "scripted_probe_setup", "tool": call.name, "args": call.args,
                                       "ok": ok, "canonical": dict(result.canonical)})
            (directory / "candidate-refresh-setup.json").write_text(
                json.dumps(probe_receipts, ensure_ascii=False, indent=2), encoding="utf-8")
        session.model_tool_names = ("save_finding", "submit_research_answer", "knowledge_read", "read_tool_result")
        return original(history, session, request, invocation=invocation)

    with urlopen("http://127.0.0.1:5001/v1/models", timeout=5) as response:
        model = json.load(response)["data"][0]["id"]
    task = QUESTIONS[args.case] + "用中文作答。仅根据提供的已打开证据形成局部结论，不能确定的部分如实说明。"
    with ExitStack() as stack:
        stack.enter_context(patch.object(ResearchContinuation, "kernel", seeded_kernel))
        if args.full_history:
            from codey.operations import kernel_driver

            prepare = kernel_driver.prepare_provider_request

            def full_history(sink, method, call_args, options, **kwargs):
                return prepare(sink, method, call_args,
                               {key: value for key, value in options.items() if key != "tool_call_history_groups"},
                               **kwargs)

            stack.enter_context(patch.object(kernel_driver, "prepare_provider_request", full_history))
        stage(directory, "research", "http://127.0.0.1:5001", model, live_web=False,
              question=task, max_turns=args.max_turns, seed=args.seed,
              provider_timeout=args.provider_timeout, constrained_text=True)


if __name__ == "__main__":
    main()
