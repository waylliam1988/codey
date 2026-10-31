"""Assess an archived exact research report through the production review kernel.

Copies the old knowledge store into a fresh diagnostic directory. The import
receipt is synthetic and explicitly marked; ALL assessment requests use the
real local provider, ordinary effect admission and durable original receipts.
No acquisition is permitted. --prepare-only builds a review basis offline.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from urllib.request import urlopen
from uuid import uuid4

from codey.durable import DurableSession, SQLiteDurableStore
from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.model import canonical_json
from codey.knowledge.store import KnowledgeStore
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.provider_receipts import runtime_implementation_id
from codey.operations.research_continuation import ResearchContinuation, ResearchInvocation
from codey.operations.research_review import assess_report
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers import local_connection
from codey.providers.api_provider import ApiProvider
from codey.providers.local_config import LocalProviderConfig
from codey.research.finding_report import FindingReport
from codey.research.reference import ResearchReference
from codey.research.semantic_review import review_basis
from codey.research.tools import ResearchTools
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.manual.kobold_diagnostics import record_native_generation


def archived_report(source: Path) -> tuple[FindingReport, str]:
    delivered = (source / "research-report.txt").read_text(encoding="utf-8")
    for path in sorted((source / "state" / "managed_outputs").rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = payload.get("data", {}).get("result", {}).get("research_report")
        if report and report.get("text") == delivered:
            return FindingReport.from_payload(report), path.parent.name
    raise ValueError("archive has no matching frozen accepted compilation")


def archived_investigations(source: Path, run_id: str, snapshot: Path):
    """Replay original settled observations from a read-only archive snapshot."""
    from codey.operations.kernel_facts import project_result_observation
    from codey.operations.kernel_receipts import restore_result_receipt
    from codey.research.ledger import ResearchLedger
    from codey.research.questions import INVESTIGATION_TOOLS
    from codey.runtime.effects.effect_records import EFFECT_CATEGORY_TOOL_CALL, RuntimeEffectStore

    database = source / "state" / "durable.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original, closing(
        sqlite3.connect(snapshot)
    ) as copy:
        original.backup(copy)
    durable = DurableSession(SQLiteDurableStore(snapshot))
    ledger = ResearchLedger()
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read"})), task_kind="research")
    try:
        session_id = durable.task(run_id).conversation_id
        outputs = ManagedOutputStore(source / "state")
        for effect in RuntimeEffectStore(durable=durable).load_effects(session_id, run_id):
            if (effect.intent.effect_category != EFFECT_CATEGORY_TOOL_CALL or effect.settlement is None
                    or effect.intent.tool_name not in INVESTIGATION_TOOLS):
                continue
            result = restore_result_receipt(effect, store=outputs, session_id=session_id, run_id=run_id)
            project_result_observation(session, result.call, result, ok=result.ok,
                                       exit_code=effect.settlement.exit_code, research_ledger=ledger)
        return ledger.investigations
    finally:
        durable.close()


def calibration_report(store, tools, case):
    from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference
    from codey.research.finding_report import compile_finding_report
    from codey.research.references import bind_version

    url = "https://example.org/synthetic-review-calibration"
    objects = (
        KnowledgeObject("calibration-source", "source", case["excerpt"], "source", title="Synthetic calibration",
                        provenance={"url": url}),
        KnowledgeObject("calibration-evidence", "evidence", case["excerpt"], "source",
                        references=(KnowledgeReference("calibration-source", 1, "from_source"),)),
        KnowledgeObject("calibration-claim", "claim", case["finding"], "model_extraction", status="candidate",
                        references=(KnowledgeReference("calibration-evidence", 1, "supported_by"),)),
    )
    store.commit_knowledge("synthetic-calibration-input", [KnowledgeMutation(value) for value in objects])
    tools.ledger.questions = (case["question"],)
    ref = bind_version(tools.ledger, store.repository.read("calibration-claim"), question_ref="q1")
    paragraph = case["paragraph"] + " [" + ref + "]"
    args = {"summary": paragraph, "report_sections": {"evidence": [paragraph],
        "counter": [case["finding"] + " [" + ref + "]"],
        "coverage": [{"question_ref": "q1", "state": "evidence_found", "answer": paragraph,
                      "findings": [ref], "investigations": []}]}}
    return compile_finding_report(args, tools.ledger, store, session_id=tools.session_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--source", type=Path)
    input_group.add_argument("--calibration-case", type=Path,
                             help="Synthetic controlled excerpt/finding/report JSON; never a live research result")
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--thinking", action="store_true", help="Enable local reasoning for a deployment comparison")
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    source, directory = args.source.resolve() if args.source else None, args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    case = json.loads(args.calibration_case.read_text(encoding="utf-8")) if args.calibration_case else None
    if source:
        shutil.copytree(source / "knowledge", directory / "knowledge")
    store = KnowledgeStore(directory / "knowledge")
    durable = None
    provider = None

    def write(name, value):
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    try:
        session_id, run_id = "archive-assessment", "assessment-" + uuid4().hex
        tools = ResearchTools(search=object(), store=store, changes=KnowledgeChanges(store.root), session_id=session_id)
        if source:
            report, source_run_id = archived_report(source)
            control = json.loads((source / "research-control.json").read_text(encoding="utf-8"))
            # This explicit diagnostic import is synthetic, never a new live result.
            tools.ledger.investigations = archived_investigations(source, source_run_id, directory / "source-state.sqlite3")
        else:
            report = calibration_report(store, tools, case)
            control = {"question": case["question"]}
        tools.ledger.questions = report.questions
        for label, metadata in report.reference_metadata.items():
            version = store.repository.read(metadata["id"], metadata["revision"])
            tools.ledger.references[label] = ResearchReference(metadata["id"], metadata["revision"],
                metadata["kind"], metadata["acquisition"], note_id=version.value.provenance.get("note_id", ""),
                question_ref=metadata["question_ref"])
        result = SimpleNamespace(accepted_report=report, question=control["question"], summary=report.text,
                                 completion_proof=None)
        basis = review_basis(report, tools, result.question)
        write("basis.json", basis)
        write("control.json", {"source": str(source), "upstream": args.upstream, "timeout": args.timeout,
            "mode": "synthetic_calibration" if case else "imported_frozen_report", "prepare_only": args.prepare_only,
            "run_id": run_id, "session_id": session_id, "basis_digest": basis["basis_digest"],
            "runtime_implementation_id": runtime_implementation_id()})
        if args.prepare_only:
            return

        class Observed(ApiProvider):
            def record(self, row):
                with (directory / "wire.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(canonical_json(row) + "\n")

            def _generate(self, messages, tools=None, **kwargs):
                self.record({"event": "request", "messages": messages, "tools": [tool.name for tool in tools or ()]})
                response = super()._generate(messages, tools, **kwargs)
                self.record({"event": "response", "body": response})
                record_native_generation(args.upstream, self.record)
                return response

        from unittest.mock import patch

        with urlopen(args.upstream + "/v1/models", timeout=5) as response:
            model = json.load(response)["data"][0]["id"]
        with patch.object(local_connection, "ApiProvider", Observed):
            provider = local_connection.connect_local(config=LocalProviderConfig(
                base_url=args.upstream + "/v1", model=model, native_tools_mode="on", thinking_enabled=args.thinking))
        provider.timeout = args.timeout
        write("deployment.json", {"model": model, "context_budget": vars(provider.context_budget),
                                  "thinking": args.thinking, "native": True})
        durable = DurableSession(SQLiteDurableStore(directory / "state.sqlite"))
        durable.admit_input(conversation_id=session_id, request_id="archive-import", task_id=run_id,
                            text=result.question, provider_snapshot={"provider_id": "local"})
        durable.claim_task(run_id)
        durable.start_task(run_id, state=dict(durable.task(run_id).state))
        mutations = RuntimeMutationLine(durable=durable)
        mutations.accept_operation(session_id=session_id, run_id=run_id, provider_id="local", turn_budget=8,
                                    max_repair_rounds=0, task_kind="research")
        outputs = ManagedOutputStore(directory)
        history = ResearchContinuation(mutations, outputs, session_id=session_id, run_id=run_id)
        sink = KernelEffectSink(mutations, session_id=session_id, run_id=run_id, provider_id="local",
                                managed_outputs=outputs, durable=durable)

        class ImportProvider:
            native_tools = False

            def send(self, prompt):
                return canonical_json({"tool": "done", "args": {"summary": "Imported archived report; no new assessment yet."}})

        imported = history.kernel(TaskSession(TaskPolicy(frozenset({"control"})), task_kind="research", max_turns=1),
            invocation=ResearchInvocation(1, "research"), request=KernelRunRequest(
                transport=KernelTransportDeps(provider=ImportProvider(), provider_id="local", run_id=run_id,
                    user_task="Offline archive import, not model work. Frozen report:\n" + canonical_json(report.to_payload())),
                execution=KernelExecutionDeps(managed_outputs=outputs, durable=durable, session_id=session_id),
                observation=KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
                    durable=durable, session_id=session_id, task_id=run_id, sink=sink))))
        result.completion_proof = imported.proof
        assessment = assess_report(history, result, tools, provider=provider, provider_id="local", cycle=0,
            on_event=lambda event: None, stop_flag=Event(),
            parent_policy=TaskPolicy(frozenset({"control", "knowledge.write"})))
        write("assessment.json", assessment)
        print(canonical_json({"status": assessment["status"], "issues": assessment["issues"]}))
    except Exception as exc:
        write("failure.json", {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        if provider is not None:
            provider.close()
        if durable is not None:
            durable.close()
        store.close()


if __name__ == "__main__":
    main()
