"""Live A/B probe for the 0.4.4 bounded Research planner.

The baseline arm runs the production ResearchPipeline with follow-up disabled.
The planner arm enables one bounded follow-up round. Both arms use the same
production ResearchRunner and deterministic fixture search provider, while the
web-model provider is live. Progress is written atomically after each row, and
an optional trace file records every provider send/reply pair.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey import provider_controls
from codey.knowledge.store import KnowledgeStore
from codey.providers.registry import connect_provider, provider_ids
from codey.research.context import ResearchContext, ResearchPipelineConfig
from codey.research.controller import ResearchController, ResearchControlState
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.identity import digest_json, digest_text, sanitize_research_url_ref, stable_ref
from codey.research import plan_executor as research_plan_executor_module
from codey.research import tools as research_tools_module
from codey.research import url_policy as research_url_policy_module
from codey.research.object_model import (
    RESEARCH_RECORD_KIND,
    RESEARCH_RECORD_SCHEMA_VERSION,
    EvidenceLocator,
    ResearchClaim,
    ResearchClaimRelation,
    ResearchEvidence,
    ResearchQuestion,
    ResearchRecord,
    ResearchSource,
)
from codey.research.plan_executor import PlanExecutionResult
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.proof_quality import review_research_proof
from codey.research.protocols import extract_json_objects
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.runner import ResearchRunner, ResearchRunResult
from codey.research.tools import ResearchTools, clone_research_tools


RESULTS_DIR = Path(__file__).resolve().parent / "results"
WEB_PROVIDERS = tuple(provider_id for provider_id in provider_ids() if provider_id != "local")
ARMS = ("baseline", "planner")
MAX_RESULT_BYTES = 1024 * 1024
MAX_TRACE_BYTES = 2 * 1024 * 1024
TRACE_PROMPT_CHARS = 8000
TRACE_REPLY_CHARS = 8000
AB_EVIDENCE_ONLY_FOLLOWUP_MODE = "evidence_only_patch_merge"


@dataclass(frozen=True)
class FixtureDocument:
    url: str
    title: str
    text: str
    keywords: tuple[str, ...] = ()
    default: bool = False

    def result(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": _clip(self.text, 260),
        }


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    documents: tuple[FixtureDocument, ...]
    expected_terms: tuple[str, ...]


CASES = {
    "warehouse_gap": Case(
        name="warehouse_gap",
        question=(
            "Research whether lithium storage retrofits are practical for small "
            "warehouses. Include the main benefit and the main limitation."
        ),
        documents=(
            FixtureDocument(
                "https://source-a.test/lithium-storage-benefit",
                "Benchmark source A",
                (
                    "Lithium storage retrofits can reduce peak demand charges in "
                    "small warehouses with predictable loads and daily cycling."
                ),
                keywords=("benefit", "practical", "warehouse", "retrofit", "lithium"),
                default=True,
            ),
            FixtureDocument(
                "https://source-b.test/lithium-storage-limit",
                "Benchmark source B",
                (
                    "The main limitation is that fire-code setbacks may require a "
                    "separated battery room and ventilation retrofit costs can exceed "
                    "the demand-charge savings."
                ),
                keywords=("limitation", "limit", "counter", "fire", "setback", "ventilation"),
            ),
        ),
        expected_terms=("peak demand charges", "fire-code setbacks"),
    ),
    "widget_noop": Case(
        name="widget_noop",
        question=(
            "Research the current Widget Storage API recommendation and cite the "
            "recommended endpoint."
        ),
        documents=(
            FixtureDocument(
                "https://source-a.test/widget-storage",
                "Benchmark source A",
                (
                    "The Widget Storage standard still recommends the stable-v2 "
                    "endpoint for client storage integration."
                ),
                keywords=("widget", "storage", "endpoint", "stable-v2", "recommend"),
                default=True,
            ),
            FixtureDocument(
                "https://source-b.test/widget-storage-update",
                "Benchmark source B",
                (
                    "The Widget Storage working group has not adopted a stable-v3 "
                    "successor; stable-v2 remains the recommended endpoint."
                ),
                keywords=("primary", "source", "evidence", "current"),
            ),
        ),
        expected_terms=("stable-v2",),
    ),
}


class FixtureSearchProvider:
    name = "fixture-search"

    def __init__(self, case: Case) -> None:
        self.case = case
        self.queries: list[str] = []
        self.fetches: list[str] = []
        self.material_phase = False

    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        self.queries.append(str(query or ""))
        lower = str(query or "").casefold()
        if self.material_phase:
            matches = [
                doc
                for doc in self.case.documents
                if doc.keywords and any(keyword.casefold() in lower for keyword in doc.keywords)
            ]
        else:
            matches = []
        defaults = [doc for doc in self.case.documents if doc.default]
        ordered: list[FixtureDocument] = []
        for doc in [*matches, *defaults]:
            if doc not in ordered:
                ordered.append(doc)
        return [doc.result() for doc in ordered[: max(0, int(limit or 0))]]

    def fetch(self, url: str) -> dict[str, object]:
        self.fetches.append(str(url or ""))
        for doc in self.case.documents:
            if doc.url == url:
                return {
                    "url": doc.url,
                    "title": doc.title,
                    "text": doc.text,
                    "truncated": False,
                }
        return {
            "url": url,
            "title": "",
            "text": "ERROR: fixture URL not found",
            "truncated": False,
        }


@contextmanager
def _fixture_material_phase(search: object) -> Any:
    if not isinstance(search, FixtureSearchProvider):
        yield
        return
    previous = bool(search.material_phase)
    search.material_phase = True
    try:
        yield
    finally:
        search.material_phase = previous


@contextmanager
def _fixture_url_policy_bypass() -> Any:
    original_research = research_url_policy_module.check_fetch_url
    original_tools = research_tools_module.check_fetch_url
    original_plan_executor = research_plan_executor_module.check_fetch_url

    def _allow_fixture_urls(url: str, *, resolve: bool = True) -> str | None:
        text = str(url or "").strip().lower()
        if text.startswith(("https://source-a.test/", "https://source-b.test/")):
            return None
        return original_research(url, resolve=resolve)

    research_url_policy_module.check_fetch_url = _allow_fixture_urls
    research_tools_module.check_fetch_url = _allow_fixture_urls
    research_plan_executor_module.check_fetch_url = _allow_fixture_urls
    try:
        yield
    finally:
        research_url_policy_module.check_fetch_url = original_research
        research_tools_module.check_fetch_url = original_tools
        research_plan_executor_module.check_fetch_url = original_plan_executor


class FreshMaterialPlanExecutor:
    """A/B-only executor variant that treats already-opened URLs as non-material."""

    def __init__(self, *, config: ResearchPipelineConfig, should_stop) -> None:
        self.config = config
        self.should_stop = should_stop

    def execute(self, plan: ResearchPlan, tools: ResearchTools) -> PlanExecutionResult:
        runtime = clone_research_tools(tools)
        baseline_opened = _opened_url_set(runtime)
        queries: list[str] = []
        opened: list[dict[str, Any]] = []
        previews: list[str] = []
        skipped = 0
        errors: list[str] = []
        seen = set(baseline_opened)
        query_limit = max(0, min(int(plan.max_queries or 0), int(self.config.max_queries_per_round or 0)))
        total_limit = max(0, int(self.config.max_total_sources or 0))
        per_query_limit = max(0, int(self.config.max_sources_per_query or 0))
        stop_reason = "no_queries"
        for candidate in plan.query_candidates[:query_limit]:
            if self.should_stop():
                stop_reason = "stopped"
                break
            query = " ".join(str(candidate.query_preview or "").split())
            if not query:
                skipped += 1
                continue
            before_searches = len(runtime.ledger.searches)
            with _fixture_material_phase(runtime.search):
                result = runtime.web_search(query)
            queries.append(query)
            if str(result or "").startswith("ERROR:"):
                errors.append(_clip(result, 180))
                stop_reason = "search_error"
                continue
            search = runtime.ledger.searches[-1] if len(runtime.ledger.searches) > before_searches else None
            if search is None:
                stop_reason = "no_results"
                continue
            opened_for_query = 0
            for hit in search.results:
                if len(opened) >= total_limit:
                    stop_reason = "max_sources"
                    break
                if opened_for_query >= per_query_limit:
                    break
                url = str(hit.url or "").strip()
                if not url or url in seen or runtime.ledger.canonical_opened_url(url):
                    skipped += 1
                    seen.add(url)
                    continue
                seen.add(url)
                before_opened = _opened_url_set(runtime)
                body = runtime.open_url(url, limit=self.config.max_source_preview_chars)
                text = str(body or "")
                if text.startswith(("ERROR:", "SKIPPED:")):
                    skipped += 1
                    errors.append(_clip(text, 180))
                    continue
                after_opened = _opened_url_set(runtime)
                new_urls = sorted(after_opened - before_opened - baseline_opened)
                if not new_urls:
                    skipped += 1
                    continue
                source = _opened_source_payload(runtime, new_urls[-1])
                if not source:
                    skipped += 1
                    continue
                opened.append(source)
                previews.append(_source_preview(query, source, text, self.config.max_source_preview_chars))
                opened_for_query += 1
                stop_reason = "opened_sources"
            if stop_reason in {"max_sources", "stopped"}:
                break
        if queries and not opened and stop_reason not in {"search_error", "stopped"}:
            stop_reason = "no_new_material"
        return PlanExecutionResult(
            queries_executed=tuple(queries),
            opened_sources=tuple(opened),
            previews=tuple(previews),
            skipped_count=skipped,
            stop_reason=stop_reason,
            errors=tuple(errors[:12]),
        )


class ABEvidenceOnlyFollowupController(ResearchController):
    """Harness-only controller that turns follow-up into evidence capture.

    Production follow-up still uses the normal Research controller. This probe
    tests a narrower loop where the model can only save evidence for freshly
    opened material, and the harness does the final patch merge deterministically.
    """

    def __init__(self, *, required_urls: tuple[str, ...]) -> None:
        super().__init__(include_source_search=False)
        self.required_urls = tuple(url for url in required_urls if url)

    def build_state(self, tools: object, *, turn: int, max_turns: int) -> ResearchControlState:
        state = super().build_state(tools, turn=turn, max_turns=max_turns)
        source_urls = {
            key: value
            for key, value in state.source_urls.items()
            if not self.required_urls or value in self.required_urls
        }
        required = set(self.required_urls)
        source_lines = tuple(
            line for line in state.source_lines if not required or any(url in line for url in required)
        )
        noncitable_source_lines = tuple(
            line for line in state.noncitable_source_lines
            if not required or any(url in line for url in required)
        )
        citable_source_lines = tuple(
            line for line in state.citable_source_lines
            if not required or any(url in line for url in required)
        )
        return replace(
            state,
            allowed_tools=("knowledge_write",),
            source_urls=source_urls,
            result_lines=(),
            hit_lines=(),
            source_lines=source_lines,
            noncitable_source_lines=noncitable_source_lines,
            citable_source_lines=citable_source_lines,
            done_escape=False,
        )

    def append_block(self, message: str, state: ResearchControlState) -> str:
        urls = self.required_urls or tuple(dict.fromkeys(state.source_urls.values()))
        lines = [
            "A/B evidence-only follow-up controller:",
            f"- mode: {AB_EVIDENCE_ONLY_FOLLOWUP_MODE}",
            "- Allowed tools this turn: knowledge_write",
            "- Forbidden tools this turn: done, web_search, open_result, reopen_source, open_hit, source_search, knowledge_search, knowledge_read, knowledge_link.",
            "- Reply with exactly one JSON object using only knowledge_write.",
            "- Do not call done, do not write a final report, and do not rewrite the prior report.",
            "- Save only narrow new evidence that directly patches the stated gap.",
            "- Do not add broader claims, new limitations, authority labels, independence claims, or coverage claims.",
            "- Use each opened_material.final_url string exactly in sources and evidence.source_url.",
            "- Do not use s1, s2, source_id, result_id, hit_id, or hand-written source labels as provenance.",
        ]
        if urls:
            lines.append("- Required opened_material.final_url values:")
            lines.extend(f"  - {url}" for url in urls)
        lines.extend([
            "- Allowed JSON shape:",
        ])
        for url in urls[:4] or ("https://example.test/source",):
            example = {
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Narrow follow-up evidence",
                    "body": "One short evidence-backed claim from the opened material.",
                    "sources": [url],
                    "evidence": [{
                        "claim": "The specific claim supported by the opened material.",
                        "source_url": url,
                        "excerpt": "Exact short excerpt copied from the opened material.",
                        "stance": "supports",
                    }],
                },
            }
            lines.append(f"- {json.dumps(example, ensure_ascii=True, separators=(',', ':'))}")
        if state.source_lines:
            lines.extend(["", "Opened material sources available for knowledge_write:"])
            lines.extend(f"- {line}" for line in state.source_lines)
        lines.extend(["", "Do not output multiple JSON objects."])
        return str(message or "").rstrip() + "\n\n" + "\n".join(lines)


def _ab_followup_final_urls(iteration_context: str) -> tuple[str, ...]:
    urls: list[str] = []
    for raw in str(iteration_context or "").splitlines():
        line = raw.strip()
        prefix = "- https://"
        if line.startswith(prefix):
            urls.append(line.removeprefix("- ").strip())
            continue
        marker = ".final_url:"
        if marker in line:
            _, value = line.split(marker, 1)
            url = value.strip()
            if url.startswith("https://"):
                urls.append(url)
    return tuple(dict.fromkeys(url for url in urls if url))


def _ab_is_evidence_only_followup(arm: str, iteration_context: str) -> bool:
    return arm == "planner" and bool(_ab_followup_final_urls(iteration_context))


def _opened_url_set(tools: ResearchTools) -> set[str]:
    urls = {str(url or "").strip() for url in getattr(tools, "sources_read", set())}
    urls.update(str(url or "").strip() for url in tools.ledger.final_url_set())
    for item in tools.ledger.opened_sources:
        urls.add(str(item.requested_url or "").strip())
        urls.add(str(item.final_url or "").strip())
    return {url for url in urls if url}


def _opened_source_payload(tools: ResearchTools, url: str) -> dict[str, Any]:
    final_url = tools.ledger.canonical_opened_url(url) or str(url or "")
    for item in tools.ledger.opened_sources:
        if item.final_url == final_url or item.requested_url == final_url:
            return item.to_dict()
    return {}


def _source_preview(query: str, source: dict[str, Any], body: str, limit: int) -> str:
    title = str(source.get("title") or "").strip()
    final_url = str(source.get("final_url") or source.get("url") or "").strip()
    header = " | ".join(part for part in (title, final_url) if part)
    text = _clip(body, max(500, min(4000, int(limit or 0))))
    return "\n".join(part for part in (f"query: {query}", header, text) if part)


def _ab_followup_context(
    *,
    question: str,
    initial: Any,
    plan: ResearchPlan,
    material: PlanExecutionResult,
    limit: int,
) -> str:
    opened_sources = tuple(source for source in material.opened_sources if isinstance(source, dict))
    final_urls = tuple(
        str(source.get("final_url") or source.get("url") or "").strip()
        for source in opened_sources
        if str(source.get("final_url") or source.get("url") or "").strip()
    )
    lines = [
        "A/B follow-up Research synthesis input.",
        f"question: {_clip(question, 240)}",
        f"initial_stop_reason: {getattr(initial, 'stop_reason', '')}",
        f"initial_summary: {_clip(getattr(initial, 'summary', ''), 1200)}",
        f"plan_ref: {plan.plan_ref}",
        "queries_executed:",
        *[f"- {_clip(query, 180)}" for query in material.queries_executed],
        "opened_material_final_urls:",
        *[f"- {url}" for url in final_urls],
        "follow_up_synthesis_scope:",
        "- This is an evidence-only repair pass, not a second full research rewrite.",
        "- Do not call done and do not write a final report in this follow-up.",
        "- The A/B harness will merge saved evidence into the initial report deterministically.",
        "- Add only evidence items that directly support the narrow gap the opened material closes.",
        "- If the new material merely confirms an existing answer, save that evidence and keep the claim narrow.",
        "- Do not add broader claims, extra limitations, authority labels, independence claims, or coverage claims unless exact evidence supports them.",
        "- Do not say sources are independent, official, current, or comprehensive just because there is one more source.",
        "- The synthetic URLs and titles are benchmark locators only; do not infer authority, freshness, or official status from domain or title.",
        "follow_up_output_contract:",
        "- The only allowed model output is one knowledge_write call.",
        "- Save one concise fact note per useful opened_material.final_url.",
        "- Do not restate the answer, do not edit 结论, and do not produce 来源 text.",
        "- The deterministic merge will add the new evidence-backed claim and source line.",
        "follow_up_material_rules:",
        "- Treat every opened_material.final_url below as the canonical source URL for this follow-up.",
        "- Call knowledge_write for every opened_material.final_url that directly closes the gap.",
        "- In knowledge_write args, sources must contain the final URL string exactly.",
        "- In each evidence item, source_url must be the same final URL string exactly.",
        "- Do not put s1, s2, source_id, result_id, or hit_id in sources or evidence.source_url.",
        "- Do not call search/open/read/link/done; if a write cannot be made, return the closest valid knowledge_write with a narrow unsupported-by-new-material note omitted.",
        "- Do not cite or list a new URL in prose; provenance belongs only in knowledge_write sources/evidence.source_url.",
        "knowledge_write_templates:",
    ]
    for index, source in enumerate(opened_sources, 1):
        final_url = str(source.get("final_url") or source.get("url") or "").strip()
        if not final_url:
            continue
        title = str(source.get("title") or "").strip() or final_url
        template = {
            "tool": "knowledge_write",
            "args": {
                "type": "fact",
                "title": f"Evidence from {title}",
                "body": "Write one narrow claim supported by this opened material; keep the report as a patch, not a rewrite.",
                "sources": [final_url],
                "evidence": [{
                    "claim": "The specific claim supported by the opened material.",
                    "source_url": final_url,
                    "excerpt": "Exact short excerpt copied from the opened material.",
                    "stance": "supports",
                }],
            },
        }
        lines.extend([
            f"- opened_material.{index}.final_url: {final_url}",
            f"  opened_material.{index}.title: {_clip(title, 160)}",
            f"  required_write_shape: {json.dumps(template, ensure_ascii=True, separators=(',', ':'))}",
        ])
    lines.extend([
        "opened_material_previews:",
        *[f"- {_clip(preview, 1600)}" for preview in material.previews],
    ])
    if material.errors:
        lines.extend(["bounded_errors:", *[f"- {_clip(error, 180)}" for error in material.errors]])
    return _clip("\n".join(lines), max(1000, min(20000, int(limit or 0))))


@dataclass(frozen=True)
class PatchOnlyMerge:
    result: ResearchRunResult | None = None
    applied: bool = False
    reason: str = ""
    new_source_urls: tuple[str, ...] = ()
    new_evidence_count: int = 0
    new_claim_count: int = 0

    def row_payload(self) -> dict[str, Any]:
        return {
            "ab_patch_merge_applied": bool(self.applied),
            "ab_patch_merge_reason": self.reason,
            "ab_patch_merge_new_source_urls": list(self.new_source_urls[:8]),
            "ab_patch_merge_new_evidence_count": max(0, int(self.new_evidence_count or 0)),
            "ab_patch_merge_new_claim_count": max(0, int(self.new_claim_count or 0)),
        }


def _ab_patch_only_merge_result(
    initial: ResearchRunResult | None,
    followup: ResearchRunResult | None,
    material: PlanExecutionResult | None = None,
) -> PatchOnlyMerge:
    if initial is None or followup is None:
        return PatchOnlyMerge(reason="missing_iteration")
    initial_record = initial.research_record
    followup_record = followup.research_record
    if initial_record is None or followup_record is None:
        return _ab_material_patch_only_merge_result(
            initial,
            followup,
            material,
            prior_reason="missing_record",
        )

    initial_source_ids = {item.source_id for item in initial_record.sources}
    initial_evidence_ids = {item.evidence_id for item in initial_record.evidence}
    new_sources = tuple(
        item for item in followup_record.sources if item.source_id not in initial_source_ids
    )
    new_source_ids = {item.source_id for item in new_sources}
    source_payloads = _ab_source_payloads_by_id(followup)
    source_url_by_id = {
        source_id: str(payload.get("final_url") or payload.get("url") or "").strip()
        for source_id, payload in source_payloads.items()
    }
    new_source_urls = tuple(
        url
        for source in new_sources
        for url in (source_url_by_id.get(source.source_id, ""),)
        if url
    )
    if not new_source_urls:
        return _ab_material_patch_only_merge_result(
            initial,
            followup,
            material,
            prior_reason="no_new_patch_source",
        )

    raw_evidence = tuple(
        dict(item) for item in followup.evidence_items if isinstance(item, dict)
    )
    new_evidence = tuple(
        item
        for item in followup_record.evidence
        if item.evidence_id not in initial_evidence_ids and item.source_id in new_source_ids
    )
    if not new_evidence:
        return _ab_material_patch_only_merge_result(
            initial,
            followup,
            material,
            prior_reason="no_new_patch_evidence",
            observed_source_urls=new_source_urls,
        )

    next_citation = _ab_next_citation_number(initial)
    citation_by_source_id = {
        source.source_id: next_citation + offset
        for offset, source in enumerate(new_sources)
    }
    base_claims, base_relations = _ab_evidence_backed_base(initial_record)
    existing_claim_ids = {item.claim_id for item in base_claims}
    patch_claims: list[ResearchClaim] = []
    patch_relations: list[ResearchClaimRelation] = []
    patch_items: list[dict[str, Any]] = []
    for evidence in new_evidence:
        raw = _ab_raw_evidence_for_record_evidence(evidence, raw_evidence, source_url_by_id)
        claim_text = _ab_patch_claim_text(raw, evidence)
        if not claim_text:
            continue
        citation_number = citation_by_source_id.get(evidence.source_id, next_citation)
        claim_id = stable_ref("ab_patch_claim", claim_text, evidence.evidence_id)
        if claim_id in existing_claim_ids:
            continue
        existing_claim_ids.add(claim_id)
        claim = ResearchClaim(
            claim_id=claim_id,
            claim_text=claim_text,
            claim_section="evidence",
            citation_numbers=(citation_number,),
            evidence_refs=(evidence.evidence_id,),
            status="evidence_backed",
        )
        relation = ResearchClaimRelation(
            relation_id=stable_ref("relation", "supports", claim_id, evidence.evidence_id),
            relation_kind="supports",
            from_ref=claim_id,
            to_ref=evidence.evidence_id,
            citation_numbers=(citation_number,),
        )
        patch_claims.append(claim)
        patch_relations.append(relation)
        patch_items.append({
            "number": citation_number,
            "claim": claim_text,
            "source_url": source_url_by_id.get(evidence.source_id, ""),
            "title": str(source_payloads.get(evidence.source_id, {}).get("title") or ""),
            "excerpt": evidence.bounded_excerpt,
        })
    if not patch_claims:
        return _ab_material_patch_only_merge_result(
            initial,
            followup,
            material,
            prior_reason="no_new_patch_claim",
            observed_source_urls=new_source_urls,
        )

    patched_record = _ab_rehash_record(
        initial_record,
        answer_status=_ab_patch_answer_status(initial_record.answer_status, bool(patch_claims)),
        sources=(*initial_record.sources, *new_sources),
        evidence=(*initial_record.evidence, *new_evidence),
        claims=(*base_claims, *patch_claims),
        assumptions=initial_record.assumptions,
        relations=(*base_relations, *patch_relations),
        unsupported_claim_count=0,
        stop_reason=followup.stop_reason or initial.stop_reason,
    )
    patched_evidence_payload = _ab_patched_evidence_payload(initial, followup, new_source_urls)
    patched = replace(
        followup,
        summary=_ab_finalizer_patch_summary(initial, patch_items),
        stop_reason=followup.stop_reason or initial.stop_reason,
        evidence_items=patched_evidence_payload,
        research_record=patched_record,
        citation_map=_ab_patched_citation_map(initial, patch_items),
    )
    return PatchOnlyMerge(
        result=patched,
        applied=True,
        reason="patch_only_merge",
        new_source_urls=new_source_urls,
        new_evidence_count=len(new_evidence),
        new_claim_count=len(patch_claims),
    )


def _ab_material_patch_only_merge_result(
    initial: ResearchRunResult,
    followup: ResearchRunResult | None,
    material: PlanExecutionResult | None,
    *,
    prior_reason: str,
    observed_source_urls: tuple[str, ...] = (),
) -> PatchOnlyMerge:
    record = initial.research_record
    if record is None:
        return PatchOnlyMerge(reason=prior_reason)
    if material is None or not material.opened_sources:
        return PatchOnlyMerge(reason=prior_reason, new_source_urls=observed_source_urls)
    existing_urls = _ab_result_url_set(initial)
    next_citation = _ab_next_citation_number(initial)
    new_sources: list[ResearchSource] = []
    new_evidence: list[ResearchEvidence] = []
    patch_claims: list[ResearchClaim] = []
    patch_relations: list[ResearchClaimRelation] = []
    patch_items: list[dict[str, Any]] = []
    seen_source_ids = {item.source_id for item in record.sources}
    seen_evidence_ids = {item.evidence_id for item in record.evidence}
    base_claims, base_relations = _ab_evidence_backed_base(record)
    seen_claim_ids = {item.claim_id for item in base_claims}
    for index, source_payload in enumerate(material.opened_sources):
        if not isinstance(source_payload, dict):
            continue
        final_url = str(source_payload.get("final_url") or source_payload.get("url") or "").strip()
        if not final_url or final_url in existing_urls:
            continue
        title = str(source_payload.get("title") or "").strip() or final_url
        preview = str(material.previews[index] if index < len(material.previews) else "")
        excerpt = _ab_material_excerpt(preview, final_url=final_url, title=title)
        if not excerpt:
            continue
        source = _ab_material_source(source_payload, final_url=final_url, title=title, preview=preview)
        if source.source_id in seen_source_ids:
            continue
        seen_source_ids.add(source.source_id)
        citation_number = next_citation + len(new_sources)
        evidence = _ab_material_evidence(source, excerpt=excerpt, preview=preview)
        if evidence.evidence_id in seen_evidence_ids:
            continue
        seen_evidence_ids.add(evidence.evidence_id)
        claim_text = _clip(excerpt, 240)
        claim_id = stable_ref("ab_patch_claim", claim_text, evidence.evidence_id)
        if claim_id in seen_claim_ids:
            continue
        seen_claim_ids.add(claim_id)
        claim = ResearchClaim(
            claim_id=claim_id,
            claim_text=claim_text,
            claim_section="evidence",
            citation_numbers=(citation_number,),
            evidence_refs=(evidence.evidence_id,),
            status="evidence_backed",
        )
        relation = ResearchClaimRelation(
            relation_id=stable_ref("relation", "supports", claim_id, evidence.evidence_id),
            relation_kind="supports",
            from_ref=claim_id,
            to_ref=evidence.evidence_id,
            citation_numbers=(citation_number,),
        )
        new_sources.append(source)
        new_evidence.append(evidence)
        patch_claims.append(claim)
        patch_relations.append(relation)
        patch_items.append({
            "number": citation_number,
            "claim": claim_text,
            "source_url": final_url,
            "title": title,
            "excerpt": excerpt,
        })
    new_source_urls = tuple(str(item.get("source_url") or "") for item in patch_items)
    if not patch_items:
        return PatchOnlyMerge(
            reason=prior_reason,
            new_source_urls=observed_source_urls,
        )
    base_result = followup or initial
    patched_record = _ab_rehash_record(
        record,
        answer_status=_ab_patch_answer_status(record.answer_status, bool(patch_claims)),
        sources=(*record.sources, *new_sources),
        evidence=(*record.evidence, *new_evidence),
        claims=(*base_claims, *patch_claims),
        assumptions=record.assumptions,
        relations=(*base_relations, *patch_relations),
        unsupported_claim_count=0,
        stop_reason=base_result.stop_reason or initial.stop_reason,
    )
    patched = replace(
        base_result,
        summary=_ab_finalizer_patch_summary(initial, patch_items),
        stop_reason=base_result.stop_reason or initial.stop_reason,
        opened_sources=_ab_patched_opened_sources(initial, material.opened_sources),
        evidence_items=_ab_patched_evidence_payload_from_material(initial, patch_items),
        citation_map=_ab_patched_citation_map(initial, patch_items),
        source_urls=sorted(_ab_result_url_set(initial).union(new_source_urls)),
        sources_read=len(_ab_result_url_set(initial).union(new_source_urls)),
        research_record=patched_record,
    )
    return PatchOnlyMerge(
        result=patched,
        applied=True,
        reason="material_patch_only_merge",
        new_source_urls=tuple(url for url in new_source_urls if url),
        new_evidence_count=len(new_evidence),
        new_claim_count=len(patch_claims),
    )


def _ab_result_url_set(result: ResearchRunResult) -> set[str]:
    urls = {str(url or "").strip() for url in result.source_urls}
    for item in result.citation_map:
        if isinstance(item, dict):
            urls.add(str(item.get("url") or "").strip())
    for item in result.opened_sources:
        if isinstance(item, dict):
            urls.add(str(item.get("final_url") or item.get("url") or "").strip())
    return {url for url in urls if url}


def _ab_material_source(
    source_payload: dict[str, Any],
    *,
    final_url: str,
    title: str,
    preview: str,
) -> ResearchSource:
    requested_url = str(source_payload.get("requested_url") or final_url).strip() or final_url
    final_ref = sanitize_research_url_ref(final_url)
    requested_ref = sanitize_research_url_ref(requested_url)
    content_kind = str(source_payload.get("content_kind") or "html").strip() or "html"
    content_hash = str(source_payload.get("text_hash") or source_payload.get("content_hash") or "").strip()
    if not content_hash:
        content_hash = digest_text(preview)
    return ResearchSource(
        source_id=stable_ref(
            "source",
            final_ref.get("url_digest") or requested_ref.get("url_digest") or final_url,
            content_hash,
            content_kind,
        ),
        requested_url_ref=requested_ref,
        final_url_ref=final_ref,
        host=str(final_ref.get("host") or requested_ref.get("host") or ""),
        title_digest=digest_text(title),
        content_hash=_clip(content_hash, 80),
        retrieved_at=str(source_payload.get("retrieved_at") or _timestamp()),
        content_kind=content_kind,
        page_count=max(0, int(source_payload.get("page_count") or 0)),
        pages_read=tuple(int(page) for page in source_payload.get("pages_read") or () if int(page) > 0),
        truncated=bool(source_payload.get("truncated")),
        quality=dict(source_payload.get("quality") or {"level": "unknown", "kind": "web"}),
    )


def _ab_material_evidence(
    source: ResearchSource,
    *,
    excerpt: str,
    preview: str,
) -> ResearchEvidence:
    start = max(0, preview.find(excerpt))
    end = start + len(excerpt) if excerpt else start
    stance = "supports"
    excerpt_digest = digest_text(excerpt)
    return ResearchEvidence(
        evidence_id=stable_ref("evidence", source.source_id, excerpt_digest, "", stance),
        source_id=source.source_id,
        excerpt_digest=excerpt_digest,
        bounded_excerpt=_clip(excerpt, 360),
        locator=EvidenceLocator(
            kind=source.content_kind or "html",
            source_id=source.source_id,
            char_start=start,
            char_end=end,
            locator="ab.opened_material",
        ),
        stance=stance,
        claim_text_digest=digest_text(_ab_norm(excerpt)),
    )


def _ab_material_excerpt(preview: str, *, final_url: str, title: str) -> str:
    lines = [line.strip() for line in str(preview or "").splitlines() if line.strip()]
    candidates = []
    for line in lines:
        if line.startswith("query:"):
            continue
        if final_url and final_url in line:
            continue
        if title and line == title:
            continue
        candidates.append(line)
    if not candidates:
        return ""
    return _clip(max(candidates, key=len), 320)


def _ab_patched_opened_sources(
    initial: ResearchRunResult,
    opened_sources: tuple[dict, ...],
) -> list[dict[str, Any]]:
    rows = [dict(item) for item in initial.opened_sources if isinstance(item, dict)]
    seen = {
        str(item.get("final_url") or item.get("url") or "").strip()
        for item in rows
    }
    for item in opened_sources:
        if not isinstance(item, dict):
            continue
        url = str(item.get("final_url") or item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append(dict(item))
    return rows


def _ab_patched_evidence_payload_from_material(
    initial: ResearchRunResult,
    patch_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = [dict(item) for item in initial.evidence_items if isinstance(item, dict)]
    seen = {
        (
            str(item.get("source_url") or "").strip(),
            _ab_norm(item.get("claim")),
            _ab_norm(item.get("excerpt")),
        )
        for item in rows
    }
    for item in patch_items:
        row = {
            "claim": str(item.get("claim") or "").strip(),
            "source_url": str(item.get("source_url") or "").strip(),
            "excerpt": str(item.get("excerpt") or "").strip(),
            "stance": "supports",
        }
        sig = (row["source_url"], _ab_norm(row["claim"]), _ab_norm(row["excerpt"]))
        if not row["source_url"] or not row["excerpt"] or sig in seen:
            continue
        seen.add(sig)
        rows.append(row)
    return rows


def _ab_source_payloads_by_id(result: ResearchRunResult) -> dict[str, dict[str, Any]]:
    record = result.research_record
    if record is None:
        return {}
    payloads = tuple(item for item in result.opened_sources if isinstance(item, dict))
    return {
        source.source_id: dict(payload)
        for source, payload in zip(record.sources, payloads, strict=False)
        if source.source_id
    }


def _ab_raw_evidence_for_record_evidence(
    evidence: ResearchEvidence,
    raw_evidence: tuple[dict[str, Any], ...],
    source_url_by_id: dict[str, str],
) -> dict[str, Any]:
    source_url = source_url_by_id.get(evidence.source_id, "")
    excerpt = _ab_norm(evidence.bounded_excerpt)
    for raw in raw_evidence:
        if str(raw.get("source_url") or "").strip() != source_url:
            continue
        if _ab_norm(raw.get("excerpt")) == excerpt:
            return raw
    for raw in raw_evidence:
        if str(raw.get("source_url") or "").strip() == source_url:
            return raw
    return {}


def _ab_patch_claim_text(raw: dict[str, Any], evidence: ResearchEvidence) -> str:
    claim = _clip(raw.get("claim") or evidence.bounded_excerpt, 240)
    return " ".join(claim.split())


def _ab_patch_answer_status(current: object, has_patch_claims: bool) -> str:
    status = str(current or "").strip()
    if status in {"answered", "partial"}:
        return status
    if has_patch_claims and status in {"", "not_answered", "insufficient_evidence"}:
        return "partial"
    return status if status in {"answered", "partial", "insufficient_evidence", "not_answered"} else "partial"


def _ab_evidence_backed_claims(record: ResearchRecord) -> tuple[ResearchClaim, ...]:
    return tuple(
        claim
        for claim in record.claims
        if str(claim.status or "") == "evidence_backed" and tuple(claim.evidence_refs)
    )


def _ab_evidence_backed_base(
    record: ResearchRecord,
) -> tuple[tuple[ResearchClaim, ...], tuple[ResearchClaimRelation, ...]]:
    claims = list(_ab_evidence_backed_claims(record))
    claim_ids = {claim.claim_id for claim in claims}
    covered_evidence = {
        evidence_ref
        for claim in claims
        for evidence_ref in claim.evidence_refs
    }
    citation_by_source_id = {
        source.source_id: index
        for index, source in enumerate(record.sources, 1)
    }
    relations = list(_ab_evidence_backed_relations(record, tuple(claims)))
    relation_ids = {relation.relation_id for relation in relations}
    for evidence in record.evidence:
        if evidence.evidence_id in covered_evidence:
            continue
        claim_text = _clip(evidence.bounded_excerpt, 240)
        if not claim_text:
            continue
        claim_id = stable_ref("ab_base_claim", claim_text, evidence.evidence_id)
        if claim_id in claim_ids:
            continue
        citation_number = citation_by_source_id.get(evidence.source_id, 0)
        claim = ResearchClaim(
            claim_id=claim_id,
            claim_text=claim_text,
            claim_section="evidence",
            citation_numbers=(citation_number,) if citation_number > 0 else (),
            evidence_refs=(evidence.evidence_id,),
            status="evidence_backed",
        )
        relation_id = stable_ref("relation", "supports", claim_id, evidence.evidence_id)
        relation = ResearchClaimRelation(
            relation_id=relation_id,
            relation_kind="supports",
            from_ref=claim_id,
            to_ref=evidence.evidence_id,
            citation_numbers=(citation_number,) if citation_number > 0 else (),
        )
        claims.append(claim)
        claim_ids.add(claim_id)
        covered_evidence.add(evidence.evidence_id)
        if relation_id not in relation_ids:
            relations.append(relation)
            relation_ids.add(relation_id)
    return tuple(claims), tuple(relations)


def _ab_evidence_backed_relations(
    record: ResearchRecord,
    claims: tuple[ResearchClaim, ...],
) -> tuple[ResearchClaimRelation, ...]:
    claim_ids = {claim.claim_id for claim in claims}
    evidence_ids = {evidence.evidence_id for evidence in record.evidence}
    return tuple(
        relation
        for relation in record.relations
        if relation.from_ref in claim_ids and relation.to_ref in evidence_ids
    )


def _ab_rehash_record(
    record: ResearchRecord,
    *,
    answer_status: str,
    sources: tuple[ResearchSource, ...],
    evidence: tuple[ResearchEvidence, ...],
    claims: tuple[ResearchClaim, ...],
    assumptions: tuple[Any, ...],
    relations: tuple[ResearchClaimRelation, ...],
    unsupported_claim_count: int,
    stop_reason: str,
) -> ResearchRecord:
    payload = {
        "schema_version": RESEARCH_RECORD_SCHEMA_VERSION,
        "kind": RESEARCH_RECORD_KIND,
        "question": record.question.to_jsonable(),
        "answer_status": answer_status,
        "sources": [item.to_jsonable() for item in sources],
        "evidence": [item.to_jsonable() for item in evidence],
        "claims": [item.to_jsonable() for item in claims],
        "assumptions": [item.to_jsonable() for item in assumptions],
        "relations": [item.to_jsonable() for item in relations],
        "unsupported_claim_count": max(0, int(unsupported_claim_count or 0)),
        "run_id": _clip(record.run_id, 120),
        "session_id": _clip(record.session_id, 120),
        "project_ref": dict(record.project_ref),
        "synthesis_id": _clip(record.synthesis_id, 120),
        "stop_reason": str(stop_reason or ""),
    }
    digest = digest_json(payload)
    return replace(
        record,
        record_id="research_record:" + digest.removeprefix("sha256:")[:16],
        record_digest=digest,
        answer_status=answer_status,
        sources=sources,
        evidence=evidence,
        claims=claims,
        assumptions=assumptions,
        relations=relations,
        unsupported_claim_count=max(0, int(unsupported_claim_count or 0)),
        stop_reason=str(stop_reason or ""),
    )


def _ab_finalizer_patch_summary(
    initial: ResearchRunResult,
    patch_items: list[dict[str, Any]],
) -> str:
    citation_by_url: dict[str, int] = {}
    source_titles: dict[str, str] = {}
    record = initial.research_record
    source_payloads = _ab_source_payloads_by_id(initial) if record is not None else {}
    if record is not None:
        for index, source in enumerate(record.sources, 1):
            payload = source_payloads.get(source.source_id, {})
            url = str(payload.get("final_url") or payload.get("url") or "").strip()
            if not url:
                continue
            citation_by_url.setdefault(url, index)
            title = str(payload.get("title") or "").strip()
            if title:
                source_titles.setdefault(url, title)
    for item in initial.citation_map:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        try:
            number = int(item.get("number") or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            citation_by_url.setdefault(url, number)
        title = str(item.get("title") or "").strip()
        if title:
            source_titles.setdefault(url, title)
    for item in patch_items:
        url = str(item.get("source_url") or "").strip()
        if not url:
            continue
        try:
            number = int(item.get("number") or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            citation_by_url.setdefault(url, number)
        title = str(item.get("title") or "").strip()
        if title:
            source_titles.setdefault(url, title)

    evidence_lines: list[str] = []
    seen_evidence: set[tuple[str, str]] = set()
    if record is not None:
        source_url_by_id = {
            source_id: str(payload.get("final_url") or payload.get("url") or "").strip()
            for source_id, payload in source_payloads.items()
        }
        for evidence in record.evidence:
            url = source_url_by_id.get(evidence.source_id, "")
            number = citation_by_url.get(url, 0)
            claim = _clip(evidence.bounded_excerpt, 240)
            sig = (url, _ab_norm(claim))
            if number <= 0 or not claim or sig in seen_evidence:
                continue
            seen_evidence.add(sig)
            evidence_lines.append(f"- {claim} [{number}]")
    for item in initial.evidence_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("source_url") or "").strip()
        number = citation_by_url.get(url, 0)
        claim = _clip(item.get("claim") or item.get("excerpt"), 240)
        sig = (url, _ab_norm(claim))
        if number <= 0 or not claim or sig in seen_evidence:
            continue
        seen_evidence.add(sig)
        evidence_lines.append(f"- {claim} [{number}]")
    for item in patch_items:
        url = str(item.get("source_url") or "").strip()
        number = int(item.get("number") or citation_by_url.get(url, 0) or 0)
        claim = _clip(item.get("claim"), 240)
        sig = (url, _ab_norm(claim))
        if number <= 0 or not claim or sig in seen_evidence:
            continue
        seen_evidence.add(sig)
        evidence_lines.append(f"- {claim} [{number}]")

    first_claim = ""
    if evidence_lines:
        first_claim = evidence_lines[0].removeprefix("- ").strip()
    conclusion = (
        f"可证实结论仅限于已写入 evidence 的事项：{first_claim}"
        if first_claim
        else "未形成可引用结论。"
    )
    source_lines = []
    for url, number in sorted(citation_by_url.items(), key=lambda item: item[1]):
        title = source_titles.get(url) or url
        source_lines.append(f"[{number}] {title} - {url}")
    sections = [
        "## 结论",
        conclusion,
        "",
        "## 关键证据",
        *(evidence_lines or ["- 未写入新的可引用 evidence。"]),
        "",
        "## 反证与限制",
        "本 A/B 合并只保留 evidence-backed claim；未由 evidence 支持的新增结论被丢弃。",
        "",
        "## 来源质量",
        "本 A/B 合并只使用已打开并写入 evidence 的 fixture 来源，不推断额外权威性或覆盖范围。",
        "",
        "## 搜索覆盖",
        "结论范围限制为本次已写入 evidence 的来源内容。",
        "",
        "## 来源",
        *(source_lines or ["无可引用来源。"]),
    ]
    return _clip("\n".join(sections), 8000)


def _ab_patched_evidence_payload(
    initial: ResearchRunResult,
    followup: ResearchRunResult,
    new_source_urls: tuple[str, ...],
) -> list[dict[str, Any]]:
    seen = {
        (
            str(item.get("source_url") or "").strip(),
            _ab_norm(item.get("claim")),
            _ab_norm(item.get("excerpt")),
        )
        for item in initial.evidence_items
        if isinstance(item, dict)
    }
    rows = [dict(item) for item in initial.evidence_items if isinstance(item, dict)]
    allowed_urls = set(new_source_urls)
    for item in followup.evidence_items:
        if not isinstance(item, dict):
            continue
        source_url = str(item.get("source_url") or "").strip()
        if source_url not in allowed_urls:
            continue
        sig = (source_url, _ab_norm(item.get("claim")), _ab_norm(item.get("excerpt")))
        if sig in seen:
            continue
        seen.add(sig)
        rows.append(dict(item))
    return rows


def _ab_patched_citation_map(
    initial: ResearchRunResult,
    patch_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = [dict(item) for item in initial.citation_map if isinstance(item, dict)]
    seen_urls = {str(item.get("url") or "").strip() for item in rows}
    next_number = max(
        (
            int(item.get("number") or 0)
            for item in rows
            if not isinstance(item.get("number"), bool)
        ),
        default=0,
    )
    for item in initial.opened_sources:
        if not isinstance(item, dict):
            continue
        url = str(item.get("final_url") or item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        next_number += 1
        seen_urls.add(url)
        rows.append({
            "number": next_number,
            "title": str(item.get("title") or "").strip() or url,
            "url": url,
        })
    for item in patch_items:
        url = str(item.get("source_url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        rows.append({
            "number": int(item.get("number") or len(rows) + 1),
            "title": str(item.get("title") or "").strip() or url,
            "url": url,
        })
    return rows


def _ab_next_citation_number(result: ResearchRunResult) -> int:
    numbers = []
    for item in result.citation_map:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("number") or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            numbers.append(number)
    return max(numbers or [len(result.source_urls)]) + 1


def _ab_norm(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


class TimedProvider:
    def __init__(self, provider, *, send_timeout: float, new_chat_timeout: float) -> None:
        self.provider = provider
        self.send_timeout = max(1.0, float(send_timeout))
        self.new_chat_timeout = max(1.0, float(new_chat_timeout))
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def new_chat(self, timeout=None) -> None:
        effective = self.new_chat_timeout if timeout is None else timeout
        return self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout=None) -> str:
        effective = self.send_timeout if timeout is None else timeout
        return self.provider.send(text, timeout=effective)

    def close(self) -> None:
        return self.provider.close()


class OutputProviderMismatch(ValueError):
    def __init__(self, *, path: Path, expected: str, found: str) -> None:
        self.path = path
        self.expected = expected
        self.found = found
        super().__init__(
            f"{path} was created for provider {found!r}; refusing to reuse it for {expected!r}"
        )


class LiveTrace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.monotonic()
        self.started_at = _timestamp()
        self.events: list[dict[str, Any]] = []
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("probe") != "bounded_research_planner_ab_trace":
            return
        started_at = str(payload.get("started_at") or "").strip()
        events = payload.get("events")
        if started_at:
            self.started_at = started_at
        if isinstance(events, list):
            self.events = [dict(event) for event in events if isinstance(event, dict)]

    def record(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.events.append(payload)
        self.flush()

    def record_run_start(
        self,
        *,
        run_id: str,
        provider: str,
        trace_output: str,
        cases: tuple[str, ...],
        arms: tuple[str, ...],
        max_turns: int,
    ) -> None:
        self.record({
            "event": "run_start",
            "run_id": run_id,
            "provider": provider,
            "trace_output": trace_output,
            "cases": list(cases),
            "arms": list(arms),
            "max_turns": max_turns,
        })

    def record_case_start(
        self,
        *,
        run_id: str,
        provider: str,
        case: str,
        arm: str,
        question: str,
    ) -> None:
        self.record({
            "event": "case_start",
            "run_id": run_id,
            "provider": provider,
            "case": case,
            "arm": arm,
            "question": _clip(question, 1200),
        })

    def record_send_start(
        self,
        *,
        run_id: str,
        provider: str,
        provider_name: str,
        case: str,
        arm: str,
        turn: int,
        prompt: str,
    ) -> None:
        self.record({
            "event": "send_start",
            "run_id": run_id,
            "provider": provider,
            "provider_name": provider_name,
            "case": case,
            "arm": arm,
            "turn": turn,
            "prompt_chars": len(prompt or ""),
            "prompt": _clip(prompt, TRACE_PROMPT_CHARS),
        })

    def record_reply(
        self,
        *,
        run_id: str,
        provider: str,
        provider_name: str,
        case: str,
        arm: str,
        turn: int,
        prompt: str,
        reply: str,
    ) -> None:
        self.record({
            "event": "reply",
            "run_id": run_id,
            "provider": provider,
            "provider_name": provider_name,
            "case": case,
            "arm": arm,
            "turn": turn,
            "prompt_chars": len(prompt or ""),
            "reply_chars": len(reply or ""),
            "prompt": _clip(prompt, TRACE_PROMPT_CHARS),
            "reply": _clip(reply, TRACE_REPLY_CHARS),
        })

    def record_case_complete(
        self,
        *,
        run_id: str,
        provider: str,
        case: str,
        arm: str,
        row: dict[str, Any],
    ) -> None:
        self.record({
            "event": "case_complete",
            "run_id": run_id,
            "provider": provider,
            "case": case,
            "arm": arm,
            "ok": bool(row.get("ok")),
            "score": row.get("score"),
            "stop_reason": row.get("stop_reason") or row.get("error") or "",
            "planner_stop_reason": row.get("planner_stop_reason") or "",
            "followup_rounds": row.get("followup_rounds"),
            "summary": _clip(row.get("summary_preview") or row.get("error") or "", 1200),
        })

    def record_run_complete(self, *, run_id: str, provider: str, rows: int) -> None:
        self.record({
            "event": "run_complete",
            "run_id": run_id,
            "provider": provider,
            "rows": rows,
        })

    def flush(self) -> None:
        payload = {
            "probe": "bounded_research_planner_ab_trace",
            "started_at": self.started_at,
            "updated_at": _timestamp(),
            "updated_elapsed_seconds": round(time.monotonic() - self.started, 3),
            "event_count": len(self.events),
            "events": self.events,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text.encode("utf-8")) > MAX_TRACE_BYTES:
            raise ValueError("bounded planner A/B trace exceeded bounded size")
        _write_json_atomic(self.path, payload)


class TracingProvider:
    def __init__(
        self,
        provider,
        *,
        trace: LiveTrace | None,
        run_id: str,
        provider_id: str,
        provider_name: str,
        case: str,
        arm: str,
    ) -> None:
        self.provider = provider
        self.trace = trace
        self.run_id = run_id
        self.provider_id = provider_id
        self.provider_name = provider_name
        self.case = case
        self.arm = arm
        self.send_index = 0
        self.reply_count = 0
        self.prompt_chars = 0
        self.reply_chars = 0
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")
        self.thread_safe_send = getattr(provider, "thread_safe_send", False)

    def __getattr__(self, name: str):
        return getattr(self.provider, name)

    def new_chat(self, timeout=None) -> None:
        return self.provider.new_chat(timeout=timeout)

    def send(self, text: str, timeout=None) -> str:
        self.send_index += 1
        turn = self.send_index
        self.prompt_chars += len(text or "")
        if self.trace is not None:
            self.trace.record_send_start(
                run_id=self.run_id,
                provider=self.provider_id,
                provider_name=self.provider_name,
                case=self.case,
                arm=self.arm,
                turn=turn,
                prompt=text,
            )
        try:
            reply = self.provider.send(text, timeout=timeout)
        except Exception as exc:
            if self.trace is not None:
                self.trace.record({
                    "event": "send_error",
                    "run_id": self.run_id,
                    "provider": self.provider_id,
                    "provider_name": self.provider_name,
                    "case": self.case,
                    "arm": self.arm,
                    "turn": turn,
                    "error": f"{type(exc).__name__}: {exc}",
                })
            raise
        reply_text = str(reply or "")
        self.reply_count += 1
        self.reply_chars += len(reply_text)
        if self.trace is not None:
            self.trace.record_reply(
                run_id=self.run_id,
                provider=self.provider_id,
                provider_name=self.provider_name,
                case=self.case,
                arm=self.arm,
                turn=turn,
                prompt=text,
                reply=reply_text,
            )
        return reply

    def close(self) -> None:
        return self.provider.close()


def run_case(
    provider,
    *,
    provider_id: str,
    case: Case,
    arm: str,
    max_turns: int,
    run_id: str,
    trace: LiveTrace | None,
) -> dict[str, Any]:
    started = time.time()
    model_actions: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    infos: list[str] = []
    provider_name = str(getattr(provider, "name", "") or "")
    with tempfile.TemporaryDirectory(prefix="codey-bounded-planner-ab-") as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        evidence_ledgers = EvidenceLedgerStore(root / "state")
        search = FixtureSearchProvider(case)
        iterations: list[ResearchIterationRun] = []
        if trace is not None:
            trace.record_case_start(
                run_id=run_id,
                provider=provider_id,
                case=case.name,
                arm=arm,
                question=case.question,
            )
        run_provider = TracingProvider(
            provider,
            trace=trace,
            run_id=run_id,
            provider_id=provider_id,
            provider_name=provider_name,
            case=case.name,
            arm=arm,
        )

        def run_iteration(
            *,
            task: str,
            max_turns: int,
            chat_handoff: str,
            search: object,
            tools=None,
            iteration_context: str = "",
        ) -> ResearchIterationRun:
            evidence_only_followup = _ab_is_evidence_only_followup(arm, iteration_context)
            effective_max_turns = 1 if evidence_only_followup else max_turns
            runner = ResearchRunner(
                run_provider,
                search,
                store,
                max_turns=effective_max_turns,
                session_id=f"bounded-planner-ab-{provider_id}-{case.name}-{arm}",
                project="",
                run_id=run_id,
                chat_handoff=chat_handoff,
                tools=tools,
                iteration_context=iteration_context,
            )
            if evidence_only_followup:
                runner.controller = ABEvidenceOnlyFollowupController(
                    required_urls=_ab_followup_final_urls(iteration_context),
                )
            for event in runner.run(task):
                if event.kind == "turn":
                    model_actions.extend(_safe_model_actions(event.turn, event.reply)[:3])
                if event.kind == "info":
                    infos.append(str(event.message or "")[:240])
                if event.kind == "tool" and event.call is not None:
                    args = event.call.args if isinstance(event.call.args, dict) else {}
                    tool_calls.append({
                        "turn": event.turn,
                        "name": event.call.name,
                        "args": _safe_args(args),
                        "ok": bool(event.outcome.ok) if event.outcome is not None else False,
                        "status": event.outcome.presentation_status() if event.outcome is not None else "",
                    })
            if runner.result is None:
                raise RuntimeError("research finished without result")
            iteration = ResearchIterationRun(result=runner.result, tools=runner.tools)
            iterations.append(iteration)
            return iteration

        try:
            context = ResearchContext(
                question=case.question,
                session_id=f"bounded-planner-ab-{provider_id}-{case.name}-{arm}",
                run_id=run_id,
                project="",
                provider_id=provider_id,
                max_turns=max_turns,
            )
            pipeline = ResearchPipeline(
                context=context,
                run_iteration=run_iteration,
                search_factory=lambda: search,
                evidence_ledgers=evidence_ledgers,
                config=_config_for_arm(arm),
            )
            materials: list[PlanExecutionResult] = []
            pipeline_result = _run_pipeline_with_ab_experiment(
                pipeline,
                arm=arm,
                materials=materials,
            )
            patch_merge = None
            result = pipeline_result.final_result
            if arm == "planner":
                patch_merge = _ab_patch_only_merge_result(
                    iterations[0].result if iterations else None,
                    iterations[-1].result if iterations else None,
                    materials[-1] if materials else None,
                )
                if patch_merge.applied and patch_merge.result is not None:
                    result = patch_merge.result
            proof = (
                review_research_proof(result.research_record, question=case.question)
                if result.research_record is not None
                else None
            )
            record_counts = _record_counts(result.research_record)
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "ok": True,
                "seconds": round(time.time() - started, 3),
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "max_turns_used": int(getattr(result, "max_turns_used", 0) or 0),
                "sources_read": result.sources_read,
                "opened_urls": result.source_urls[:12],
                "evidence_count": len(result.evidence_items),
                "record_source_count": record_counts["source_count"],
                "record_evidence_count": record_counts["evidence_count"],
                "record_claim_count": record_counts["claim_count"],
                "unsupported_claim_count": record_counts["unsupported_claim_count"],
                "unsupported_claim_rate": _ratio(
                    record_counts["unsupported_claim_count"],
                    record_counts["claim_count"],
                ),
                "proof_ok": bool(proof.ok) if proof is not None else False,
                "proof_answer_status": proof.answer_status if proof is not None else "",
                "proof_coverage": proof.answer_coverage_score if proof is not None else None,
                "citation_locator_verified": (
                    bool(proof.citation_locator_verified) if proof is not None else False
                ),
                "support_relation_verified": (
                    bool(proof.support_relation_verified) if proof is not None else False
                ),
                "counterevidence_checked": (
                    bool(proof.counterevidence_checked) if proof is not None else False
                ),
                "proof_missing_evidence": list(proof.missing_evidence[:8]) if proof is not None else [],
                "expected_terms_present": _expected_terms_present(result.summary, case.expected_terms),
                "followup_applied": pipeline_result.followup_applied,
                "followup_rounds": pipeline_result.followup_rounds,
                "pipeline_stop_reason": pipeline_result.stop_reason,
                "planner_stop_reason": pipeline_result.planner_stop_reason,
                "ab_followup_mode": (
                    AB_EVIDENCE_ONLY_FOLLOWUP_MODE
                    if arm == "planner" and pipeline_result.followup_rounds
                    else ""
                ),
                "ab_followup_max_turns": (
                    1 if arm == "planner" and pipeline_result.followup_rounds else 0
                ),
                **(patch_merge.row_payload() if patch_merge is not None else {}),
                "fixture_queries": search.queries[:12],
                "fixture_fetches": search.fetches[:12],
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "provider_prompt_chars": run_provider.prompt_chars,
                "provider_reply_chars": run_provider.reply_chars,
                "model_actions": model_actions[:60],
                "tool_calls": tool_calls[:60],
                "info": infos[:16],
                "summary_chars": len(result.summary or ""),
                "summary_preview": _clip(result.summary, 1600),
            }
            if patch_merge is not None:
                row["ab_patch_merge_applied"] = bool(patch_merge.applied)
                row["ab_patch_merge_reason"] = patch_merge.reason
                row["ab_patch_merge_new_source_urls"] = list(patch_merge.new_source_urls[:8])
                row["ab_patch_merge_new_evidence_count"] = int(patch_merge.new_evidence_count or 0)
                row["ab_patch_merge_new_claim_count"] = int(patch_merge.new_claim_count or 0)
            row["score"] = _score_row(row)
            if trace is not None:
                trace.record_case_complete(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    row=row,
                )
            return row
        except Exception as exc:
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "ok": False,
                "seconds": round(time.time() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "fixture_queries": search.queries[:12],
                "fixture_fetches": search.fetches[:12],
                "provider_send_count": run_provider.send_index,
                "provider_reply_count": run_provider.reply_count,
                "provider_prompt_chars": run_provider.prompt_chars,
                "provider_reply_chars": run_provider.reply_chars,
                "model_actions": model_actions[:60],
                "tool_calls": tool_calls[:60],
                "info": infos[:16],
            }
            if trace is not None:
                trace.record_case_complete(
                    run_id=run_id,
                    provider=provider_id,
                    case=case.name,
                    arm=arm,
                    row=row,
                )
            return row
        finally:
            store.close()


def _config_for_arm(arm: str) -> ResearchPipelineConfig:
    if arm == "baseline":
        return ResearchPipelineConfig(enabled=False, max_followup_rounds=0)
    return ResearchPipelineConfig(
        enabled=True,
        max_followup_rounds=1,
        max_queries_per_round=3,
        max_sources_per_query=2,
        max_total_sources=6,
    )


def _run_pipeline_with_ab_experiment(
    pipeline: ResearchPipeline,
    *,
    arm: str,
    materials: list[PlanExecutionResult] | None = None,
):
    if arm != "planner":
        return pipeline.run()
    from codey.research import pipeline as pipeline_module

    original_executor = pipeline_module.PlanExecutor
    original_has_actionable_gap = pipeline_module._has_actionable_gap
    original_pipeline_stop_reason = pipeline_module._pipeline_stop_reason
    original_is_followup_eligible_stop = pipeline_module._is_followup_eligible_stop

    class _FreshMaterialExecutor(FreshMaterialPlanExecutor):
        def __init__(self, *, config=None, should_stop=None) -> None:
            super().__init__(
                config=config or ResearchPipelineConfig(),
                should_stop=should_stop or (lambda: False),
            )

        def execute(self, plan: ResearchPlan, tools: ResearchTools) -> PlanExecutionResult:
            material = super().execute(plan, tools)
            if materials is not None:
                materials.append(material)
            return material

    def ab_pipeline_stop_reason(plan, review=None):
        if _ab_has_actionable_gap(review) and not _ab_needs_new_material(review):
            return "no_new_material_needed"
        return original_pipeline_stop_reason(plan, review)

    try:
        pipeline_module.PlanExecutor = _FreshMaterialExecutor
        pipeline_module._has_actionable_gap = _ab_needs_new_material
        pipeline_module._pipeline_stop_reason = ab_pipeline_stop_reason
        pipeline_module._is_followup_eligible_stop = lambda stop_reason: str(stop_reason or "") in {
            "done",
            "max_turns",
            "no_progress",
            "protocol",
        }
        return pipeline.run()
    finally:
        pipeline_module.PlanExecutor = original_executor
        pipeline_module._has_actionable_gap = original_has_actionable_gap
        pipeline_module._pipeline_stop_reason = original_pipeline_stop_reason
        pipeline_module._is_followup_eligible_stop = original_is_followup_eligible_stop


def _ab_has_actionable_gap(review: object | None) -> bool:
    if review is None or bool(getattr(review, "ok", False)):
        return False
    if _ab_needs_new_material(review):
        return True
    missing = set(getattr(review, "missing_evidence", ()) or ())
    return bool(
        missing.intersection({
            "assumption_used_as_answer",
            "claim_missing_citation",
            "claim_missing_evidence_ref",
            "claim_missing_support_relation",
            "claim_not_evidence_backed",
            "support_relation_bad_locator",
            "support_relation_missing_evidence",
            "unsupported_claims",
        })
        or getattr(review, "overclaim_warnings", ())
    )


def _ab_needs_new_material(review: object | None) -> bool:
    if review is None or bool(getattr(review, "ok", False)):
        return False
    answer_status = str(getattr(review, "answer_status", "") or "")
    if answer_status in {"not_answered", "insufficient_evidence"}:
        return True
    missing = set(getattr(review, "missing_evidence", ()) or ())
    return bool(
        getattr(review, "coverage_gaps", ())
        or getattr(review, "followup_questions", ())
        or getattr(review, "query_rewrite_candidates", ())
        or missing.intersection({
            "answer_coverage_gap",
            "counterevidence_not_checked",
            "partial_answer",
        })
        or getattr(review, "source_trust_warnings", ())
    )


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"query", "url", "source_id", "result_id", "hit_id", "pages", "type", "title"}:
            safe[str(key)] = _clip(value, 240)
        elif key in {"offset", "limit"}:
            safe[str(key)] = value
    return safe


def _safe_model_actions(turn: int, reply: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for obj in extract_json_objects(reply or ""):
        tool = str(obj.get("tool") or "").strip().lower()
        args = obj.get("args")
        if not isinstance(args, dict):
            args = {}
        actions.append({
            "turn": int(turn or 0),
            "tool": _clip(tool, 80),
            "args": _safe_args(args),
        })
    if not actions and str(reply or "").strip():
        actions.append({"turn": int(turn or 0), "tool": "<no_json>", "args": {}})
    return actions


def _expected_terms_present(summary: str, expected_terms: tuple[str, ...]) -> bool:
    text = str(summary or "").casefold()
    return all(term.casefold() in text for term in expected_terms)


def _record_counts(record: object) -> dict[str, int]:
    payload: dict[str, object] = {}
    to_jsonable = getattr(record, "to_jsonable", None)
    if callable(to_jsonable):
        try:
            raw = to_jsonable()
            if isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
    elif isinstance(record, dict):
        payload = record
    return {
        "source_count": _count_from_payload(payload, "source_count", "sources"),
        "evidence_count": _count_from_payload(payload, "evidence_count", "evidence"),
        "claim_count": _count_from_payload(payload, "claim_count", "claims"),
        "unsupported_claim_count": _count_from_payload(
            payload,
            "unsupported_claim_count",
            "unsupported_claims",
        ),
    }


def _count_from_payload(payload: dict[str, object], count_key: str, list_key: str) -> int:
    raw_count = payload.get(count_key)
    if not isinstance(raw_count, bool):
        try:
            return max(0, int(raw_count))
        except (TypeError, ValueError):
            pass
    raw_list = payload.get(list_key)
    if isinstance(raw_list, (list, tuple)):
        return len(raw_list)
    return 0


def _ratio(numerator: object, denominator: object) -> float:
    den = _float(denominator)
    if den <= 0:
        return 0.0
    return round(max(0.0, _float(numerator)) / den, 3)


def _float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _score_row(row: dict[str, Any]) -> int:
    status = str(row.get("proof_answer_status") or "")
    status_score = {
        "answered": 4,
        "partial": 2,
        "insufficient_evidence": 1,
        "not_answered": 0,
    }.get(status, 0)
    return (
        (4 if row.get("proof_ok") else 0)
        + status_score
        + min(3, int(row.get("evidence_count") or 0))
        + (2 if row.get("expected_terms_present") else 0)
    )


def _answer_status_rank(status: object) -> int:
    return {
        "answered": 3,
        "partial": 2,
        "insufficient_evidence": 1,
        "not_answered": 0,
    }.get(str(status or ""), 0)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"rows": len(rows), "by_case": {}}
    for case in sorted({str(row.get("case") or "") for row in rows if row.get("case")}):
        case_rows = [row for row in rows if row.get("case") == case]
        arms = {str(row.get("arm") or ""): row for row in case_rows}
        baseline = arms.get("baseline", {})
        planner = arms.get("planner", {})
        usefulness = _followup_usefulness(baseline, planner)
        summary["by_case"][case] = {
            "baseline_score": baseline.get("score"),
            "planner_score": planner.get("score"),
            "delta": (
                int(planner.get("score") or 0) - int(baseline.get("score") or 0)
                if baseline and planner
                else None
            ),
            "planner_followup_rounds": planner.get("followup_rounds"),
            "planner_stop_reason": planner.get("planner_stop_reason"),
            "baseline_answer_status": baseline.get("proof_answer_status"),
            "planner_answer_status": planner.get("proof_answer_status"),
            "followup_usefulness": usefulness,
        }
    return summary


def _followup_usefulness(
    baseline: dict[str, Any],
    planner: dict[str, Any],
) -> dict[str, Any]:
    if not baseline or not planner:
        return {"evaluated": False}
    baseline_ok = bool(baseline.get("ok"))
    planner_ok = bool(planner.get("ok"))
    if not baseline_ok or not planner_ok:
        return {
            "evaluated": False,
            "reason": "row_not_ok",
            "baseline_ok": baseline_ok,
            "planner_ok": planner_ok,
        }
    coverage_delta = round(
        _float(planner.get("proof_coverage")) - _float(baseline.get("proof_coverage")),
        3,
    )
    unsupported_rate_delta = round(
        _float(planner.get("unsupported_claim_rate")) - _float(baseline.get("unsupported_claim_rate")),
        3,
    )
    score_delta = int(planner.get("score") or 0) - int(baseline.get("score") or 0)
    source_delta = int(planner.get("record_source_count") or 0) - int(baseline.get("record_source_count") or 0)
    evidence_delta = int(planner.get("record_evidence_count") or 0) - int(baseline.get("record_evidence_count") or 0)
    query_delta = len(planner.get("fixture_queries") or ()) - len(baseline.get("fixture_queries") or ())
    fetch_delta = len(planner.get("fixture_fetches") or ()) - len(baseline.get("fixture_fetches") or ())
    baseline_fetch_urls = {str(item or "") for item in baseline.get("fixture_fetches") or ()}
    planner_fetch_urls = {str(item or "") for item in planner.get("fixture_fetches") or ()}
    new_fetched_urls = tuple(sorted(url for url in planner_fetch_urls - baseline_fetch_urls if url))
    send_delta = int(planner.get("provider_send_count") or 0) - int(baseline.get("provider_send_count") or 0)
    seconds_delta = round(_float(planner.get("seconds")) - _float(baseline.get("seconds")), 3)
    answer_status_delta = _answer_status_rank(planner.get("proof_answer_status")) - _answer_status_rank(
        baseline.get("proof_answer_status")
    )
    reasons: list[str] = []
    quality_reasons: list[str] = []
    quality_regressions: list[str] = []
    if coverage_delta >= 0.05:
        reasons.append("coverage_improved")
        quality_reasons.append("coverage_improved")
    elif coverage_delta <= -0.05:
        quality_regressions.append("coverage_regressed")
    if unsupported_rate_delta <= -0.02:
        reasons.append("unsupported_rate_improved")
        quality_reasons.append("unsupported_rate_improved")
    elif unsupported_rate_delta >= 0.02:
        quality_regressions.append("unsupported_rate_regressed")
    if evidence_delta > 0:
        reasons.append("new_evidence")
    if source_delta > 0:
        reasons.append("new_sources")
    if new_fetched_urls:
        reasons.append("new_fetched_sources")
    if answer_status_delta > 0:
        reasons.append("answer_status_improved")
        quality_reasons.append("answer_status_improved")
    elif answer_status_delta < 0:
        quality_regressions.append("answer_status_regressed")
    if planner.get("proof_ok") and not baseline.get("proof_ok"):
        reasons.append("proof_ok_recovered")
        quality_reasons.append("proof_ok_recovered")
    elif baseline.get("proof_ok") and not planner.get("proof_ok"):
        quality_regressions.append("proof_ok_regressed")
    if score_delta > 0:
        reasons.append("score_improved")
    elif score_delta < 0:
        quality_regressions.append("score_regressed")
    if planner.get("expected_terms_present") and not baseline.get("expected_terms_present"):
        reasons.append("expected_terms_recovered")
        quality_reasons.append("expected_terms_recovered")
    elif baseline.get("expected_terms_present") and not planner.get("expected_terms_present"):
        quality_regressions.append("expected_terms_lost")
    material_gain = bool(source_delta > 0 or evidence_delta > 0)
    execution_material_gain = bool(new_fetched_urls)
    quality_gain = bool(quality_reasons)
    quality_regression = bool(quality_regressions)
    useful = bool(
        int(planner.get("followup_rounds") or 0) > 0
        and material_gain
        and quality_gain
        and not quality_regression
    )
    return {
        "evaluated": True,
        "useful": useful,
        "material_gain": material_gain,
        "execution_material_gain": execution_material_gain,
        "quality_gain": quality_gain,
        "quality_regression": quality_regression,
        "reason_codes": reasons,
        "quality_regression_codes": quality_regressions,
        "followup_rounds": int(planner.get("followup_rounds") or 0),
        "new_sources": max(0, source_delta),
        "new_evidence": max(0, evidence_delta),
        "new_fetched_sources": len(new_fetched_urls),
        "new_fetched_source_urls": [_clip(url, 160) for url in new_fetched_urls[:6]],
        "answer_coverage_delta": coverage_delta,
        "unsupported_claim_rate_delta": unsupported_rate_delta,
        "answer_status_delta": answer_status_delta,
        "score_delta": score_delta,
        "query_delta": query_delta,
        "fetch_delta": fetch_delta,
        "provider_send_delta": send_delta,
        "seconds_delta": seconds_delta,
    }


def run_provider(
    provider_id: str,
    *,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
    port: int,
    output: Path,
    max_turns: int,
    send_timeout: float,
    new_chat_timeout: float,
    open_if_missing: bool,
    rerun_failed: bool,
    trace: LiveTrace | None,
    run_id: str,
) -> dict[str, Any]:
    payload = _load_or_new_payload(output, provider_id=provider_id, cases=cases, arms=arms)
    _normalize_payload_metadata(payload, provider_id=provider_id, cases=cases, arms=arms)
    if trace is not None:
        payload["trace_output"] = str(trace.path)
    existing = {
        (str(row.get("case") or ""), str(row.get("arm") or ""))
        for row in payload["rows"]
        if row.get("ok") or not rerun_failed
    }
    pending = _pending_case_keys(cases=cases, arms=arms, existing=existing)
    if trace is not None:
        trace.record_run_start(
            run_id=run_id,
            provider=provider_id,
            trace_output=str(trace.path),
            cases=tuple(case.name for case in cases),
            arms=arms,
            max_turns=max_turns,
        )
    if not pending:
        if trace is not None:
            trace.record({
                "event": "no_pending_rows",
                "run_id": run_id,
                "provider": provider_id,
                "output": str(output),
                "cases": [case.name for case in cases],
                "arms": list(arms),
                "rerun_failed": bool(rerun_failed),
                "existing_rows": len(payload["rows"]),
            })
            trace.record_run_complete(run_id=run_id, provider=provider_id, rows=len(payload["rows"]))
        payload["complete"] = True
        payload["summary"] = summarize(payload["rows"])
        payload["updated_at"] = _timestamp()
        _write_payload(output, payload)
        print(
            f"[{provider_id}] no pending rows for cases={','.join(case.name for case in cases)} "
            f"arms={','.join(arms)}; use --rerun-failed or a new --output to run again.",
            flush=True,
        )
        return payload
    _write_payload(output, payload)
    provider_controls.begin_task_context(f"bounded-research-planner-ab:{provider_id}")
    provider = None
    try:
        with _fixture_url_policy_bypass():
            provider = TimedProvider(
                connect_provider(
                    provider_id,
                    port=port,
                    open_if_missing=open_if_missing,
                    bring_to_front=open_if_missing,
                ),
                send_timeout=send_timeout,
                new_chat_timeout=new_chat_timeout,
            )
            for case in cases:
                for arm in arms:
                    key = (case.name, arm)
                    if key in existing:
                        continue
                    row = run_case(
                        provider,
                        provider_id=provider_id,
                        case=case,
                        arm=arm,
                        max_turns=max_turns,
                        run_id=run_id,
                        trace=trace,
                    )
                    payload["rows"].append(row)
                    payload["summary"] = summarize(payload["rows"])
                    payload["updated_at"] = _timestamp()
                    _write_payload(output, payload)
                    print(
                        f"[{provider_id} {case.name} {arm}] "
                        f"ok={row.get('ok')} score={row.get('score')} "
                        f"followup={row.get('followup_rounds')} "
                        f"stop={row.get('planner_stop_reason') or row.get('stop_reason', row.get('error', ''))}",
                        flush=True,
                    )
            payload["complete"] = True
            payload["summary"] = summarize(payload["rows"])
            payload["updated_at"] = _timestamp()
            _write_payload(output, payload)
            if trace is not None:
                trace.record_run_complete(
                    run_id=run_id,
                    provider=provider_id,
                    rows=len(payload["rows"]),
                )
            return payload
    finally:
        provider_controls.end_task_context()
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass


def _load_or_new_payload(
    output: Path,
    *,
    provider_id: str,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
) -> dict[str, Any]:
    if output.exists():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
                _ensure_payload_provider(payload, provider_id=provider_id, output=output)
                payload["complete"] = False
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "probe": "bounded_research_planner_ab",
        "provider": provider_id,
        "started_at": _timestamp(),
        "updated_at": _timestamp(),
        "trace_output": "",
        "cases": [case.name for case in cases],
        "arms": list(arms),
        "complete": False,
        "rows": [],
        "summary": {},
    }


def _normalize_payload_metadata(
    payload: dict[str, Any],
    *,
    provider_id: str,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
) -> None:
    payload["provider"] = provider_id
    payload["cases"] = _merge_unique_names(payload.get("cases"), [case.name for case in cases])
    payload["arms"] = _merge_unique_names(payload.get("arms"), list(arms))


def _ensure_payload_provider(payload: dict[str, Any], *, provider_id: str, output: Path) -> None:
    found = str(payload.get("provider") or "").strip().lower()
    expected = str(provider_id or "").strip().lower()
    if found and expected and found != expected:
        raise OutputProviderMismatch(path=output, expected=expected, found=found)


def _pending_case_keys(
    *,
    cases: tuple[Case, ...],
    arms: tuple[str, ...],
    existing: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    return [
        (case.name, arm)
        for case in cases
        for arm in arms
        if (case.name, arm) not in existing
    ]


def _merge_unique_names(*values: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            candidates = (value,)
        else:
            try:
                candidates = tuple(value)  # type: ignore[arg-type]
            except TypeError:
                candidates = (value,)
        for candidate in candidates:
            name = str(candidate or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(name)
    return merged


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise ValueError("bounded planner A/B result exceeded bounded size")
    _write_json_atomic(path, payload)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _default_output(provider_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return RESULTS_DIR / f"bounded_research_planner_ab-{provider_id}-{stamp}.json"


def _trace_output_path(output: Path) -> Path:
    if output.suffix:
        return output.with_name(f"{output.stem}.trace{output.suffix}")
    return output.with_name(f"{output.name}.trace.json")


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= 3:
        return text[:limit]
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _parse_names(raw_values: list[str] | None, allowed: Any, label: str) -> tuple[str, ...]:
    if not raw_values:
        return tuple(allowed)
    names: list[str] = []
    for raw in raw_values:
        for part in str(raw or "").split(","):
            name = part.strip()
            if name:
                names.append(name)
    unknown = [name for name in names if name not in allowed]
    if unknown:
        raise SystemExit(f"unknown {label}: {', '.join(unknown)}")
    return tuple(dict.fromkeys(names))


def _self_test() -> None:
    rows = [
        {
            "case": "warehouse_gap",
            "arm": "baseline",
            "ok": True,
            "score": 3,
            "proof_answer_status": "partial",
            "proof_coverage": 0.62,
            "unsupported_claim_rate": 0.14,
            "record_source_count": 1,
            "record_evidence_count": 1,
            "fixture_queries": ["benefit"],
            "fixture_fetches": ["https://www.nrel.gov/docs/lithium-storage-benefit"],
            "provider_send_count": 4,
            "seconds": 12.0,
        },
        {
            "case": "warehouse_gap",
            "arm": "planner",
            "ok": True,
            "score": 8,
            "proof_answer_status": "answered",
            "proof_coverage": 0.91,
            "unsupported_claim_rate": 0.08,
            "record_source_count": 2,
            "record_evidence_count": 3,
            "fixture_queries": ["benefit", "limitation"],
            "fixture_fetches": [
                "https://www.nrel.gov/docs/lithium-storage-benefit",
                "https://www.nrel.gov/docs/lithium-storage-limit",
            ],
            "provider_send_count": 6,
            "seconds": 20.0,
            "followup_rounds": 1,
            "planner_stop_reason": "max_followup_rounds",
        },
    ]
    summary = summarize(rows)
    assert summary["by_case"]["warehouse_gap"]["delta"] == 5
    usefulness = summary["by_case"]["warehouse_gap"]["followup_usefulness"]
    assert usefulness["useful"] is True
    assert usefulness["material_gain"] is True
    assert usefulness["execution_material_gain"] is True
    assert usefulness["quality_gain"] is True
    assert usefulness["quality_regression"] is False
    assert usefulness["new_sources"] == 1
    assert usefulness["new_evidence"] == 2
    assert usefulness["new_fetched_sources"] == 1
    assert usefulness["answer_coverage_delta"] == 0.29
    assert usefulness["unsupported_claim_rate_delta"] == -0.06
    assert usefulness["provider_send_delta"] == 2
    assert _config_for_arm("baseline").enabled is False
    assert _config_for_arm("planner").enabled is True
    fixture = FixtureSearchProvider(CASES["widget_noop"])
    assert [item["url"] for item in fixture.search("current primary source evidence")] == [
        "https://source-a.test/widget-storage"
    ]
    with _fixture_material_phase(fixture):
        assert [item["url"] for item in fixture.search("current primary source evidence")] == [
            "https://source-b.test/widget-storage-update",
            "https://source-a.test/widget-storage",
        ]
    material = PlanExecutionResult(
        queries_executed=("current primary source evidence",),
        opened_sources=(
            {
                "final_url": "https://source-b.test/widget-storage-update",
                "title": "Benchmark source B",
            },
        ),
        previews=(
            "query: current primary source evidence\n"
            "Benchmark source B | https://source-b.test/widget-storage-update\n"
            "The Widget Storage working group has not adopted a stable-v3 successor; stable-v2 remains the recommended endpoint.",
        ),
        stop_reason="opened_sources",
    )
    prompt = _ab_followup_context(
        question=CASES["widget_noop"].question,
        initial=type("_Initial", (), {"stop_reason": "done", "summary": "initial summary"})(),
        plan=ResearchPlan(
            plan_ref="research_plan:" + "d" * 16,
            query_candidates=(
                QueryCandidate("research_query:" + "d" * 16, "current primary source evidence"),
            ),
            max_queries=1,
            max_sources=2,
        ),
        material=material,
        limit=12000,
    )
    assert "opened_material.1.final_url: https://source-b.test/widget-storage-update" in prompt
    assert '"sources":["https://source-b.test/widget-storage-update"]' in prompt
    assert '"source_url":"https://source-b.test/widget-storage-update"' in prompt
    assert "Do not put s1, s2, source_id, result_id, or hit_id" in prompt
    assert "Do not call done and do not write a final report" in prompt
    assert "This is an evidence-only repair pass, not a second full research rewrite" in prompt
    assert "The only allowed model output is one knowledge_write call" in prompt
    assert "The deterministic merge will add the new evidence-backed claim and source line" in prompt
    assert "Do not say sources are independent, official, current, or comprehensive" in prompt
    assert "benchmark locators only" in prompt
    controller = ABEvidenceOnlyFollowupController(
        required_urls=("https://source-b.test/widget-storage-update",),
    )
    state = ResearchControlState(
        allowed_tools=("web_search", "open_result", "knowledge_write", "done"),
        source_urls={"s1": "https://source-a.test/widget-storage", "s2": "https://source-b.test/widget-storage-update"},
        source_lines=(
            "s1: Benchmark source A - https://source-a.test/widget-storage",
            "s2: Benchmark source B - https://source-b.test/widget-storage-update",
        ),
        noncitable_source_lines=(
            "s2: Benchmark source B - https://source-b.test/widget-storage-update",
        ),
        evidence_count=1,
        note_count=1,
    )
    block = controller.append_block("followup", state)
    assert "Allowed tools this turn: knowledge_write" in block
    assert "Forbidden tools this turn: done" in block
    assert "Do not call done" in block
    restricted_state = controller.build_state(
        type("_Tools", (), {"ledger": type("_Ledger", (), {
            "searches": (),
            "opened_sources": (),
            "evidence_items": (),
        })(), "created_ids": (), "updated_ids": (), "grounded_ids": ()})(),
        turn=1,
        max_turns=1,
    )
    assert restricted_state.allowed_tools == ("knowledge_write",)
    failed_usefulness = _followup_usefulness(
        {"arm": "baseline", "ok": False, "error": "timeout"},
        {
            "arm": "planner",
            "ok": True,
            "followup_rounds": 1,
            "score": 8,
            "proof_coverage": 0.9,
            "record_source_count": 2,
            "record_evidence_count": 2,
        },
    )
    assert failed_usefulness["evaluated"] is False
    assert failed_usefulness["reason"] == "row_not_ok"
    material_only = _followup_usefulness(
        {
            "arm": "baseline",
            "ok": True,
            "score": 8,
            "proof_answer_status": "answered",
            "proof_coverage": 0.9,
            "unsupported_claim_rate": 0.0,
            "record_source_count": 1,
            "record_evidence_count": 1,
        },
        {
            "arm": "planner",
            "ok": True,
            "score": 7,
            "proof_answer_status": "partial",
            "proof_coverage": 0.8,
            "unsupported_claim_rate": 0.1,
            "record_source_count": 2,
            "record_evidence_count": 2,
            "followup_rounds": 1,
        },
    )
    assert material_only["material_gain"] is True
    assert material_only["quality_regression"] is True
    assert material_only["useful"] is False
    initial_record = ResearchRecord(
        record_id="research_record:1111111111111111",
        record_digest=digest_text("initial-record"),
        question=ResearchQuestion(
            question_id="question:widget",
            question_text_digest=digest_text("Widget Storage API recommendation"),
            chars=34,
        ),
        answer_status="partial",
        sources=(
            ResearchSource(
                source_id="source:1111111111111111",
                requested_url_ref={"host": "source-a.test"},
                final_url_ref={"host": "source-a.test"},
                host="source-a.test",
                title_digest=digest_text("Benchmark source A"),
                content_hash="hash-a",
                retrieved_at="2026-08-20T00:00:00Z",
                content_kind="html",
                quality={"level": "primary", "kind": "web", "freshness": "recent"},
            ),
        ),
        evidence=(
            ResearchEvidence(
                evidence_id="evidence:1111111111111111",
                source_id="source:1111111111111111",
                excerpt_digest=digest_text("stable-v2 remains recommended"),
                bounded_excerpt="stable-v2 remains recommended",
                locator=EvidenceLocator(
                    kind="html",
                    source_id="source:1111111111111111",
                    char_start=5,
                    char_end=32,
                    locator="p.1",
                ),
                stance="supports",
                claim_text_digest=digest_text("stable-v2 remains recommended"),
            ),
        ),
        claims=(
            ResearchClaim(
                claim_id="claim:1111111111111111",
                claim_text="stable-v2 remains recommended",
                claim_section="evidence",
                citation_numbers=(1,),
                evidence_refs=("evidence:1111111111111111",),
                status="evidence_backed",
            ),
            ResearchClaim(
                claim_id="claim:2222222222222222",
                claim_text="The current recommendation is official.",
                claim_section="conclusion",
                citation_numbers=(1,),
                evidence_refs=(),
                status="unsupported",
            ),
        ),
        assumptions=(),
        relations=(
            ResearchClaimRelation(
                relation_id="relation:1111111111111111",
                relation_kind="supports",
                from_ref="claim:1111111111111111",
                to_ref="evidence:1111111111111111",
                citation_numbers=(1,),
            ),
        ),
        unsupported_claim_count=1,
        run_id="run-self",
        session_id="session-self",
        project_ref={"basename": "", "digest": ""},
        synthesis_id="synthesis:self",
        stop_reason="done",
    )
    followup_record = ResearchRecord(
        record_id="research_record:2222222222222222",
        record_digest=digest_text("followup-record"),
        question=initial_record.question,
        answer_status="partial",
        sources=(
            *initial_record.sources,
            ResearchSource(
                source_id="source:2222222222222222",
                requested_url_ref={"host": "source-b.test"},
                final_url_ref={"host": "source-b.test"},
                host="source-b.test",
                title_digest=digest_text("Benchmark source B"),
                content_hash="hash-b",
                retrieved_at="2026-08-20T00:00:00Z",
                content_kind="html",
                quality={"level": "primary", "kind": "web", "freshness": "recent"},
            ),
        ),
        evidence=(
            *initial_record.evidence,
            ResearchEvidence(
                evidence_id="evidence:2222222222222222",
                source_id="source:2222222222222222",
                excerpt_digest=digest_text("stable-v2 remains recommended"),
                bounded_excerpt="stable-v2 remains recommended",
                locator=EvidenceLocator(
                    kind="html",
                    source_id="source:2222222222222222",
                    char_start=7,
                    char_end=34,
                    locator="p.1",
                ),
                stance="supports",
                claim_text_digest=digest_text("stable-v2 remains recommended"),
            ),
        ),
        claims=initial_record.claims,
        assumptions=(),
        relations=initial_record.relations,
        unsupported_claim_count=1,
        run_id="run-self",
        session_id="session-self",
        project_ref={"basename": "", "digest": ""},
        synthesis_id="synthesis:self",
        stop_reason="done",
    )
    initial_result = ResearchRunResult(
        question="Widget Storage API recommendation endpoint",
        summary=(
            "## 结论\n"
            "stable-v2 endpoint remains recommended [1].\n\n"
            "## 关键证据\n"
            "- stable-v2 remains recommended [1].\n\n"
            "## 反证与限制\n"
            "- 未找到强反证。\n\n"
            "## 来源质量\n"
            "- [1] Benchmark source A - https://source-a.test/widget-storage\n\n"
            "## 搜索覆盖\n"
            "- Widget Storage API recommendation endpoint\n\n"
            "## 来源\n"
            "[1] Benchmark source A - https://source-a.test/widget-storage"
        ),
        stop_reason="done",
        turns=6,
        opened_sources=[
            {
                "final_url": "https://source-a.test/widget-storage",
                "title": "Benchmark source A",
            },
        ],
        evidence_items=[
            {
                "claim": "stable-v2 remains recommended",
                "source_url": "https://source-a.test/widget-storage",
                "excerpt": "stable-v2 remains recommended",
                "stance": "supports",
            },
        ],
        citation_map=[
            {
                "number": 1,
                "title": "Benchmark source A",
                "url": "https://source-a.test/widget-storage",
            },
        ],
        source_urls=["https://source-a.test/widget-storage"],
        sources_read=1,
        research_record=initial_record,
        max_turns_used=14,
    )
    followup_result = ResearchRunResult(
        question=initial_result.question,
        summary=(
            "## 结论\n"
            "stable-v2 endpoint remains recommended [1][2].\n\n"
            "## 关键证据\n"
            "- stable-v2 remains recommended [1].\n"
            "- stable-v2 remains recommended [2].\n\n"
            "## 反证与限制\n"
            "- 未找到强反证。\n\n"
            "## 来源质量\n"
            "- [1] Benchmark source A - https://source-a.test/widget-storage\n"
            "- [2] Benchmark source B - https://source-b.test/widget-storage-update\n\n"
            "## 搜索覆盖\n"
            "- Widget Storage API recommendation endpoint\n\n"
            "## 来源\n"
            "[1] Benchmark source A - https://source-a.test/widget-storage\n"
            "[2] Benchmark source B - https://source-b.test/widget-storage-update"
        ),
        stop_reason="done",
        turns=2,
        opened_sources=[
            {
                "final_url": "https://source-a.test/widget-storage",
                "title": "Benchmark source A",
            },
            {
                "final_url": "https://source-b.test/widget-storage-update",
                "title": "Benchmark source B",
            },
        ],
        evidence_items=[
            {
                "claim": "stable-v2 remains recommended",
                "source_url": "https://source-a.test/widget-storage",
                "excerpt": "stable-v2 remains recommended",
                "stance": "supports",
            },
            {
                "claim": "stable-v2 remains recommended",
                "source_url": "https://source-b.test/widget-storage-update",
                "excerpt": "stable-v2 remains recommended",
                "stance": "supports",
            },
        ],
        citation_map=[
            {
                "number": 1,
                "title": "Benchmark source A",
                "url": "https://source-a.test/widget-storage",
            },
            {
                "number": 2,
                "title": "Benchmark source B",
                "url": "https://source-b.test/widget-storage-update",
            },
        ],
        source_urls=[
            "https://source-a.test/widget-storage",
            "https://source-b.test/widget-storage-update",
        ],
        sources_read=2,
        research_record=followup_record,
        max_turns_used=14,
    )
    patched = _ab_patch_only_merge_result(initial_result, followup_result)
    assert patched.applied is True
    assert patched.reason == "patch_only_merge"
    assert patched.new_source_urls == ("https://source-b.test/widget-storage-update",)
    assert patched.new_evidence_count == 1
    assert patched.new_claim_count == 1
    assert patched.result is not None
    assert "A/B Patch-Only Follow-up" not in patched.result.summary
    assert "Benchmark source B" in patched.result.summary
    assert "- stable-v2 remains recommended [2]" in patched.result.summary
    assert "## 来源\n[1] Benchmark source A - https://source-a.test/widget-storage\n[2] Benchmark source B - https://source-b.test/widget-storage-update" in patched.result.summary
    assert patched.result.research_record is not None
    patched_counts = _record_counts(patched.result.research_record)
    assert patched_counts["source_count"] == 2
    assert patched_counts["evidence_count"] == 2
    assert patched_counts["claim_count"] == 2
    assert patched_counts["unsupported_claim_count"] == 0
    assert patched.result.research_record.answer_status == "partial"
    patched_review = review_research_proof(
        patched.result.research_record,
        question=patched.result.question,
    )
    assert "unsupported_claims" not in patched_review.missing_evidence
    no_evidence_record = replace(
        followup_record,
        evidence=initial_record.evidence,
        claims=initial_record.claims,
        relations=initial_record.relations,
    )
    no_evidence_result = replace(
        followup_result,
        evidence_items=list(initial_result.evidence_items),
        research_record=no_evidence_record,
    )
    material_patched = _ab_patch_only_merge_result(
        initial_result,
        no_evidence_result,
        material,
    )
    assert material_patched.applied is True
    assert material_patched.reason == "material_patch_only_merge"
    assert material_patched.new_source_urls == ("https://source-b.test/widget-storage-update",)
    assert material_patched.new_evidence_count == 1
    assert material_patched.new_claim_count == 1
    assert material_patched.result is not None
    assert "Benchmark source B" in material_patched.result.summary
    assert "## 来源\n[1] Benchmark source A - https://source-a.test/widget-storage\n[2] Benchmark source B - https://source-b.test/widget-storage-update" in material_patched.result.summary
    assert material_patched.result.research_record is not None
    material_counts = _record_counts(material_patched.result.research_record)
    assert material_counts["source_count"] == 2
    assert material_counts["evidence_count"] == 2
    assert material_counts["claim_count"] == 2
    assert material_counts["unsupported_claim_count"] == 0
    assert _expected_terms_present("stable-v2 endpoint", ("stable-v2",))
    assert _trace_output_path(Path("tests/manual/results/bounded_research_planner_ab-deepseek.json")).name == (
        "bounded_research_planner_ab-deepseek.trace.json"
    )
    assert _pending_case_keys(
        cases=(CASES["warehouse_gap"],),
        arms=("baseline",),
        existing=set(),
    ) == [("warehouse_gap", "baseline")]
    with tempfile.TemporaryDirectory(prefix="codey-bounded-planner-ab-self-") as td:
        trace = LiveTrace(Path(td) / "trace.json")
        trace.record_send_start(
            run_id="run-self",
            provider="deepseek",
            provider_name="DeepSeek",
            case="warehouse_gap",
            arm="baseline",
            turn=1,
            prompt="prompt text",
        )
        trace.record_reply(
            run_id="run-self",
            provider="deepseek",
            provider_name="DeepSeek",
            case="warehouse_gap",
            arm="baseline",
            turn=1,
            prompt="prompt text",
            reply='{"tool":"done","args":{"answer":"ok"}}',
        )
        payload = json.loads((Path(td) / "trace.json").read_text(encoding="utf-8"))
        assert payload["probe"] == "bounded_research_planner_ab_trace"
        assert payload["event_count"] == 2
        appended_trace = LiveTrace(Path(td) / "trace.json")
        appended_trace.record_case_complete(
            run_id="run-self",
            provider="deepseek",
            case="warehouse_gap",
            arm="planner",
            row={"ok": True, "score": 1, "stop_reason": "done", "summary_preview": "ok"},
        )
        appended_payload = json.loads((Path(td) / "trace.json").read_text(encoding="utf-8"))
        assert appended_payload["event_count"] == 3
        assert [event["event"] for event in appended_payload["events"]] == [
            "send_start",
            "reply",
            "case_complete",
        ]
        output = Path(td) / "payload.json"
        output.write_text(
            json.dumps({"probe": "bounded_research_planner_ab", "provider": "qwen", "rows": []}),
            encoding="utf-8",
        )
        try:
            _load_or_new_payload(
                output,
                provider_id="deepseek",
                cases=(CASES["warehouse_gap"],),
                arms=("baseline",),
            )
        except OutputProviderMismatch as exc:
            assert exc.expected == "deepseek"
            assert exc.found == "qwen"
        else:
            raise AssertionError("provider mismatch was not rejected")
        payload = {
            "probe": "bounded_research_planner_ab",
            "provider": "deepseek",
            "rows": [],
            "cases": ["warehouse_gap"],
            "arms": ["baseline"],
        }
        _normalize_payload_metadata(
            payload,
            provider_id="deepseek",
            cases=(CASES["warehouse_gap"], CASES["widget_noop"]),
            arms=("planner",),
        )
        assert payload["cases"] == ["warehouse_gap", "widget_noop"]
        assert payload["arms"] == ["baseline", "planner"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Live A/B for 0.4.4 bounded Research planner")
    parser.add_argument("--provider", choices=(*WEB_PROVIDERS, "all"), default="deepseek")
    parser.add_argument("--case", action="append", help="case name or comma list; defaults to all cases")
    parser.add_argument("--arms", default="baseline,planner")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trace-output", type=Path, default=None, help="trace path; default is next to output with a .trace.json suffix")
    parser.add_argument("--max-turns", type=int, default=14)
    parser.add_argument("--send-timeout", type=float, default=120)
    parser.add_argument("--new-chat-timeout", type=float, default=60)
    parser.add_argument("--open-if-missing", action="store_true")
    parser.add_argument("--rerun-failed", action="store_true")
    parser.add_argument("--no-live-trace", action="store_true", help="disable incremental atomic trace writes")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        print("self-test ok")
        return 0

    selected_cases = tuple(CASES[name] for name in _parse_names(args.case, CASES, "case"))
    selected_arms = _parse_names([args.arms], ARMS, "arm")
    providers = WEB_PROVIDERS if args.provider == "all" else (args.provider,)
    run_id = f"bounded-research-planner-ab-{int(time.time())}"
    for provider_id in providers:
        output = args.output or _default_output(provider_id)
        if args.provider == "all" and args.output is not None:
            output = args.output.with_name(f"{args.output.stem}-{provider_id}{args.output.suffix}")
        trace: LiveTrace | None = None
        if not args.no_live_trace:
            if args.trace_output is not None:
                trace_output = args.trace_output
                if args.provider == "all":
                    trace_output = args.trace_output.with_name(
                        f"{args.trace_output.stem}-{provider_id}{args.trace_output.suffix}"
                    )
            else:
                trace_output = _trace_output_path(output)
            trace = LiveTrace(trace_output)
        try:
            run_provider(
                provider_id,
                cases=selected_cases,
                arms=selected_arms,
                port=args.port,
                output=output,
                max_turns=max(1, args.max_turns),
                send_timeout=args.send_timeout,
                new_chat_timeout=args.new_chat_timeout,
                open_if_missing=args.open_if_missing,
                rerun_failed=args.rerun_failed,
                trace=trace,
                run_id=run_id,
            )
        except OutputProviderMismatch as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
