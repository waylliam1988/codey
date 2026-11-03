"""Paired error-display deduplication; complete drafts and original receipts stay intact."""
from __future__ import annotations

import argparse
import hashlib
import runpy
import shutil
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_lazy_revision_ab as freezing
from tests.manual import research_read_guidance_ab as runner

ARMS = ("baseline", "compact", "original_control", "production")
ERROR_RECEIPT = "Tool failed. The complete error appears in latest_error in the current evidence view.\n"


@contextmanager
def treatment(root, arm):
    from codey.operations import kernel_context, task_guidance
    from codey.providers.base import render_result_metadata
    from codey.research import answer_context
    from codey.research.answer_contract import AnswerScope

    if arm not in ARMS:
        raise ValueError(arm)
    with ExitStack() as stack:
        if arm == "compact":
            original_history = answer_context.investigation_history
            original_result = task_guidance.task_result_context

            def history(session):
                latest = next(reversed(session._memory_results), "")
                return [{key: value for key, value in row.items() if key != "error" or row["result_ref"] != latest}
                        for row in original_history(session)]

            def result_context(session, result, scope):
                if (isinstance(scope, AnswerScope) and not result.ok and len(result.model_text) <= 1600
                        and next(reversed(session._memory_results.values()), None) is result):
                    identity = next((key for key, value in session._memory_results.items() if value is result), "")
                    if identity:
                        return ERROR_RECEIPT + render_result_metadata(identity, tool=result.call.name, ok=False)
                return original_result(session, result, scope)

            stack.enter_context(patch.object(answer_context, "investigation_history", history))
            for module in (task_guidance, kernel_context):
                stack.enter_context(patch.object(module, "task_result_context", result_context))
        elif arm == "original_control":
            old_history = runpy.run_path(str(root / "baseline-answer-context.py"))["investigation_history"]
            old_result = runpy.run_path(str(root / "baseline-task-guidance.py"))["task_result_context"]
            stack.enter_context(patch.object(answer_context, "investigation_history", old_history))
            for module in (task_guidance, kernel_context):
                stack.enter_context(patch.object(module, "task_result_context", old_result))
        yield


def prepare(root, previous, production=False):
    root.mkdir(parents=True, exist_ok=False)
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("empty-knowledge", "warm-knowledge"):
        shutil.copytree(previous / name, root / name)
    if production:
        for name in ("fixtures.json", "baseline-answer-context.py", "baseline-task-guidance.py"):
            shutil.copy2(previous / name, root / name)
    else:
        old = framework.read_json(Path(".e2e-artifacts/research-branch-review-20261103-final/fixtures.json"))["cases"]
        cases = [deepcopy(next(c for c in old if c["id"] == name)) for name in ("draft_complex", "error_complex")]
        prior = framework.read_json(previous / "fixtures.json")["cases"]
        cases.append(deepcopy(next(c for c in prior if c["id"] == "history_error")))
        normal = deepcopy(next(c for c in prior if c["id"] == "accumulate"))
        cases.append(normal)
        draft = deepcopy(normal)
        draft.update(id="draft_simple", controlled_sequence=[
            {"tool": "open_url", "args": {"url": draft["documents"][0]["url"]}},
            {"draft": "SQLite 从 3.22.0 开始放宽只读 WAL 限制。满足下列任一条件即可："
                "-shm 和 -wal 已存在且可读；或目录可写以便创建这两个文件；"
                "或使用 immutable 查询参数打开连接。不能要求三项同时满足 [e1]。"}],
            controlled_reason="One actual source read, then completed prose without submission; live model repairs delivery and saves the requested new claim.")
        cases.append(draft)
        errors = deepcopy(normal)
        errors.update(id="two_errors", controlled_sequence=[
            {"tool": "knowledge_read", "args": {"id": "missing-first-exact-identity", "revision": 1}},
            {"tool": "knowledge_read", "args": {"id": "missing-second-exact-identity", "revision": 1}}],
            controlled_reason="Two settled read failures before live source acquisition and answer/knowledge publication; older error remains visible.")
        cases.append(errors)
        framework.write_json(root / "fixtures.json", {"schema_version": 1, "cases": cases})
        shutil.copy2("codey/research/answer_context.py", root / "baseline-answer-context.py")
        shutil.copy2("codey/operations/task_guidance.py", root / "baseline-task-guidance.py")
    seeds = [521, 522] if production else [501, 502]
    arms = ["original_control", "production"] if production else ["baseline", "compact"]
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 10, "process_seconds": 900, "agent_seconds": 840})
    framework.write_json(root / "preregistration.json", {
        "phase": "production" if production else "A/B", "seeds": seeds,
        "matrix": [{"case": c["id"], "arms": arms} for c in framework.read_json(root / "fixtures.json")["cases"]],
        "change": "Keep latest_error unchanged; omit its duplicate text in the latest investigation row. "
            "For the latest failed receipt only, replace the delivered error by metadata and a pointer only if its FULL text fits latest_error. "
            "Long and older failures, full drafts, original stored results and all validation remain unchanged.",
        "gate": ["Evaluate local error-recovery benefit separately from unmodified prose repair and normal paths.",
            "No loss of original-correct paired task requirements, delivery, requested candidate saving or exact citation/recovery.",
            "Target actual total input including retries must decrease; a failed cheaper run is not a benefit.",
            "Complex baseline may remain imperfect; compare individual version/condition/actor/marker facts and retain all outputs.",
            "No truncation, new state, new tool, permission/version relaxation or automatic acceptance of drafts.",
            "If integrated, verify treatment equivalence and repeat actual production versus frozen original with new seeds."],
        "limitations": "Exploratory two seeds; fixed official/synthetic documents, controlled failure/prose prefixes; all subsequent model decisions and publication are real."})


def freeze(root):
    freezing.freeze(root)
    values = framework.read_json(root / "frozen-inputs.json")
    for name in ("baseline-answer-context.py", "baseline-task-guidance.py"):
        values[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    framework.write_json(root / "frozen-inputs.json", values)
    framework.write_json(root / "production-sha256.json", {
        p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("codey").rglob("*.py"))})
    branch.verify(root)


def worker(root, directory, base_url, case, seed, arm):
    @contextmanager
    def apply(name):
        with treatment(root, name):
            yield

    with patch.object(branch, "ablation", apply):
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
        prepare(root, args.previous.resolve(), args.production)
        return
    if args.action == "freeze":
        freeze(root)
        return
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    branch.verify(root)
    spec = framework.read_json(root / "preregistration.json")
    path = root / "matrix.json"
    rows = framework.read_json(path) if path.exists() else []
    for index, item in enumerate(spec["matrix"]):
        for seed in spec["seeds"]:
            arms = item["arms"]
            shift = (index + seed) % len(arms)
            for arm in arms[shift:] + arms[:shift]:
                if (args.case and args.case != item["case"] or args.arm and args.arm != arm
                        or args.seed is not None and args.seed != seed):
                    continue
                if any((r["case"], r["seed"], r["arm"]) == (item["case"], seed, arm) for r in rows):
                    continue
                print("START", item["case"], seed, arm, flush=True)
                with patch.object(runner, "__spec__", __spec__):
                    row = runner.run_one(root, item["case"], seed, arm)
                rows.append(row)
                framework.write_json(path, rows)
                print("END", item["case"], seed, arm, row.get("result"), flush=True)
                framework.backend_ready()


if __name__ == "__main__":
    main()
