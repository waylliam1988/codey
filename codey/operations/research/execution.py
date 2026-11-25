"""Execute research acquisitions and publish their structured receipts."""
from __future__ import annotations

import contextlib
import copy
from typing import TYPE_CHECKING, Protocol

from codey.operations.browser.browser_detail import audit_browser_open
from codey.operations.kernel_result import result_from_outcome
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation, source_search_observation
from codey.runtime.core import cancellation
from codey.runtime.core.execution_record import HitTarget
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import _CONTROLLER_ALIAS_ID_ARG, SOURCE_READ_TOOL_NAMES, is_source_read_tool
from codey.types.json_value import (
    JsonObject,
    ToolArgs,
    decode_json_object,
    encode_transport_value,
    require_json_integer,
)

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.operations.task_session import TaskSession
    from codey.research.answer_contract import AnswerScope
    from codey.research.receipt_types import SourceSearchObservation
    from codey.storage.managed_outputs import ManagedOutputStore
from codey.research.tools import ResearchToolOutput, ResearchTools


class _SerializableRecord(Protocol):
    def to_dict(self) -> object: ...


def _record_payload(record: _SerializableRecord) -> JsonObject:
    return decode_json_object(record.to_dict())



class ResearchExecutor:
    def __init__(self, *, session: TaskSession | None, research_tools: ResearchTools | None,
                 browser_session: BrowserSession | None, research_scope: AnswerScope | None,
                 managed_outputs: ManagedOutputStore | None, session_id: str, run_id: str,
                 permission_profile: str) -> None:
        self.session = session
        self.research_tools = research_tools
        self.browser_session = browser_session
        self._research_scope = research_scope
        self.managed_outputs = managed_outputs
        self.session_id = session_id
        self.run_id = run_id
        self.permission_profile = permission_profile

    def _research_resources(self) -> ResearchTools:
        if self.research_tools is None:
            raise ValueError("research tools unavailable")
        return self.research_tools

    def _opened_result(self, call: ToolCall, opened: ResearchToolOutput, url: str, *, ledger: ResearchLedger, turn: int,
                       tool_index: int) -> tuple[ToolResult, bool, int | None]:
        model_text = opened.model_text
        if not opened.ok:
            canonical: JsonObject = {"source_failure": {"url": url, "detail": model_text, "code": opened.failure_code}}
            if opened.acquisition:
                canonical["source_acquisition"] = opened.acquisition
            return ToolResult(ok=False, call=call, model_text=model_text,
                              canonical=canonical), False, None
        if not isinstance(ledger, ResearchLedger):
            raise ValueError("successful source read requires a research ledger observation")
        from codey.research.answer_contract import AnswerScope

        if isinstance(self._research_scope, AnswerScope):
            from codey.research.answer_context import fitting_full_read

            resources = self._research_resources()
            assert self.session is not None
            opened = fitting_full_read(self.session, resources.ledger,
                                       call, opened, provider=resources.context_provider)
            model_text = opened.model_text
        observation = opened.read_observation or ledger_observation(ledger, "open", url=url)
        from codey.research.output_receipts import maybe_externalize_output

        title = next((line.removeprefix("Title: ") for line in model_text.splitlines()
                      if line.startswith("Title: ")), "")
        outcome = maybe_externalize_output(
            store=self.managed_outputs, session_id=self.session_id, run_id=self.run_id,
            permission_profile=self.permission_profile, call=call,
            output=opened.receipt_text or model_text, ok=opened.ok,
            turn=turn, tool_index=tool_index, presentation_result=title,
            model_text_override=model_text,
        )
        base = result_from_outcome(call, outcome)
        merged = dict(base.canonical)
        merged["research_observation"] = observation
        if opened.acquisition:
            merged["source_acquisition"] = opened.acquisition
        if opened.read_passages:
            merged["read_passages"] = list(opened.read_passages)
        result = ToolResult(
            ok=base.ok, call=base.call, model_text=base.model_text, truncated=base.truncated,
            presentation=dict(base.presentation) if isinstance(base.presentation, dict) else {},
            audit=dict(base.audit) if isinstance(base.audit, dict) else {},
            canonical=merged,
        )
        return result, True, None

    def _observation_tools(self, *, source_read: bool = False) -> ResearchTools:
        # Acquisition mutates a temporary ledger. Only the committed result
        # receipt may publish it into the run's formal projection.
        tools = copy.copy(self._research_resources())
        from codey.research.answer_contract import AnswerScope

        tools.coherent_passages = isinstance(self._research_scope, AnswerScope)
        # Per-call presentation follows the admitted request, including replay.
        tools.evidence_only = self.session is not None and self.session.task_kind == "evidence_followup"
        tools.finding_report = (self.session is not None and self.session.task_kind == "research"
                                and self.session.policy.strict_research)
        tools.answer_submission = self._research_scope is not None
        ledger = tools.ledger
        # Acquisition keeps source version metadata for monotonic timestamps,
        # while the receipt still starts without stale bodies or passages.
        with self.session.projection_lock if self.session is not None else contextlib.nullcontext():
            tools.ledger = ledger.clone_for_acquisition() if source_read else ledger.clone()
        tools.sources_read = {url for source in ledger.opened_sources
                              for url in (source.requested_url, source.final_url)}
        tools.search_result_urls = {hit.url for search in ledger.searches for hit in search.results}
        return tools

    def _reserve_research_acquisition(self, name: str, turn: int, tool_index: int) -> bool:
        if self.session is not None and self.session.research_acquisition_limits is not None:
            category = "search" if name == "web_search" else "open" if is_source_read_tool(name) else ""
            if category:
                with self.session.projection_lock:
                    slots = self.session.research_acquisition_slots
                    limit = self.session.research_acquisition_limits[0 if category == "search" else 1]
                    # Rebuilt from receipts on restore, plus in-flight slots.
                    # Failed acquisitions consume budget too.
                    previous = {key for key, kind in slots.items() if kind == category}
                    if (turn, tool_index) not in previous and len(previous) >= limit:
                        return False
                    slots[(turn, tool_index)] = category
        return True

    def _research_call_denial(self, call: ToolCall) -> tuple[ToolResult, bool, int | None] | None:
        from codey.operations.kernel_result import policy_denial_result
        from codey.research.answer_contract import digest

        try:
            if self._research_scope is not None:
                scope = self._research_scope
                if self.session is None or scope.task_digest != digest(self.session.task_text):
                    raise ValueError("research scope belongs to another task")
        except ValueError as exc:
            return policy_denial_result(call, str(exc), reason_code="research_scope_denied"), False, None
        return None

    def execute(self, call: ToolCall, *, turn: int, tool_index: int) -> tuple[ToolResult, bool, int | None]:
        if not self._reserve_research_acquisition(call.name, turn, tool_index):
            return ToolResult(ok=False, call=call, model_text="ERROR: research acquisition budget exhausted"), False, None
        return self._execute_research(call, turn=turn, tool_index=tool_index)

    def calculate(self, call: ToolCall) -> ToolResult:
        from codey.research.calculation import calculate, read_data

        assert self.session is not None
        try:
            value = (calculate(call.args, self.session) if call.name == "calculate"
                     else read_data(call.args, self.session, self._research_scope))
            return ToolResult(call, str(value), ok=True, canonical={"value": value})
        except (ValueError, KeyError, IndexError, ArithmeticError) as exc:
            return ToolResult(call, "Calculation rejected: " + str(exc), ok=False)

    def _execute_research(self, call: ToolCall, *, turn: int = 0,
                          tool_index: int = 0) -> tuple[ToolResult, bool, int | None]:
        name = (call.name or "").strip().lower()
        tools = self._observation_tools(source_read=name in SOURCE_READ_TOOL_NAMES)
        args = dict(call.args or {})
        try:
            denied = self._research_call_denial(call)
            if denied is not None:
                return denied
            if name == "research_plan":
                return self._record_research_plan(call, tools)
            if name == "submit_research_answer":
                return self._submit_research_answer(call, tools)
            if name == "web_search":
                from codey.research.text_args import first_text_arg

                outcome = tools.web_search(first_text_arg(args, "query"))
                canonical: JsonObject = {}
                ledger = tools.ledger
                if outcome.ok:
                    if not isinstance(ledger, ResearchLedger):
                        raise ValueError("successful search requires a research ledger observation")
                    canonical["research_observation"] = ledger_observation(ledger, name)
                else:
                    from codey.research.search_provenance import provider_search_attempts

                    canonical["search_attempts"] = encode_transport_value(provider_search_attempts(tools.search))
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text, canonical=canonical), outcome.ok, None
            if name in SOURCE_READ_TOOL_NAMES:
                return self._execute_source_open(call, tools, turn=turn, tool_index=tool_index)
            if name == "source_search":
                query = str(args.get("query") or "")
                url = str(args.get("url") or "")
                if not url and str(args.get("source_id") or ""):
                    url = self._resolve_alias_url("reopen_source", {"source_id": args.get("source_id")}) or ""
                outcome = tools.source_search(url, query, args.get("limit", 6))
                if outcome.ok:
                    ledger = tools.ledger
                    if not isinstance(ledger, ResearchLedger):
                        raise ValueError("successful source search requires a research ledger observation")
                    observation = ledger_observation(ledger, name)
                    text, mapping = self._build_hit_mapping(source_search_observation(observation))
                    hit_canonical: dict[str, object] = {"hit_targets": mapping, "research_observation": observation}
                    return (
                        ToolResult(ok=outcome.ok, call=call, model_text=text, canonical=hit_canonical),
                        True, None,
                    )
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text), outcome.ok, None
            if name == "knowledge_search":
                from codey.research.text_args import first_text_arg

                outcome = tools.knowledge_search(first_text_arg(args, "query"),
                                                 depth=require_json_integer(args.get("depth", 1), "depth"))
                search_canonical = ({"knowledge_search_empty": True}
                    if outcome.ok and outcome.knowledge_search_empty is True else {})
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text,
                                  canonical=search_canonical), outcome.ok, None
            if name == "knowledge_read":
                return self._read_research_knowledge(call, tools)
            if name == "knowledge_commit":
                return self._commit_research_knowledge(call, tools)
            if name in {"knowledge_write", "save_finding", "save_inference"}:
                return self._execute_research_write(call, tools)
            if name == "knowledge_link":
                links_before = tools.links_created
                outcome = tools.knowledge_link(str(args.get("src") or ""), str(args.get("dst") or ""),
                                            str(args.get("kind") or "relates"))
                return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text,
                    canonical={"knowledge_links_created": tools.links_created - links_before}), outcome.ok, None
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception as exc:
            return ToolResult(ok=False, call=call, model_text=f"ERROR: {exc}"), False, None
        return ToolResult(ok=False, call=call, model_text=f"ERROR: unknown research tool {name}"), False, None

    def _execute_source_open(self, call: ToolCall, tools: ResearchTools, *, turn: int,
                             tool_index: int) -> tuple[ToolResult, bool, int | None]:
        from codey.operations.source_execution import open_source
        from codey.research.source_snapshots import SourceSnapshotUnavailable

        if call.name == "open_url":
            url = str(call.args.get("url") or "")
        else:
            url = self._resolve_alias_url(call.name, dict(call.args))
            if not url:
                field = _CONTROLLER_ALIAS_ID_ARG[call.name][1]
                detail = f"ERROR: unknown {call.name} id. Use {field} from the observed references. "
                detail += "A tool result_ref or out_ artifact handle is not a source locator."
                return ToolResult(ok=False, call=call, model_text=detail), False, None
        target = (self.session.hit_targets.get(str(call.args.get("hit_id") or "").lower())
                  if call.name == "open_hit" and self.session is not None else None)
        try:
            opened = open_source(call, tools, self._research_resources().ledger, url, target)
        except SourceSnapshotUnavailable as exc:
            opened = ResearchToolOutput(f"ERROR: {exc}", ok=False, failure_code="snapshot_unavailable")
        result, ok, code = self._opened_result(call, opened, url, ledger=tools.ledger, turn=turn, tool_index=tool_index)
        result = audit_browser_open(result, self.session, self.browser_session, url)
        return result, ok, code

    def _commit_research_knowledge(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        outcome = tools.knowledge_commit(call.args)
        return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text), outcome.ok, None

    def _read_research_knowledge(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        from codey.research.answer_contract import AnswerScope
        from codey.research.questions import question_ref

        args = call.args
        identity = str(args.get("id") or "")
        revision = require_json_integer(args["revision"], "revision") if args.get("revision") is not None else None
        outcome = tools.knowledge_read(identity, revision=revision, question_ref=question_ref(args, tools.ledger),
                                       offset=require_json_integer(args.get("offset", 0), "offset"),
                                       limit=require_json_integer(args.get("limit", 6000), "limit"),
                                       whole_answer=isinstance(self._research_scope, AnswerScope))
        text = outcome.model_text
        if not outcome.ok and isinstance(self._research_scope, AnswerScope):
            if identity in tools.ledger.read_passages:
                text = ("ERROR: this id names a task-read passage, not stored knowledge. "
                        "Cite its existing [eN] label directly. knowledge_read needs an exact stored id and revision.")
            elif self.session is not None and identity in self.session.source_ids:
                text = ("ERROR: this id is a source locator, not stored knowledge. "
                        "Use the existing [eN] evidence, or the displayed source-reading action for unread text. "
                        "knowledge_read needs an exact stored id and revision.")
        canonical: dict[str, object] = {
            "research_reference_bindings": {k: _record_payload(v) for k, v in tools.ledger.references.items()}} if outcome.ok else {}
        if outcome.ok and outcome.knowledge_read_versions:
            canonical["knowledge_read_versions"] = [
                {"id": ref.target, "revision": ref.revision} for ref in outcome.knowledge_read_versions]
        if outcome.ok and outcome.read_passages:
            canonical.update(research_observation=outcome.read_observation, read_passages=list(outcome.read_passages))
        return ToolResult(ok=outcome.ok, call=call, model_text=text, canonical=canonical), outcome.ok, None

    def _record_research_plan(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        outcome = tools.research_plan(call.args.get("questions"))
        canonical = {"research_observation": outcome.read_observation} if outcome.ok else {}
        return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text, canonical=canonical), outcome.ok, None

    def _submit_research_answer(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        from codey.research.answer_contract import AnswerScope, submit_answer
        from codey.research.primary_sources import PRIMARY_ACQUISITION_IDS

        if not isinstance(self._research_scope, AnswerScope):
            raise ValueError("submit_research_answer requires an original frozen whole-answer scope")
        last = next(reversed(self.session._memory_results.items()), None) if self.session is not None else None
        if (last is not None and last[1].call.call_id in PRIMARY_ACQUISITION_IDS and last[1].ok
                and last[0] not in self._research_scope.attempt_refs and call.args.get("state") == "answered"):
            raise ValueError("first_party_followup_available: review the just-acquired source results before submitting.")
        declared = submit_answer(self._research_scope, call.args, passages=tools.ledger.read_passages)
        from codey.research.report_evidence import (
            CLAIM_CHECK_VERSION,
            check_report_evidence,
            exhausted_review_answer,
            research_review_key,
            review_submission_issues,
        )

        evidence_check = check_report_evidence(declared, tools.ledger.read_passages, claim_check_version=CLAIM_CHECK_VERSION)
        issues = review_submission_issues(declared, evidence_check, tools.ledger.read_passages)
        resolution: dict[str, object] = {}
        if issues:
            issue = issues[0]
            prior_reviews = self.session._memory_results.values() if self.session is not None else ()
            fallback = exhausted_review_answer(self._research_scope, issue,
                tuple(result.canonical.get("research_review_key") for result in prior_reviews))
            if fallback is None:
                feedback = (f"Research submission needs review. The draft says {issue['claim']!r}. "
                            f"Its cited [{issue['label']}] says {issue['original']!r}. "
                            "Reconcile the claim with the cited original, cite other evidence that supports it, "
                            "or describe the unresolved point and submit the supported parts.")
                return ToolResult(call, feedback, ok=False, canonical={"research_submission_issue": issue,
                    "research_review_key": research_review_key(issue)}), False, None
            declared = fallback
            resolution["research_review_exhausted"] = issue
            evidence_check = check_report_evidence(declared, tools.ledger.read_passages, claim_check_version=CLAIM_CHECK_VERSION)
        if self.session is not None:
            from codey.research.browser_result_evidence import validate_current_result

            validate_current_result(self.session.executed, self.session._memory_results,
                declared["markdown"], declared["state"], self._research_scope.aliases)
        return ToolResult(call, "Whole answer recorded. The application will validate and publish this submission.",
                          ok=True, canonical={**resolution, "research_submission": declared,
                                              "research_evidence_check": evidence_check}), True, None

    def _execute_research_write(self, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
        name, args = call.name, dict(call.args or {})
        if name == "save_finding" and self.session is not None:
            from codey.research.finding_stage import finding_stage_error

            if error := finding_stage_error(self.session, args):
                return ToolResult(call, error, ok=False), False, None
        before = len(tools.ledger.evidence_items)
        created_before, updated_before = set(tools.created_ids), set(tools.updated_ids)
        links_before = tools.links_created
        lowered, problem = (self._lower_finding_source(args) if name == "save_finding" else
                            (args, "") if name == "save_inference" else self._lower_knowledge_sources(args))
        if problem:
            return ToolResult(ok=False, call=call, model_text=f"ERROR: {problem}"), False, None
        outcome = (tools.save_finding(lowered) if name == "save_finding" else
                   tools.save_inference(lowered) if name == "save_inference" else tools.knowledge_write(lowered))
        if not outcome.ok:
            return ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text), False, None
        evidence = self._ledger_evidence(before, tools.ledger)
        evidence_canonical: dict[str, object] = {"evidence_items": evidence}
        if name in {"save_finding", "save_inference"}:
            evidence_canonical["research_reference_bindings"] = {k: _record_payload(v) for k, v in tools.ledger.references.items()}
        evidence_canonical["knowledge_links_created"] = tools.links_created - links_before
        proposals: list[object] = []
        created = [item for item in tools.created_ids if item not in created_before]
        target = str(lowered.get("id") or "")
        changed = [item for item in tools.updated_ids if item not in updated_before]
        if target in tools.updated_ids:
            changed.append(target)
        updated = list({item: None for item in changed})
        evidence_canonical["knowledge_notes"] = {
            "created": created,
            "updated": updated,
            "proposals": proposals,
        }
        affected = list({item: None for item in (*created, *updated)})
        self._staged_note_receipt(tools, affected, proposals, evidence_canonical)
        ledger = tools.ledger
        if isinstance(ledger, ResearchLedger) and name != "save_inference":
            evidence_canonical["research_observation"] = ledger_observation(ledger, name, evidence_start=before)
        return (
            ToolResult(ok=outcome.ok, call=call, model_text=outcome.model_text, canonical=evidence_canonical),
            True, None,
        )

    @staticmethod
    def _staged_note_receipt(tools: ResearchTools, created: list[str], proposals: list[object], canonical: dict[str, object]) -> None:
        from codey.providers.response_record import json_value
        from codey.research.staging import StagedKnowledgeStore

        store = tools.store
        if isinstance(store, StagedKnowledgeStore):
            proposals.extend(json_value(store._staged_notes[identity]) for identity in created)
            canonical["knowledge_bases"] = {identity: store._staged_bases[identity] for identity in created}
            canonical["knowledge_batches"] = {
                identity: {"commit_id": batch[0], "mutations": [
                    {"value": _record_payload(mutation.value), "expected_revision": mutation.expected_revision}
                    for mutation in batch[1]]}
                for identity, batch in store._staged_knowledge.items() if identity in created
            }

    def _lower_knowledge_sources(self, args: ToolArgs) -> tuple[ToolArgs, str]:
        from codey.operations.source_execution import lower_knowledge_sources

        return lower_knowledge_sources(self.session, args)

    def _lower_finding_source(self, args: ToolArgs) -> tuple[ToolArgs, str]:
        # The selected passage owns the source identity. An explicitly supplied
        # source is only an assertion to validate, including frozen older calls.
        if "source_ref" not in args:
            return args, ""
        reference = args.get("source_ref")
        sources: dict[str, str] = dict(self.session.source_ids) if self.session is not None else {}
        if not isinstance(reference, str) or reference not in sources:
            return {}, "source_ref must be an exact opened source reference: " + ", ".join(sources)
        return {**args, "source_ref": sources[reference]}, ""

    def _resolve_alias_url(self, alias: str, args: ToolArgs) -> str:
        from codey.operations.source_execution import resolve_alias_url

        return resolve_alias_url(self.session, alias, args)

    def _build_hit_mapping(self, observation: SourceSearchObservation) -> tuple[str, dict[str, HitTarget]]:
        from codey.research.source_locators import build_hit_mapping

        return build_hit_mapping(observation, self.session.hit_targets if self.session is not None else {})

    def _ledger_evidence(self, before: int, ledger: ResearchLedger) -> list[dict[str, str]]:
        items = list(ledger.evidence_items)
        fresh = items[before:] if before >= 0 else items
        evidence: list[dict[str, str]] = []
        for item in fresh:
            url = item.source_url.strip()
            excerpt = item.excerpt.strip()
            if url and excerpt:
                evidence.append({"source_url": url, "excerpt": excerpt[:600]})
        return evidence
