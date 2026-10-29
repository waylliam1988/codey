"""Reference-only research continuations over the existing operation and outputs.

The writer attempt remains the invocation identity. Completed kernel decisions
and acquired material are referenced here so rebuilding the outer research
projection never sends an old request or acquires the same settled material.
This module has no execution loop, effect store, or provider adapter.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from typing import Any, cast

from codey.durable.checkpoint import operation_from_checkpoint
from codey.durable.model import thaw_json
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.providers.response_record import json_value
from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation
from codey.research.pipeline import ResearchContinuationError
from codey.research.plan_executor import PlanExecutionResult
from codey.runtime.effects.effect_records import compute_args_digest


def restore_note_proposals(tools: Any, canonical: Mapping[str, object]) -> None:
    """Rebuild note metadata and in-memory proposals from the original receipt.

    Restoring a tool result never writes the vault. Only the existing staged
    commit applies proposals after candidate selection.
    """
    from codey.knowledge.note import KnowledgeNote
    from codey.research.tools import StagedKnowledgeStore

    links = canonical.get("knowledge_links_created", 0)
    if type(links) is not int or links < 0:
        raise ResearchContinuationError("invalid knowledge link count")
    if links:
        tools.links_created += links
    payload = canonical.get("knowledge_notes")
    if payload is None:
        return
    if not isinstance(payload, dict) or set(payload) != {"created", "updated", "proposals"}:
        raise ResearchContinuationError("invalid knowledge proposal receipt")
    for key, target in (("created", tools.created_ids), ("updated", tools.updated_ids)):
        values = payload[key]
        if not isinstance(values, list) or any(type(item) is not str or not item for item in values):
            raise ResearchContinuationError("invalid knowledge note identity")
        target.extend(item for item in values if item not in target)
    proposals = payload["proposals"]
    if not isinstance(proposals, list) or (proposals and not isinstance(tools.store, StagedKnowledgeStore)):
        raise ResearchContinuationError("knowledge proposals require their original staging store")
    for raw in proposals:
        if not isinstance(raw, dict) or set(raw) != {field.name for field in fields(KnowledgeNote)}:
            raise ResearchContinuationError("invalid staged note proposal")
        note = KnowledgeNote(**raw)
        if json_value(note) != raw or note.id not in payload["created"]:
            raise ResearchContinuationError("staged proposal changed its original identity or content")
        existing = tools.store._staged_notes.get(note.id)
        if existing is not None and existing != note:
            raise ResearchContinuationError("conflicting staged note proposal")
        committed = tools.store._parent.read_note(note.id)
        if (committed is not None
                and replace(committed, updated=note.updated).to_markdown() != note.to_markdown()):
            raise ResearchContinuationError("committed note differs from its original proposal")
        tools.store.write_note(note, changes=tools.changes)
    batches = canonical.get("knowledge_batches", {})
    if not isinstance(batches, dict):
        raise ResearchContinuationError("invalid knowledge batches")
    from codey.knowledge.model import KnowledgeMutation, KnowledgeObject

    for identity, batch in batches.items():
        if (identity not in payload["created"] or not isinstance(batch, dict)
                or set(batch) != {"commit_id", "mutations"}):
            raise ResearchContinuationError("invalid knowledge batch proposal")
        tools.store._staged_knowledge[identity] = (batch["commit_id"], [
            KnowledgeMutation(KnowledgeObject.from_dict(row["value"]), row["expected_revision"])
            for row in batch["mutations"]
        ])


class ResearchContinuation:
    def __init__(self, mutations: Any, outputs: Any, *, session_id: str, run_id: str) -> None:
        if mutations is None or outputs is None:
            raise ResearchContinuationError("research continuation requires durable mutations and managed outputs")
        self.mutations, self.outputs = mutations, outputs
        self.durable = mutations.durable
        self.session_id, self.run_id = session_id, run_id

    def state(self) -> Any:
        state = operation_from_checkpoint(self.durable.task(self.run_id), self.session_id)
        if state is None or state.task_kind != "research":
            raise ResearchContinuationError("research continuation has no admitted research operation")
        return state

    def read(self, reference: str, kind: str) -> dict[str, Any]:
        try:
            artifact = self.durable.artifact(reference)
            raw = thaw_json(artifact.metadata.get("ref"))
            if (artifact.owner_task_id != self.run_id or artifact.owner_effect_id is not None
                    or artifact.metadata.get("kind") != kind or not isinstance(raw, dict)
                    or raw.get("handle") != reference or raw.get("sha256") != artifact.sha256
                    or raw.get("session_id") != self.session_id or raw.get("run_id") != self.run_id):
                raise ValueError("continuation artifact ownership mismatch")
            payload = self.outputs.read_json_artifact(raw)
            if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
                    or payload["schema_version"] != 1 or payload.get("kind") != kind):
                raise ValueError("invalid continuation artifact")
            return payload
        except Exception as exc:
            raise ResearchContinuationError(f"research continuation cannot be read: {exc}") from exc

    def index(self, state: Any) -> dict[str, Any]:
        if not state.continuation_ref:
            if state.writer_attempt > 1:
                raise ResearchContinuationError("active research followup has no original continuation")
            return {"schema_version": 1, "kind": "research_continuation", "results": {}, "materials": {}}
        payload = self.read(state.continuation_ref, "research_continuation")
        if (set(payload) != {"schema_version", "kind", "results", "materials"}
                or not isinstance(payload["results"], dict) or not isinstance(payload["materials"], dict)
                or len(payload["results"]) > 7 or len(payload["materials"]) > 3):
            raise ResearchContinuationError("invalid bounded research continuation")
        for name, maximum in (("results", 7), ("materials", 3)):
            if any(key not in {str(number) for number in range(1, maximum + 1)}
                   or not isinstance(value, str) or not value
                   for key, value in payload[name].items()):
                raise ResearchContinuationError("invalid research continuation reference")
        return payload

    def artifact(self, payload: dict[str, Any]) -> Any:
        ref = self.outputs.write_json_artifact(session_id=self.session_id, run_id=self.run_id, payload=payload)
        return self.outputs.json_artifact_record(ref, owner_task_id=self.run_id, owner_effect_id=None,
                                                 metadata={"kind": payload["kind"]})

    def commit(self, state: Any, index: dict[str, Any], *, attempt: int | None = None,
               material: Any = None) -> None:
        if state.leaf != "kernel_settled" or not state.work_ref:
            raise ResearchContinuationError("research successor requires its settled predecessor")
        key = str(state.writer_attempt)
        if index["results"].get(key, state.work_ref) != state.work_ref:
            raise ResearchContinuationError("research invocation result is immutable")
        index["results"][key] = state.work_ref
        artifact = self.artifact(index)
        try:
            self.mutations.commit_research_continuation(self.session_id, self.run_id, expected=state,
                artifact=artifact, writer_attempt=attempt, artifacts=(material,) if material is not None else ())
        except Exception as exc:
            raise ResearchContinuationError(f"research continuation was not committed: {exc}") from exc

    def kernel(self, session: Any, request: KernelRunRequest, *, attempt: int) -> KernelResult:
        """Restore a completed output or enter the sole driver for active work."""
        from codey.operations.kernel_context import KernelContext
        from codey.operations.kernel_control import result_from_payload
        from codey.operations.task_loop import run_task_kernel

        state = self.state()
        if attempt < state.writer_attempt:
            reference = self.index(state)["results"].get(str(attempt))
            if not reference:
                raise ResearchContinuationError("completed research invocation has no original result reference")
            context = KernelContext(session, request)
            context.restore_facts()
            decision = context.decision(replace(state, work_ref=reference))
            return result_from_payload(decision["result"])
        if attempt > state.writer_attempt:
            self.commit(state, self.index(state), attempt=attempt)
        else:
            self.mutations.mark_writer_running(self.session_id, self.run_id, provider_id=state.provider_id,
                                                writer_attempt=attempt)
        return run_task_kernel(session, request=request)

    def investigation_plan(self, round_index: int, plan: Any, tools: Any) -> None:
        """Bind strategy data to the existing continuation before any request.

        This is an immutable input reference, not another execution position.
        Changed plans/baselines cannot silently replace an admitted invocation.
        """
        state = self.state()
        index = self.index(state)
        payload = {"schema_version": 1, "kind": "research_investigation_plan", "plan": json_value(plan),
                   "baseline": compute_args_digest(json_value({"searches": tools.ledger.searches,
                       "sources": tools.ledger.opened_sources, "evidence": tools.ledger.evidence_items}))}
        reference = index["materials"].get(str(round_index))
        if reference:
            if self.read(reference, "research_investigation_plan") != payload:
                raise ResearchContinuationError("investigation plan or baseline differs from its admitted input")
            return
        artifact = self.artifact(payload)
        index["materials"][str(round_index)] = artifact.handle
        self.commit(state, index, material=artifact)

    def material(self, round_index: int, executor: Any, plan: Any, tools: Any, *, policy: Any) -> PlanExecutionResult:
        state, before = self.state(), len(tools.ledger.searches)
        index = self.index(state)
        key = str(round_index)
        baseline = compute_args_digest(json_value({"searches": tools.ledger.searches,
            "sources": tools.ledger.opened_sources, "evidence": tools.ledger.evidence_items}))
        reference = index["materials"].get(key)
        if reference:
            payload = self.read(reference, "research_material")
            if (set(payload) != {"schema_version", "kind", "plan", "baseline", "material", "observations"}
                    or payload["plan"] != json_value(plan) or payload["baseline"] != baseline):
                raise ResearchContinuationError("research material no longer matches its original plan and baseline")
            material = self.decode_material(payload["material"])
            self.restore_material(tools, plan, material, payload["observations"])
            self.project_urls(tools)
            return material
        if state.writer_attempt >= 2 + (round_index - 1) * 2:
            raise ResearchContinuationError("active research followup has no original material receipt")
        old_urls = set(tools.ledger.final_url_set())
        material = cast(PlanExecutionResult, executor.execute(plan, tools, policy=policy))
        observations = [{"search": json_value(row)} for row in tools.ledger.searches[before:]]
        observations.extend(ledger_observation(tools.ledger, "open_url", url=url)
                            for url in sorted(set(tools.ledger.final_url_set()) - old_urls))
        artifact = self.artifact({"schema_version": 1, "kind": "research_material", "baseline": baseline,
            "plan": json_value(plan), "material": json_value(material), "observations": observations})
        index["materials"][key] = artifact.handle
        self.commit(state, index, material=artifact)
        return material

    @staticmethod
    def decode_material(payload: Any) -> PlanExecutionResult:
        if not isinstance(payload, dict) or set(payload) != {field.name for field in fields(PlanExecutionResult)}:
            raise ResearchContinuationError("invalid research material receipt")
        row = dict(payload)
        for key in ("queries_executed", "opened_sources", "previews", "fresh_source_urls", "baseline_source_urls", "errors"):
            if (not isinstance(row[key], list)
                    or any(not isinstance(item, dict) if key == "opened_sources" else type(item) is not str
                           for item in row[key])):
                raise ResearchContinuationError("invalid research material sequence")
            row[key] = tuple(row[key])
        if any(type(row[key]) is not int or row[key] < 0 for key in ("fresh_source_count", "skipped_count")):
            raise ResearchContinuationError("invalid research material counts")
        if (type(row["stop_reason"]) is not str
                or row["stop_reason"] not in {"policy_denied", "no_queries", "stopped", "max_sources", "search_error",
                                       "no_results", "opened_sources", "no_new_material"}
                or row["fresh_source_count"] != len(row["fresh_source_urls"])
                or len(set(row["fresh_source_urls"])) != row["fresh_source_count"]
                or len(row["opened_sources"]) != row["fresh_source_count"]):
            raise ResearchContinuationError("invalid research material outcome")
        return PlanExecutionResult(**row)

    @staticmethod
    def restore_material(tools: Any, plan: Any, material: PlanExecutionResult, observations: Any) -> None:
        """Validate the entire acquisition before publishing its projection."""
        if not isinstance(observations, list) or len(observations) > 20:
            raise ResearchContinuationError("invalid bounded research observations")
        ledger = tools.ledger.clone()
        before = len(ledger.searches)
        sources = []
        try:
            from codey.research.ledger_receipts import source_observation

            for observation in observations:
                if not isinstance(observation, dict) or set(observation) not in ({"search"}, {"source", "text", "pages"}):
                    raise ValueError("invalid plan acquisition observation")
                if "source" in observation:
                    sources.append(source_observation(observation)[0].to_dict())
                restore_ledger_observation(ledger, observation)
            queries = [" ".join(candidate.query_preview.split()) for candidate in plan.query_candidates]
            if (any(query not in queries for query in material.queries_executed)
                    or any(search.query not in material.queries_executed for search in ledger.searches[before:])
                    or {row["final_url"] for row in sources} != set(material.fresh_source_urls)
                    or sorted(sources, key=lambda row: row["final_url"])
                        != sorted(material.opened_sources, key=lambda row: row["final_url"])):
                raise ValueError("acquired research material differs from its original observations")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ResearchContinuationError(f"invalid research material observation: {exc}") from exc
        tools.ledger.commit_projection(ledger)

    @staticmethod
    def project_urls(tools: Any) -> None:
        tools.sources_read.update(tools.ledger.final_url_set())
        tools.sources_read.update(source.requested_url for source in tools.ledger.opened_sources)
        tools.search_result_urls.update(hit.url for search in tools.ledger.searches for hit in search.results)
