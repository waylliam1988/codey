"""Independent real-model revision and fitting-full-source experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_guidance_simplification_ab as shared


def revision_args(session, store, args, *, reject_noop=False):
    """Expand explicit changed fields from an actually read immutable version."""
    rows = []
    for change in args["changes"]:
        if set(change) != {"value"} or not isinstance(change["value"], dict):
            raise ValueError("Each change needs value with id and changed fields; the program supplies the read version.")
        patch_value = change["value"]
        identity = patch_value.get("id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("An exact knowledge id is required.")
        read = None
        for result in reversed(list(session._memory_results.values())):
            if not result.ok or result.call.name != "knowledge_read" or result.call.args.get("id") != identity:
                continue
            try:
                value = json.loads(result.model_text)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get("id") == identity and type(value.get("revision")) is int:
                read = store.repository.read(identity, value["revision"])
                break
        if read is None:
            # Existing identities require a read, never a silent refresh to HEAD.
            if store.repository.read(identity) is not None:
                raise ValueError("Read this exact stored object with knowledge_read before changing it.")
            complete = patch_value
            expected = 0
        else:
            complete = {**read.value.to_dict(), **patch_value}
            expected = read.revision
            if reject_noop and complete == read.value.to_dict():
                raise ValueError("No fields differ from the version you read; no changes written. "
                                 "Do not resubmit unchanged edits. Continue the answer using original evidence, "
                                 "or read again if you intend a different update.")
        rows.append({"value": complete, "expected_revision": expected})
    return {**args, "changes": rows}


def revision_spec(spec):
    from codey.research.knowledge_schema import mutation_schema
    from codey.toolchain.tool_spec import freeze_spec_parameters, thaw_schema_value

    params = thaw_schema_value(dict(spec.parameters))
    # The experiment retains its original full-value control even after the
    # current production tool has adopted flat partial edits.
    params["changes"]["items"] = mutation_schema()
    params["changes"]["items"]["properties"]["value"]["properties"]["kind"]["enum"] = [
        "claim", "inference", "question", "entity", "observation"]
    row = params["changes"]["items"]
    row["properties"].pop("expected_revision")
    row["required"] = ["value"]
    row["properties"]["value"]["required"] = ["id"]
    example = {"tool": "knowledge_commit", "args": {"commit_id": "retitle-1", "changes": [
        {"value": {"id": "exact-id-from-knowledge-read", "title": "Updated title"}}]}}
    description = (
        "Change stored knowledge: first knowledge_read the exact id, then send value containing that id and only "
        "the fields to change. The application preserves omitted fields and uses the version actually read; "
        "do not send expected_revision or revision. Existing id means update, not a replacement object. "
        "For a new object, provide id, kind, content and origin. Only claim/inference/question/entity/observation "
        "are allowed. Use exact database references, never eN labels. Reuse commit_id only for an identical retry."
    )
    return freeze_spec_parameters(replace(spec, parameters=tuple(params.items()), description=description,
                                          json_examples=(json.dumps(example),)))


def flat_revision_args(session, store, args, *, reject_noop=False):
    return revision_args(session, store, {**args, "changes": [{"value": row} for row in args["changes"]]},
                         reject_noop=reject_noop)


def flat_revision_spec(spec):
    from codey.toolchain.tool_spec import freeze_spec_parameters, thaw_schema_value

    result = revision_spec(spec)
    params = thaw_schema_value(dict(result.parameters))
    params["changes"]["items"] = params["changes"]["items"]["properties"]["value"]
    params["changes"]["minItems"] = 1
    description = (
        "Edit an existing object after knowledge_read: changes contains its exact id and only fields to change. "
        "The program preserves other fields and checks the version actually read. Example: "
        '{"commit_id":"retitle-1","changes":[{"id":"exact-read-id","title":"New title"}]}. '
        "Do not wrap each change in value. Do not supply revision or expected_revision. "
        "A successful commit is already saved; do not submit it again with a new commit_id or create a replacement "
        "through knowledge_updates. For an explicitly requested new object, include id, kind, content and origin. "
        "Only claim/inference/question/entity/observation are allowed; references use exact database ids and versions."
    )
    return freeze_spec_parameters(replace(result, parameters=tuple(params.items()), description=description,
                                          json_examples=()))


def explicit_creation_args(session, store, args):
    """A misspelled edit identity must never implicitly create a new object."""
    rows = []
    for fields in args["changes"]:
        fields = dict(fields)
        create = fields.pop("create", False)
        if create is True:
            rows.append({"value": fields, "expected_revision": 0})
        else:
            identity = fields.get("id")
            if not isinstance(identity, str) or store.repository.read(identity) is None:
                raise ValueError("Unknown edit id. Copy the exact id from knowledge_read; an edit cannot create a replacement.")
            rows.extend(flat_revision_args(session, store, {**args, "changes": [fields]}, reject_noop=True)["changes"])
    return {**args, "changes": rows}


def explicit_creation_spec(spec):
    from codey.toolchain.tool_spec import freeze_spec_parameters, thaw_schema_value

    result = flat_revision_spec(spec)
    params = thaw_schema_value(dict(result.parameters))
    params["changes"]["items"]["properties"]["create"] = {"type": "boolean", "const": True,
        "description": "Only for an explicitly requested new object; never a fallback for an edit error."}
    description = result.description.replace(
        "For an explicitly requested new object, include id, kind, content and origin.",
        "An edit id must already exist and have been read. Only for an explicitly requested NEW object, "
        "set create=true and include id, kind, content and origin. Never use create=true to work around an edit error.")
    return freeze_spec_parameters(replace(result, parameters=tuple(params.items()), description=description))


def committed_view(original, session, ledger, scope, visible, *, include_changes=False):
    from codey.knowledge.model import canonical_json

    text = original(session, ledger, scope, visible)
    text = text.split("\n\nKnowledge changes already committed (not evidence; do not repeat):\n", 1)[0]
    commits = [{**json.loads(result.model_text), **({"changes": result.call.args["changes"]} if include_changes else {})}
               for result in session._memory_results.values() if result.ok and result.call.name == "knowledge_commit"]
    if commits:
        text += "\n\nKnowledge changes already committed (not evidence; do not repeat):\n" + canonical_json(commits)
    return text


def expanded_read(delegate, call, opened, provider):
    """Offer the remaining HTML only when every current original still fits."""
    from codey.research.answer_context import _render_context
    from codey.research.answer_contract import AnswerScope, answer_scope
    from codey.research.passages import describe_document_window, render_passage_window, restore_passages
    from codey.research.source_document import SourceDocument
    from codey.research.source_rendering import render_opened_source
    from codey.utils.text_budget import estimate_tokens

    if (not opened.ok or not isinstance(delegate._research_scope, AnswerScope)
            or "limit" in call.args or call.args.get("pages") or not opened.read_passages):
        return opened
    observation = opened.read_observation
    source = observation.get("source", {})
    text = observation.get("text", "")
    if source.get("content_kind") != "html" or opened.read_passages[-1]["end"] >= len(text):
        return opened
    document = SourceDocument.html(requested_url=source["requested_url"], final_url=source["final_url"],
                                   title=source["title"], text=text, truncated=source["truncated"])
    rows = describe_document_window(document, source["text_hash"], opened.read_passages[0]["start"],
                                    len(text), coherent=True)
    session = delegate.session.clone_projection()
    ledger = delegate.research_tools.ledger.clone()
    restore_passages(ledger, observation, rows)
    from codey.runtime.core.models import ToolResult

    session._memory_results["proposed_full_read"] = ToolResult(
        call=call, model_text="", ok=True, canonical={"read_passages": rows})
    scope = answer_scope(session)
    view = _render_context(session, ledger, scope, scope.aliases)
    budget = getattr(provider, "context_budget", None)
    counter = getattr(provider, "count_text", None)
    cap = min(budget.window_tokens // 2, budget.input_limit) if budget else 16384
    try:
        count = counter(view, deadline=time.monotonic() + 30).value if callable(counter) else estimate_tokens(view)
    except (ValueError, TimeoutError, OSError):
        return opened
    if type(count) is not int or not 0 <= count <= cap:
        return opened
    body = render_opened_source(document, render_passage_window(document, rows),
                                read_window=(rows[0]["start"], rows[-1]["end"]))
    return replace(opened, model_text=body, read_passages=tuple(rows))


@contextmanager
def treatment(arm):
    from codey.operations import provider_session
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research import answer_contract

    with ExitStack() as stack:
        if arm in {"revision", "revision_flat", "revision_flat_noop", "revision_visible", "revision_explicit_create"}:
            old_spec = answer_contract.answer_spec
            old_commit = ExecutionDelegate._commit_research_knowledge

            def spec(value, scope):
                result = old_spec(value, scope)
                transform = (revision_spec if arm == "revision" else
                             explicit_creation_spec if arm == "revision_explicit_create" else flat_revision_spec)
                return transform(result) if result.name == "knowledge_commit" else result

            def commit(self, call, tools):
                from codey.research.answer_contract import AnswerScope

                if not isinstance(self._research_scope, AnswerScope):
                    return old_commit(self, call, tools)
                normalize = revision_args if arm == "revision" else flat_revision_args
                args = (explicit_creation_args(self.session, tools.store, call.args) if arm == "revision_explicit_create"
                        else normalize(self.session, tools.store, call.args,
                                       reject_noop=arm in {"revision_flat_noop", "revision_visible"}))
                from codey.runtime.core.models import ToolResult

                if any(row["value"].get("kind") not in {"claim", "inference", "question", "entity", "observation"}
                       for row in args["changes"]):
                    raise ValueError("whole research knowledge_commit only accumulates explicit claims, inferences, "
                                     "questions, entities or observations; answer publication owns source/evidence/answer")
                outcome = tools.knowledge_commit(args)
                return ToolResult(call=call, model_text=outcome.model_text, ok=outcome.ok), outcome.ok, None

            stack.enter_context(patch.object(answer_contract, "answer_spec", spec))
            stack.enter_context(patch.object(ExecutionDelegate, "_commit_research_knowledge", commit))
            if arm in {"revision_flat", "revision_flat_noop", "revision_visible", "revision_explicit_create"}:
                from codey.research import answer_context

                original_view = answer_context._render_context

                def view(*args):
                    return committed_view(original_view, *args, include_changes=arm == "revision_visible")

                stack.enter_context(patch.object(answer_context, "_render_context", view))
        if arm == "full_read":
            provider = None
            old_provider = provider_session.ObservedProvider
            old_opened = ExecutionDelegate._opened_result

            def observed(*args, **kwargs):
                nonlocal provider
                provider = old_provider(*args, **kwargs)
                return provider

            def opened(self, call, output, url, **kwargs):
                output = expanded_read(self, call, output, provider)
                return old_opened(self, call, output, url, **kwargs)

            stack.enter_context(patch.object(provider_session, "ObservedProvider", observed))
            stack.enter_context(patch.object(ExecutionDelegate, "_opened_result", opened))
        yield


def worker(root, directory, base_url, case, seed, arm):
    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def prepare(root):
    previous = Path(".e2e-artifacts/research-direct-tools-prior-knowledge-20261103")
    long_root = Path(".e2e-artifacts/research-guidance-production-20261104")
    root.mkdir(exist_ok=False)
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("warm-knowledge", "empty-knowledge", "revision-knowledge", "conflict-knowledge"):
        shutil.copytree(previous / name, root / name)
    old = {r["id"]: r for r in framework.read_json(previous / "fixtures.json")["cases"]}
    long = {r["id"]: r for r in framework.read_json(long_root / "fixtures.json")["cases"]}
    revision = {**old["revise"], "id": "revise_alias", "question": old["revise"]["question"].replace(
        "把它的标题改为‘只读 WAL 条件（核查修订）’，只改变标题", "为它追加别名‘已核查只读条件’，只改变 aliases，保留已有别名")}
    synthetic = {**long["synthetic_conditions"], "id": "long_synthetic"}
    doc = dict(synthetic["documents"][0])
    paragraphs = doc["text"].splitlines()
    filler = "This section catalogues archive labels; it adds no policy conditions or performance measurements. " * 55
    doc["text"] = ("\n".join(paragraphs[:2]) + "\n\n## Archive background\n\n" + filler
                   + "\n\n" + "\n".join(paragraphs[2:-2]) + "\n\n## Archive background continued\n\n"
                   + filler + "\n\n" + "\n".join(paragraphs[-2:]))
    synthetic["documents"] = [doc]
    cases = [old["revise"], revision, long["long"], synthetic, old["history"], old["accumulate"]]
    framework.write_json(root / "fixtures.json", {"schema_version": 1, "cases": cases})
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 12, "process_seconds": 900, "agent_seconds": 840})
    framework.write_json(root / "preregistration.json", {
        "phase": "A/B", "seeds": [703, 704],
        "matrix": [{"case": r["id"], "arms": ["baseline", "revision_flat" if r["id"].startswith("revise")
                    or r["id"] in {"history", "accumulate"} else "full_read"]} for r in cases],
        "gate": "Target correctness first; database revision and preserved metadata separately checked. No new semantic regression.",
        "limitations": "Two seeds, fixed sources; full-read candidate applies only to default HTML reads fitting the whole work-view budget.",
    })


def main():
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
        prepare(root)
    elif args.action == "freeze":
        shared.freeze(root)
        hashes = framework.read_json(root / "frozen-inputs.json")
        for folder in ("revision-knowledge", "conflict-knowledge", "baseline-code"):
            for path in (root / folder).rglob("*"):
                if path.is_file():
                    hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        framework.write_json(root / "frozen-inputs.json", hashes)
        branch.verify(root)
        print("FROZEN", root, flush=True)
    elif args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
    else:
        spec = framework.read_json(root / "preregistration.json")
        rows = framework.read_json(root / "matrix.json") if (root / "matrix.json").exists() else []
        with patch.object(shared, "__spec__", SimpleNamespace(name=__spec__.name)):
            for index, item in enumerate(spec["matrix"]):
                if args.case and args.case != item["case"]:
                    continue
                for seed in spec["seeds"]:
                    if args.seed is not None and seed != args.seed:
                        continue
                    arms = item["arms"]
                    shift = (seed + index) % len(arms)
                    for arm in arms[shift:] + arms[:shift]:
                        if args.arm and arm != args.arm:
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
