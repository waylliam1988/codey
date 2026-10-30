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


def archived_report(source: Path) -> FindingReport:
    delivered = (source / "research-report.txt").read_text(encoding="utf-8")
    for path in sorted((source / "state" / "managed_outputs").rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = payload.get("data", {}).get("result", {}).get("research_report")
        if report and report.get("text") == delivered:
            return FindingReport.from_payload(report)
    raise ValueError("archive has no matching frozen accepted compilation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--thinking", action="store_true", help="Enable local reasoning for a deployment comparison")
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    source, directory = args.source.resolve(), args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    report = archived_report(source)
    control = json.loads((source / "research-control.json").read_text(encoding="utf-8"))
    shutil.copytree(source / "knowledge", directory / "knowledge")
    store = KnowledgeStore(directory / "knowledge")
    durable = None
    provider = None

    def write(name, value):
        (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    try:
        session_id, run_id = "archive-assessment", "assessment-" + uuid4().hex
        tools = ResearchTools(search=object(), store=store, changes=KnowledgeChanges(store.root), session_id=session_id)
        tools.ledger.questions = report.questions
        for label, metadata in report.reference_metadata.items():
            version = store.repository.read(metadata["id"], metadata["revision"])
            tools.ledger.references[label] = ResearchReference(metadata["id"], metadata["revision"],
                metadata["kind"], metadata["acquisition"], note_id=version.value.provenance.get("note_id", ""))
        result = SimpleNamespace(accepted_report=report, question=control["question"], summary=report.text,
                                 completion_proof=None)
        basis = review_basis(report, tools, result.question)
        write("basis.json", basis)
        write("control.json", {"source": str(source), "upstream": args.upstream, "timeout": args.timeout,
            "mode": "imported_frozen_report", "prepare_only": args.prepare_only,
            "run_id": run_id, "session_id": session_id, "basis_digest": basis["basis_digest"]})
        if args.prepare_only:
            return

        class Observed(ApiProvider):
            def _generate(self, messages, tools=None, **kwargs):
                with (directory / "wire.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(canonical_json({"event": "request", "messages": messages,
                        "tools": [tool.name for tool in tools or ()]}) + "\n")
                response = super()._generate(messages, tools, **kwargs)
                with (directory / "wire.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(canonical_json({"event": "response", "body": response}) + "\n")
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
