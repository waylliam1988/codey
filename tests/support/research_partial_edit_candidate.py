"""Offline candidate: retain expected_revision/creation, copy only changed fields."""

import argparse
import hashlib
import json
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def normalize_edits(session, store, args):
    rows = []
    for change in args["changes"]:
        if set(change) != {"value", "expected_revision"} or not isinstance(change["value"], dict):
            raise ValueError("Each change needs value and expected_revision.")
        fields = change["value"]
        identity = fields.get("id")
        expected = change["expected_revision"]
        if not isinstance(identity, str) or not identity or type(expected) is not int or expected < 0:
            raise ValueError("An exact id and integer expected_revision are required.")
        if expected == 0:
            rows.append(change)  # Original full-value creation; never infer creation from a misspelled edit.
            continue
        read = False
        for result in session._memory_results.values():
            if not result.ok or result.call.name != "knowledge_read":
                continue
            try:
                payload = json.loads(result.model_text)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and (payload.get("id"), payload.get("revision")) == (identity, expected):
                read = True
                break
        if not read:
            raise ValueError("Read the exact object first. expected_revision is the EXISTING revision you read, "
                             "not the new version. No changes were saved.")
        original = store.repository.read(identity, expected)
        if original is None:
            raise ValueError("The exact read version is unavailable; no changes saved.")
        merged = {**original.value.to_dict(), **fields}
        if merged == original.value.to_dict():
            raise ValueError("No changed fields. No changes saved. If already committed, continue to the answer.")
        rows.append({"value": merged, "expected_revision": expected})
    return {**args, "changes": rows}


def edit_spec(spec):
    from codey.toolchain.tool_spec import freeze_spec_parameters, thaw_schema_value

    params = thaw_schema_value(dict(spec.parameters))
    item = params["changes"]["items"]
    item["properties"]["value"]["required"] = ["id"]
    item["properties"]["expected_revision"]["description"] = (
        "EXISTING revision returned by knowledge_read, NOT the version to be created. Use 0 only for a new object.")
    description = (
        "Create or edit stored knowledge atomically. For an EDIT, first knowledge_read the exact id; "
        "send expected_revision equal to the EXISTING read revision, and value with id and only changed fields. "
        "The program preserves omitted fields from that exact version; a later conflicting edit is rejected. "
        "For a NEW object keep expected_revision=0 and a full value with id, kind, content and origin. "
        "Creation never follows automatically from a failed edit. Use exact database references, not eN. "
        "Only claim/inference/question/entity/observation are allowed. A successful commit is already saved; "
        "continue to the answer, without another commit or replacement knowledge_updates. "
        "Reuse commit_id only for an identical retry."
    )
    return freeze_spec_parameters(replace(spec, parameters=tuple(params.items()), description=description,
        json_examples=(json.dumps({"tool": "knowledge_commit", "args": {"commit_id": "edit-title",
            "changes": [{"expected_revision": 1, "value": {"id": "exact-read-id", "title": "New title"}}]}}),)))


def saved_changes(session):
    rows = []
    for result in session._memory_results.values():
        if result.ok and result.call.name == "knowledge_commit":
            rows.append({"receipt": json.loads(result.model_text), "changes": result.call.args["changes"]})
    return rows


def evidence_last(text):
    """Frozen-fixture presentation probe; production would reorder its own blocks."""
    prior = text.find("\n\nPreviously read knowledge (quoted prior statements,")
    task = text.find("\n\nComplete user task (verbatim):\n")
    if prior < 0 or task < prior:
        return text
    return text[prior + 2:task] + "\n\n" + text[:prior] + text[task:]


@contextmanager
def treatment(arm):
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research import answer_context, answer_contract
    from tests.manual.research_state_fidelity_ab import pending_references

    with ExitStack() as stack:
        if arm == "reference_focus":
            original_view = answer_context._render_context

            def focused_view(*args):
                return evidence_last(original_view(*args))

            stack.enter_context(patch.object(answer_context, "_render_context", focused_view))
        elif arm == "partial_edit":
            original_spec = answer_contract.answer_spec
            original_commit = ExecutionDelegate._commit_research_knowledge
            original_view = answer_context._render_context

            def spec(value, scope):
                result = original_spec(value, scope)
                return edit_spec(result) if result.name == "knowledge_commit" else result

            def commit(self, call, tools):
                from codey.research.answer_contract import AnswerScope

                if not isinstance(self._research_scope, AnswerScope):
                    return original_commit(self, call, tools)
                args = normalize_edits(self.session, tools.store, call.args)
                result, ok, code = original_commit(self, replace(call, args=args), tools)
                return replace(result, call=call), ok, code

            def view(session, ledger, scope, visible):
                from codey.knowledge.model import canonical_json

                result = original_view(session, ledger, scope, visible)
                result += ("\n\nUnread pinned links from prior work (navigation, not evidence):\n"
                           + canonical_json(pending_references(session))
                           + "\nRead needed original evidence for eN labels; old conclusions are not evidence.")
                saved = saved_changes(session)
                if saved:
                    result += ("\n\nChanges already saved (not evidence; do not repeat):\n" + canonical_json(saved)
                               + "\nContinue the investigation or submit the final answer. "
                                 "knowledge_updates creates new candidates; it does not edit an old object.")
                return result

            stack.enter_context(patch.object(answer_contract, "answer_spec", spec))
            stack.enter_context(patch.object(ExecutionDelegate, "_commit_research_knowledge", commit))
            stack.enter_context(patch.object(answer_context, "_render_context", view))
        elif arm not in {"baseline", "production"}:
            raise ValueError(arm)
        yield


def main():
    from tests.manual import research_branch_review as branch
    from tests.manual import research_framework_comparison as framework
    from tests.manual import research_guidance_simplification_ab as shared
    from tests.manual import research_state_fidelity_ab as first

    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        previous = Path(".e2e-artifacts/research-state-fidelity-ab-20261104")
        root.mkdir(exist_ok=False)
        for name in ("fixtures.json", "clock-context.txt", "material-channel.json", "seed-identities.json", "limits.json"):
            shutil.copy2(previous / name, root / name)
        for folder in ("warm-knowledge", "empty-knowledge", "revision-knowledge", "conflict-knowledge", "inference-knowledge"):
            shutil.copytree(previous / folder, root / folder)
        framework.write_json(root / "preregistration.json", {
            "phase": "follow-up A/B after specific full-copy failures", "seeds": [723, 724],
            "matrix": [{"case": case, "arms": ["baseline", "partial_edit"]}
                       for case in ("revise", "revise_content", "create_entity")]
                      + [{"case": "inference", "arms": ["baseline", "reference_focus"]}],
            "treatment": "Retain explicit expected_revision and original zero/full-value creation. Existing edits "
                         "preserve omitted fields from an actually read exact version. Show pending original reads "
                         "and actual committed effects. No new tool, unfold phase, implicit creation or latest-head refresh. "
                         "Independent inference candidate only moves prior work/navigation before original evidence; "
                         "it adds/removes no source or prior content and adds no tool or graph traversal.",
            "gate": "Exact target edited once, no extra replacement, correct final body and original evidence; "
                    "normal explicit creation must not regress. Count all requests and failures.",
        })
        return
    original_verify = branch.verify

    def verify(directory):
        original_verify(directory)
        snapshot = directory / "partial-edit-candidate.py"
        if snapshot.read_bytes() != Path(__file__).read_bytes():
            raise RuntimeError("The frozen partial-edit candidate changed.")

    if args.action == "freeze":
        shutil.copy2(__file__, root / "partial-edit-candidate.py")
        first.freeze(root)
        frozen = framework.read_json(root / "frozen-inputs.json")
        frozen["partial-edit-candidate.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        framework.write_json(root / "frozen-inputs.json", frozen)
        verify(root)
        print("FROZEN", root, flush=True)
    elif args.action == "worker":
        with patch.object(branch, "verify", verify), patch.object(branch, "ablation", treatment):
            branch.worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
    else:
        spec = framework.read_json(root / "preregistration.json")
        rows = framework.read_json(root / "matrix.json") if (root / "matrix.json").exists() else []
        with patch.object(branch, "verify", verify), patch.object(shared, "__spec__", SimpleNamespace(name=__spec__.name)):
            for index, item in enumerate(spec["matrix"]):
                if args.case and args.case != item["case"]:
                    continue
                for seed in spec["seeds"]:
                    if args.seed is not None and args.seed != seed:
                        continue
                    arms = item["arms"]
                    shift = (seed + index) % len(arms)
                    for arm in arms[shift:] + arms[:shift]:
                        if args.arm and args.arm != arm:
                            continue
                        if any((r["case"], r["seed"], r["arm"]) == (item["case"], seed, arm) for r in rows):
                            continue
                        print("START", item["case"], seed, arm, flush=True)
                        row = shared.run_one(root, item["case"], seed, arm)
                        rows.append(row)
                        framework.write_json(root / "matrix.json", rows)
                        print("END", item["case"], seed, arm, row.get("result"), flush=True)
                        framework.backend_ready()


if __name__ == "__main__":
    main()
