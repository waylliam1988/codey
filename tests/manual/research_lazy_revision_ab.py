"""Frozen live trial: defer the revision schema behind a non-writing preparation call."""

from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_read_guidance_ab as runner
from tests.manual.research_experiment_identity import freeze_experiment
from tests.support.legacy_research_revision_tools import REVISION_GUIDANCE

PREPARE = "prepare_knowledge_revision"
PREPARE_GUIDANCE = (
    "For authorized changes to stored objects, first call prepare_knowledge_revision to reveal knowledge_commit; "
    "preparation writes nothing. Current eN labels belong in the submission batch, not knowledge_commit. "
)
PREPARE_DESCRIPTION = (
    "Make the full knowledge_commit tool available next turn for authorized stored-knowledge changes. "
    "This only prepares the tool; it does not create, edit or verify knowledge. "
    "Ordinary lookup needs no preparation; new answer knowledge can use submit_research_answer.knowledge_updates."
)
ARMS = ("baseline", "lazy", "full_control", "production")


def revision_ready(session):
    return any(result.ok and result.call.name == PREPARE
               and result.canonical.get("knowledge_revision_ready") is True
               for result in getattr(session, "_memory_results", {}).values())


def revision_tool_names(session, names):
    from codey.research.answer_contract import whole_research

    if names is not None:
        names = tuple(name for name in names if name != PREPARE)
    if (not whole_research(session) or names is None or "knowledge_commit" not in names
            or getattr(session, "model_tool_names", None) is not None or revision_ready(session)):
        return names
    denied = getattr(session, "controller_denied", ()) or ()
    # An explicit ban on discovery keeps the full original tool available.
    if PREPARE in denied:
        return names
    return tuple(PREPARE if name == "knowledge_commit" else name for name in names)


def revision_guidance(session, guidance):
    from codey.research.answer_contract import whole_research

    if (whole_research(session) and getattr(session, "model_tool_names", None) is None
            and not revision_ready(session) and session.policy.allows("knowledge.write")
            and not {PREPARE, "knowledge_commit"}.intersection(getattr(session, "controller_denied", ()) or ())):
        return guidance.replace(REVISION_GUIDANCE, PREPARE_GUIDANCE)
    return guidance


def preparation_result(session, scope, call):
    from codey.research.answer_contract import AnswerScope, digest, whole_research
    from codey.runtime.core.models import ToolResult

    if (not whole_research(session) or not isinstance(scope, AnswerScope)
            or scope.task_digest != digest(session.task_text) or call.args
            or not session.policy.allows("knowledge.write")
            or "knowledge_commit" in (session.controller_denied or ())):
        return ToolResult(call, "ERROR: knowledge revision preparation is not authorized for this task.", ok=False)
    return ToolResult(call, "Revision tool prepared; knowledge_commit is available next turn. "
                      "No knowledge was created or changed. Read the target before revising it; "
                      "keep exact ids, expected revisions, scope and references.", ok=True,
                      canonical={"knowledge_revision_ready": True})


def preparation_spec():
    from codey.toolchain.tool_spec import ToolSpec

    return ToolSpec(name=PREPARE, grant="knowledge.write", executor="knowledge", replay_class="safe",
                    description=PREPARE_DESCRIPTION,
                    json_examples=('{"tool":"prepare_knowledge_revision","args":{}}',))


@contextmanager
def treatment(arm):
    if arm not in ARMS:
        raise ValueError(arm)
    from codey.operations import kernel_protocol
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research import answer_context
    from codey.toolchain import tool_spec

    production = "prepare_knowledge_revision" in tool_spec.tool_specs()
    if arm in {"baseline", "lazy"} and production or arm in {"production", "full_control"} and not production:
        raise ValueError("Wrong source phase for the declared tool treatment")
    with ExitStack() as stack:
        if arm == "lazy":
            original_specs = tool_spec.tool_specs
            original_allowed = kernel_protocol.controller_allowed_for_session
            original_context = answer_context.answer_context
            original_execute = ExecutionDelegate._execute_research

            def specs():
                return {**original_specs(), PREPARE: preparation_spec()}

            def allowed(session):
                return revision_tool_names(session, original_allowed(session))

            def context(session, *args, **kwargs):
                return revision_guidance(session, original_context(session, *args, **kwargs))

            def execute(self, call, **kwargs):
                if call.name == PREPARE:
                    result = preparation_result(self.session, self._research_scope, call)
                    return result, result.ok, None
                return original_execute(self, call, **kwargs)

            stack.enter_context(patch.object(tool_spec, "tool_specs", specs))
            stack.enter_context(patch.object(kernel_protocol, "controller_allowed_for_session", allowed))
            stack.enter_context(patch.object(answer_context, "answer_context", context))
            stack.enter_context(patch.object(ExecutionDelegate, "_execute_research", execute))
        elif arm == "full_control":
            from codey.research import revision_tools

            stack.enter_context(patch.object(revision_tools, "revision_tool_names", lambda _s, names: names))
            stack.enter_context(patch.object(revision_tools, "revision_guidance", lambda _s, text: text))
        yield


def worker(root, directory, base_url, case, seed, arm):
    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def freeze(root):
    if (root / "experiment-source.json").exists() or (root / "frozen-inputs.json").exists():
        raise ValueError("Use a new directory; frozen inputs must never be overwritten")
    for name in ("runs", "wire", "logs", "results"):
        (root / name).mkdir(exist_ok=True)
    framework.backend_ready()
    with urlopen(framework.UPSTREAM + "/v1/models", timeout=5) as response:
        framework.write_json(root / "backend-model.json", json.load(response))
    names = ["preregistration.json", "backend-model.json", "seed-identities.json", "clock-context.txt",
             "limits.json", "fixtures.json", "material-channel.json"]
    for folder in sorted(root.glob("*-knowledge")):
        names.extend(p.relative_to(root).as_posix() for p in sorted(folder.rglob("*")) if p.is_file())
    framework.write_json(root / "frozen-inputs.json", {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})
    freeze_experiment(root)
    branch.verify(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    if args.action == "freeze":
        freeze(root)
        print("FROZEN", root, flush=True)
        return
    branch.verify(root)
    spec = framework.read_json(root / "preregistration.json")
    rows = framework.read_json(root / "matrix.json") if (root / "matrix.json").exists() else []
    for index, item in enumerate(spec["matrix"]):
        if args.case and args.case != item["case"]:
            continue
        for seed in spec["seeds"]:
            if args.seed is not None and args.seed != seed:
                continue
            arms = item["arms"]
            shift = (index + seed) % len(arms)
            for arm in arms[shift:] + arms[:shift]:
                if args.arm and args.arm != arm:
                    continue
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
