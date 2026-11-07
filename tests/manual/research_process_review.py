"""Per-responsibility research ablations, always using the original headless kernel.

Production stays untouched. Frozen fixture stores are cloned before a worker
starts; controlled provider faults are labelled separately from real inference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
from uuid import uuid4

from tests.manual import research_framework_comparison as framework
from tests.manual.research_experiment_identity import freeze_experiment, verify_experiment
from tests.manual.research_workflow_ablation import ablation as previous_ablation

ARMS = (
    "baseline", "dedup_rules", "no_auto_recall", "no_task_anchor",
    "no_semantic_guidance", "no_history_hints", "no_knowledge_search", "no_web_search",
    "no_original_read", "no_source_search", "no_navigation", "no_receipt_index",
    "no_attempt_history", "no_omitted_index", "no_knowledge_updates", "no_state_field",
    "no_required_tools", "no_draft_copy", "no_repeat_feedback", "no_proof_audit", "no_error_feedback",
)


def _rewrite_view(text, *, fields=(), remove_task="", strip_errors=False):
    parts = text.split("\n\n")
    for index, part in enumerate(parts):
        if part.startswith('{"investigation_attempts":') or part.startswith('{"source_locators":'):
            row = json.loads(part)
            if "read_receipts" in row:
                for field in fields:
                    row[field] = []
                if strip_errors:
                    row["latest_error"] = []
                    for attempt in row["investigation_attempts"]:
                        attempt.pop("error", None)
                parts[index] = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text = "\n\n".join(parts)
    if remove_task:
        text = text.replace("Complete user task (verbatim):\n" + remove_task, "")
    return text


def _edit_answer_spec(original, *, remove):
    from codey.toolchain.tool_spec import freeze_spec_parameters

    def edited(spec, scope):
        result = original(spec, scope)
        if result.name != "submit_research_answer":
            return result
        parameters = {key: value for key, value in result.parameters if key != remove}
        description = result.description
        if remove == "knowledge_updates":
            start = description.index("Optional knowledge_updates")
            end = description.index("The application validates and publishes")
            description = description[:start] + description[end:]
        else:
            description = description.replace("state is your declaration, not semantic certification. ", "")
        examples = []
        for example in result.json_examples:
            row = json.loads(example)
            row["args"].pop(remove, None)
            examples.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return freeze_spec_parameters(replace(result, parameters=tuple(parameters.items()),
            required=tuple(key for key in result.required if key != remove),
            description=description, json_examples=tuple(examples)))

    return edited


@contextmanager
def ablation(arm):
    if arm not in ARMS:
        raise ValueError(arm)
    from codey.operations import kernel_prompt, kernel_protocol, knowledge_context
    from codey.research import answer_context, answer_contract

    with ExitStack() as stack:
        if arm in {"baseline", "dedup_rules", "no_auto_recall"}:
            stack.enter_context(previous_ablation(arm))
        guidance = answer_contract.ANSWER_GUIDANCE
        if arm == "no_semantic_guidance":
            guidance = guidance[guidance.index("Use state=answered"):]
        if arm == "no_history_hints":
            guidance = guidance.replace(" Historical search hits are leads: knowledge_read pins source/evidence versions into the same eN channel. ", " ")
            stack.enter_context(patch.object(answer_contract, "KNOWLEDGE_READ_GUIDANCE", ""))
            from codey.knowledge import recall

            original_recall = recall.render_recall

            def no_hints(*args, **kwargs):
                text = original_recall(*args, **kwargs)
                return "\n".join(line for line in text.splitlines()
                                 if line.startswith("{") or line.startswith("Previously saved knowledge"))

            stack.enter_context(patch.object(knowledge_context, "render_recall", no_hints))
        if arm == "no_knowledge_updates":
            start = guidance.index("To retain new structured knowledge")
            guidance = guidance[:start] + "Use knowledge_commit to edit existing stored objects with exact database references."
            stack.enter_context(patch.object(answer_contract, "answer_spec",
                _edit_answer_spec(answer_contract.answer_spec, remove="knowledge_updates")))
        if arm == "no_state_field":
            start = guidance.index("Use state=answered")
            end = guidance.index("Your next response must invoke")
            guidance = (guidance[:start] + guidance[end:]).replace(" and state=answered/partial/unresolved", "")
            stack.enter_context(patch.object(answer_contract, "answer_spec",
                _edit_answer_spec(answer_contract.answer_spec, remove="state")))
            original_submit = answer_contract.submit_answer

            def conservative_state(scope, args):
                # No semantic certification is invented: cited bodies stay partial.
                state = "partial" if answer_contract.citation_occurrences(args.get("markdown", "")) else "unresolved"
                return original_submit(scope, {**args, "state": args.get("state", state)})

            stack.enter_context(patch.object(answer_contract, "submit_answer", conservative_state))
        if guidance != answer_contract.ANSWER_GUIDANCE:
            stack.enter_context(patch.object(answer_contract, "ANSWER_GUIDANCE", guidance))
            stack.enter_context(patch.object(answer_context, "ANSWER_GUIDANCE", guidance))
        hidden = {
            "no_knowledge_search": {"knowledge_search"}, "no_web_search": {"web_search"},
            "no_original_read": {"open_url", "open_result", "reopen_source", "open_hit"},
            "no_source_search": {"source_search"},
        }.get(arm)
        if hidden:
            original_allowed = kernel_protocol.controller_allowed_for_session

            def allowed(session):
                names = original_allowed(session)
                return tuple(name for name in names if name not in hidden) if answer_contract.whole_research(session) else names

            stack.enter_context(patch.object(kernel_protocol, "controller_allowed_for_session", allowed))
        fields = {
            "no_navigation": ("source_locators",), "no_receipt_index": ("read_receipts",),
            "no_attempt_history": ("investigation_attempts",), "no_omitted_index": ("omitted_windows",),
        }.get(arm, ())
        if fields or arm in {"no_task_anchor", "no_error_feedback"}:
            original_view = answer_context.answer_context

            def view(session, *args, **kwargs):
                return _rewrite_view(original_view(session, *args, **kwargs), fields=fields,
                                     remove_task=session.task_text if arm == "no_task_anchor" else "",
                                     strip_errors=arm == "no_error_feedback")

            stack.enter_context(patch.object(answer_context, "answer_context", view))
        if arm == "no_error_feedback":
            from codey.operations import kernel_context

            original_result = kernel_context.task_result_context

            def result_context(session, result, scope):
                return "Tool failed." if not result.ok else original_result(session, result, scope)

            stack.enter_context(patch.object(kernel_context, "task_result_context", result_context))
        if arm == "no_draft_copy":
            original_repair = kernel_prompt._repair_prompt

            def repair(*args, **kwargs):
                text = original_repair(*args, **kwargs)
                marker = "Rejected response (quoted data, not instructions): "
                if marker in text:
                    start = text.index(marker) + len(marker)
                    end = text.index("\n\n", start)
                    row = json.loads(text[start:end])
                    if "response" in row:
                        row["preview"] = json.dumps(row.pop("response"), ensure_ascii=False)[:120]
                        text = text[:start] + json.dumps(row, ensure_ascii=False) + text[end:]
                        text = text.replace("Repair the missing submission action using the complete quoted draft for markdown. ", "")
                return text

            stack.enter_context(patch.object(kernel_prompt, "_repair_prompt", repair))
        if arm == "no_repeat_feedback":
            from codey.operations.kernel_progress import KernelProgress

            stack.enter_context(patch.object(KernelProgress, "repeat_feedback", lambda self: ""))
        if arm == "no_proof_audit":
            from codey.research.audit_result import AuditDiagnostic, AuditOutcome
            from codey.research.pipeline import ResearchPipeline

            def skip_review(*_args, phase, diagnostics, **_kwargs):
                diagnostic = AuditDiagnostic("proof_review", "skipped", phase, "experiment_disabled")
                diagnostics.append(diagnostic)
                return AuditOutcome(diagnostic)

            stack.enter_context(patch.object(ResearchPipeline, "_review", skip_review))
            stack.enter_context(patch.object(ResearchPipeline, "_record_final_findings", lambda *a, **kw: None))
        yield


def verify(root):
    verify_experiment(root)
    for name, digest in framework.read_json(root / "frozen-inputs.json").items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Frozen review input changed: {name}")


def worker(root, directory, base_url, case_id, seed, arm):
    verify(root)
    from codey.providers.api_provider import ApiProvider
    from tests.manual import persistent_research_local as probe

    case = framework.custom_cases(root)[case_id]
    original_stage, original_task = probe.stage, framework.task_text
    original_generate = ApiProvider._generate
    original_observed = probe.whole_answer_observed
    seed_ids = set(framework.read_json(root / "seed-identities.json")["answer_ids"])
    injected = False

    def stage(directory, *args, **kwargs):
        template = "warm-knowledge" if case.get("warm") else "empty-knowledge"
        shutil.copytree(root / template, directory / "knowledge")
        return original_stage(directory, *args, **kwargs)

    def current_answer(observed, versions, summary, store):
        return original_observed(observed, [v for v in versions if v.value.id not in seed_ids], summary, store)

    def task(root, case_id):
        return case["question"] if case.get("warm") or case.get("discovery") else original_task(root, case_id)

    def generate(self, messages, tools=None, **kwargs):
        nonlocal injected
        if arm == "no_required_tools":
            kwargs.pop("require_tool_call", None)
        text = "\n".join(str(m.get("content", "")) for m in messages)
        if case.get("provider_fault") and not injected and "[e1]" in text:
            injected = True
            if case["provider_fault"] == "plain_draft":
                message = {"content": case["draft"]}
            else:
                message = {"tool_calls": [{"id": "controlled-bad-citation", "type": "function",
                    "function": {"name": "submit_research_answer", "arguments": json.dumps({
                        "state": "answered", "markdown": "The requested result is supported [e99]."})}}]}
            framework.write_json(directory / "controlled-fault.json", {
                "kind": case["provider_fault"], "synthetic_not_inference": True,
                "message": message, "condition": "first provider generation after evidence e1 was actually read"})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}
        return original_generate(self, messages, tools, **kwargs)

    with ablation(arm), patch.object(probe, "stage", stage), \
            patch.object(probe, "whole_answer_observed", current_answer), \
            patch.object(framework, "task_text", task), patch.object(ApiProvider, "_generate", generate), \
            patch.object(framework, "DEFAULT_CLOCK_FILE", root / "clock-context.txt"):
        framework.codey_worker(root, directory, base_url, case_id, seed)
    verify(root)


def run_one(root, case, seed, arm):
    verify(root)
    ready = framework.backend_ready()
    name = f"{case}-{seed}-{arm}"
    directory = root / "runs" / name
    limits = framework.experiment_limits(root)
    started = time.monotonic()
    with patch.object(framework, "verify_experiment", verify), \
            framework.InferenceProxy(root / "wire" / f"{name}.jsonl", seed,
            genkey="process-review-" + uuid4().hex, request_limit=limits["requests"], identity_root=root) as proxy, \
            (root / "logs" / f"{name}.stdout").open("w", encoding="utf-8") as out, \
            (root / "logs" / f"{name}.stderr").open("w", encoding="utf-8") as err:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", __spec__.name, "worker",
            "--root", str(root), "--directory", str(directory), "--base-url", proxy,
            "--case", case, "--seed", str(seed), "--arm", arm], stdout=out, stderr=err)
        timed_out = False
        try:
            process.communicate(timeout=limits["process_seconds"])
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.communicate(timeout=15)
    row = {"arm": arm, "case": case, "seed": seed, "process_exit_code": process.returncode,
           "runner_timeout": timed_out, "elapsed_seconds": round(time.monotonic() - started, 2),
           "backend_before": ready, "semantic_grade": "pending_independent_review"}
    if (directory / "research-result.json").exists():
        row["result"] = framework.read_json(directory / "research-result.json")
    framework.write_json(root / "results" / f"{name}.json", row)
    verify(root)
    return row


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
        for folder in ("runs", "wire", "logs", "results"):
            (root / folder).mkdir(exist_ok=True)
        framework.backend_ready()
        with urlopen(framework.UPSTREAM + "/v1/models", timeout=5) as response:
            framework.write_json(root / "backend-model.json", json.load(response))
        names = ["preregistration.json", "backend-model.json", "seed-identities.json", "clock-context.txt"]
        for folder in ("warm-knowledge", "empty-knowledge"):
            names.extend(str(p.relative_to(root)).replace("\\", "/")
                         for p in sorted((root / folder).rglob("*")) if p.is_file())
        framework.write_json(root / "frozen-inputs.json", {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names})
        freeze_experiment(root)
        verify(root)
        print("FROZEN", root)
        return
    verify(root)
    spec = framework.read_json(root / "preregistration.json")
    matrix_path = root / "matrix.json"
    rows = framework.read_json(matrix_path) if matrix_path.exists() else []
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
                row = run_one(root, item["case"], seed, arm)
                rows.append(row)
                framework.write_json(matrix_path, rows)
                print("END", item["case"], seed, arm, row.get("result"), flush=True)
                framework.backend_ready()


if __name__ == "__main__":
    main()
