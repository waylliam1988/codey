"""Frozen live comparison: Research reads history and saves new findings only."""
from __future__ import annotations

import argparse
import shutil
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_lazy_revision_ab as freezing
from tests.manual import research_read_guidance_ab as runner
from tests.support import legacy_research_revision_tools as legacy

READ_ONLY_GUIDANCE = (
    "Stored knowledge is read-only in Research. Investigate and report conflicts using original evidence; "
    "save new findings through knowledge_updates. If asked to change an existing object, explicitly state "
    "that Research cannot perform that change; creating a new object does not update the old one. "
)
RETIRED = {legacy.PREPARE, "knowledge_commit"}
ARMS = ("baseline", "no_revision", "deferred_control", "production")


def read_only_guidance(text):
    return text.replace(legacy.REVISION_GUIDANCE, READ_ONLY_GUIDANCE)


@contextmanager
def treatment(arm):
    from codey.operations import kernel_protocol
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research import answer_context, answer_contract
    from codey.toolchain import tool_spec

    if arm not in ARMS:
        raise ValueError(arm)
    with ExitStack() as stack:
        if arm == "no_revision":
            original_allowed = kernel_protocol.controller_allowed_for_session

            def allowed(session):
                names = original_allowed(session)
                return tuple(n for n in names if n not in RETIRED) if names is not None else names

            stack.enter_context(patch.object(kernel_protocol, "controller_allowed_for_session", allowed))
            for module in (answer_context, answer_contract):
                stack.enter_context(patch.object(module, "ANSWER_GUIDANCE", read_only_guidance(module.ANSWER_GUIDANCE)))
        elif arm == "deferred_control":
            original_specs = tool_spec._all_specs
            original_allowed = kernel_protocol.controller_allowed_for_session
            original_context = answer_context.answer_context
            original_execute = ExecutionDelegate._execute_research
            original_answer_spec = answer_contract.answer_spec

            def specs():
                return {**original_specs(), legacy.PREPARE: legacy.preparation_spec()}

            def allowed(session):
                if not answer_contract.whole_research(session):
                    return original_allowed(session)
                denied = {str(n or "").strip().lower() for n in session.controller_denied or ()}
                names = tuple(n for n in (*answer_contract.ANSWER_TOOLS, "knowledge_commit") if n not in denied and (
                    n not in {"open_result", "reopen_source", "open_hit"} or getattr(session,
                    {"open_result": "search_results", "reopen_source": "source_ids", "open_hit": "hit_targets"}[n], None)))
                return legacy.revision_tool_names(session, names)

            def context(session, *args, **kwargs):
                return legacy.revision_guidance(session, original_context(session, *args, **kwargs))

            def execute(self, call, **kwargs):
                if call.name == legacy.PREPARE:
                    result = legacy.preparation_result(self.session, self._research_scope, call)
                    return result, result.ok, None
                return original_execute(self, call, **kwargs)

            def commit(self, call, tools):
                from codey.runtime.core.models import ToolResult

                if any(row.get("value", {}).get("kind") not in {
                    "claim", "inference", "question", "entity", "observation"} for row in call.args.get("changes", [])):
                    raise ValueError("whole research knowledge_commit only accumulates explicit authored objects")
                outcome = tools.knowledge_commit(call.args)
                return ToolResult(call, outcome.model_text, ok=outcome.ok), outcome.ok, None

            def answer_spec(spec, scope):
                if spec.name != "knowledge_commit":
                    return original_answer_spec(spec, scope)
                from dataclasses import replace

                from codey.toolchain.tool_spec import freeze_spec_parameters, thaw_schema_value

                spec = original_answer_spec(spec, scope)
                parameters = thaw_schema_value(dict(spec.parameters))
                parameters["changes"]["items"]["properties"]["value"]["properties"]["kind"]["enum"] = [
                    "claim", "inference", "question", "entity", "observation"]
                return freeze_spec_parameters(replace(spec, parameters=tuple(parameters.items()), description=spec.description +
                    " For this whole research task, only explicitly authored claims, inferences, questions, "
                    "entities and observations are allowed. Source/evidence/answer publication belongs to "
                    "the original answer transaction. Optional accumulation never certifies interpretation."))

            stack.enter_context(patch.object(tool_spec, "_all_specs", specs))
            stack.enter_context(patch.object(kernel_protocol, "controller_allowed_for_session", allowed))
            stack.enter_context(patch.object(answer_context, "answer_context", context))
            stack.enter_context(patch.object(answer_contract, "answer_spec", answer_spec))
            stack.enter_context(patch.object(ExecutionDelegate, "_execute_research", execute))
            stack.enter_context(patch.object(ExecutionDelegate, "_commit_research_knowledge", commit))
            for module in (answer_context, answer_contract):
                stack.enter_context(patch.object(module, "ANSWER_GUIDANCE", module.ANSWER_GUIDANCE.replace(
                    READ_ONLY_GUIDANCE, legacy.REVISION_GUIDANCE)))
        yield


def prepare(root, previous, *, production=False):
    root.mkdir(parents=True, exist_ok=False)
    for name in ("seed-identities.json", "clock-context.txt", "material-channel.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("warm-knowledge", "empty-knowledge", "conflict-knowledge", "revision-knowledge"):
        if any(p.stat().st_size for p in (previous / name).rglob("*-wal")):
            raise ValueError("Seed database has an active WAL")
        shutil.copytree(previous / name, root / name, ignore=shutil.ignore_patterns("*-wal", "*-shm"))
    old = {c["id"]: c for c in framework.read_json(previous / "fixtures.json")["cases"]}
    cases = [deepcopy(old[c]) for c in ("history", "history_error", "conflict", "accumulate", "revise")]
    web = deepcopy(old["conflict"])
    web.update(id="web_conflict", question=(
        "请先读取知识库的‘冲突核查’候选主张，再搜索并打开 SQLite 官方 WAL 文档核查。"
        "用中文准确说明旧主张写了什么、错在哪里、原文支持的版本及完整 OR 替代条件。"
        "必须使用本轮网页读取的原文作引用，不用旧答案代替证据；不修改旧知识。"))
    cases.insert(3, web)
    cases.insert(5, {"id": "asyncio", "warm": False, "question":
        "阅读 Python 官方材料，用中文说明 TaskGroup 从哪个版本引入；一个子任务发生非 CancelledError 异常时，"
        "其他任务怎样处理、什么时候抛出组合异常。明确该规则是否把单独的 CancelledError 当成触发条件。"
        "同时保存一条‘TaskGroup 在 Python 3.11 引入’的新候选知识，引用原文。",
        "documents": [{"url": "https://docs.python.org/3.11/library/asyncio-task.html", "title": "Task Groups — Python 3.11 documentation",
        "text": "Task Groups\nAdded in version 3.11.\n\n"
        "The first time any of the tasks belonging to the group fails with an exception other than asyncio.CancelledError, "
        "the remaining tasks in the group are cancelled. No further tasks can then be added to the group.\n\n"
        "Once all tasks have finished, if any tasks have failed with an exception other than asyncio.CancelledError, "
        "those exceptions are combined in an ExceptionGroup or BaseExceptionGroup (as appropriate; see their documentation) "
        "which is then raised."}]})
    framework.write_json(root / "fixtures.json", {"schema_version": 1, "cases": cases})
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 12, "process_seconds": 900, "agent_seconds": 840})
    arms = ["deferred_control", "production"] if production else ["baseline", "no_revision"]
    framework.write_json(root / "preregistration.json", {
        "phase": "production follow-up" if production else "isolated removal AB",
        "baseline_commit": "622ce76a", "seeds": [211, 212] if production else [201, 202],
        "matrix": [{"case": c["id"], "arms": arms} for c in cases],
        "scope": "Remove old-object editing from Research; no separate maintenance workflow is introduced.",
        "gates": ["All normal-research baseline full-body-and-storage passing pairs must also pass in candidate",
                  "Normal-research total full passes and deliveries must not decrease",
                  "New candidate claims saved when requested; exact original citations and original objects protected",
                  "Common tool schemas and initial inputs differ only by removal of revision tools and their guidance",
                  "Explicit revision is an intentionally removed capability, reported separately; no false success claims",
                  "Token/request cost is measured including errors and retries, not a substitute for body quality"],
        "quality": {"wal": "3.22.0; OR; both -shm/-wal exist and readable OR directory writable to create both OR immutable; no unsupported necessity",
                    "conflict": "Accurate old-claim quotation and evidence-grounded correction, not just correct fresh answer",
                    "asyncio": "3.11, non-CancelledError trigger, cancel remaining, wait all tasks, grouped exception; candidate fact stored",
                    "revise": "Scope removal reported; must not claim old object modified or equate new object with an update"},
        "limitations": "Two seeds per phase, two frozen official excerpts; web tools use fixed responses, not the changing public web. Synthetic error prefix explicitly recorded. Non-blind independent body inspection; no universal no-regression proof."})


def worker(root, directory, base_url, case, seed, arm):
    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root, args.previous.resolve(), production=args.production)
        return
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    if args.action == "freeze":
        freezing.freeze(root)
        return
    branch.verify(root)
    spec = framework.read_json(root / "preregistration.json")
    rows = framework.read_json(root / "matrix.json") if (root / "matrix.json").exists() else []
    for index, item in enumerate(spec["matrix"]):
        for seed in spec["seeds"]:
            arms = item["arms"]
            shift = (index + seed) % len(arms)
            for arm in arms[shift:] + arms[:shift]:
                if any((r["case"], r["seed"], r["arm"]) == (item["case"], seed, arm) for r in rows):
                    continue
                print("START", item["case"], seed, arm, flush=True)
                with patch.object(runner, "__spec__", __spec__):
                    row = runner.run_one(root, item["case"], seed, arm)
                rows.append(row)
                framework.write_json(root / "matrix.json", rows)
                print("END", item["case"], seed, arm, row.get("result"), flush=True)
                framework.backend_ready()


if __name__ == "__main__":
    main()
