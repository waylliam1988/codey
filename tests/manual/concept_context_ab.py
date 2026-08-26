"""Live A/B probe for manual-only Concept Context injection.

Compares the current production Research intro (baseline) against the same
intro plus a bounded, probe-local Concept Context block built from declared
concept relations and missing-link candidates in the seeded vault. The block
is a prototype that lives only in this script; production prompts, tools, and
Research behavior are unchanged.

The live provider runs a real JSON-tool research loop, but web search and
source bodies are deterministic fixtures. The bridge-real case contains a
document that genuinely connects two concepts sharing a declared neighbor;
the bridge-none control contains no such document, so the probe can measure
whether the concept block induces unsupported relations.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ruff: noqa: E402 - direct script execution must add the repository root first.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codey.runtime import cancellation
from codey.knowledge.concept_schema import normalize_concept
from codey.knowledge.concepts import SupportRef, _missing_suggestions
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.providers.registry import connect_provider, provider_ids
from codey.research.protocols import JsonToolCodec
from codey.research.runner import ResearchRunner
from codey.research.source_document import SourceDocument
from codey.research.tools import OPEN_DEFAULT_LIMIT, OPEN_MAX_LIMIT, ResearchTools

ARMS = ("baseline", "concept")
DEFAULT_MAX_TURNS = 12
DEFAULT_OUTPUT = Path(tempfile.gettempdir()) / "codey-concept-context-ab.json"
MAX_REPLY_CHARS = 4_000
MAX_FIELD_CHARS = 2_000

CONCEPT_CONTEXT_MAX_CHARS = 1_200
CONCEPT_CONTEXT_MAX_RELATIONS = 8
CONCEPT_CONTEXT_MAX_SUGGESTIONS = 3
CONCEPT_EDGE_ROW_SCAN = 512

_TOKEN_RE = re.compile(r"[a-z0-9一-鿿]+")


# ---------------------------------------------------------------------------
# Probe-local Concept Context prototype (manual-only)
# ---------------------------------------------------------------------------


def concept_context_block(store: KnowledgeStore, session_id: str, question: str) -> str:
    """Bounded background block from declared relations near the question.

    Empty string when no known concept matches the question, so the block is
    zero-cost for unrelated research runs. Missing-link candidates reuse the
    production `_missing_suggestions` query and stay marked as unproven.
    """
    try:
        rows = store.index.concept_edge_rows(CONCEPT_EDGE_ROW_SCAN, session_id=session_id)
    except Exception:
        return ""
    declared: dict[tuple[str, str, str], list[SupportRef]] = {}
    for row in rows:
        key = (str(row["src"]), str(row["dst"]), str(row["kind"]))
        refs = declared.setdefault(key, [])
        note_id = str(row["note_id"])
        if note_id not in {ref.note_id for ref in refs}:
            refs.append(SupportRef(
                note_id,
                str(row.get("title") or note_id),
                str(row.get("session_id") or ""),
            ))
    if not declared:
        return ""
    concepts = sorted({c for (src, dst, _kind) in declared for c in (src, dst)})
    matched = _matched_concepts(question, concepts)
    if not matched:
        return ""
    relation_lines: list[str] = []
    for (src, dst, kind), refs in sorted(declared.items()):
        if src not in matched and dst not in matched:
            continue
        if len(relation_lines) >= CONCEPT_CONTEXT_MAX_RELATIONS:
            break
        titles = "; ".join(ref.title for ref in refs[:2])
        relation_lines.append(f"- {src} --{kind}--> {dst} ({len(refs)} note(s): {titles})")
    suggestion_lines: list[str] = []
    seen: set[str] = set()
    for concept, lines in _missing_suggestions(declared, concepts).items():
        if concept not in matched:
            continue
        for line in lines:
            if line not in seen and len(suggestion_lines) < CONCEPT_CONTEXT_MAX_SUGGESTIONS:
                seen.add(line)
                suggestion_lines.append(line)
    parts = [
        "Concept context from your local knowledge graph (bounded background):",
        "Declared relations near this question:",
        *relation_lines,
    ]
    if suggestion_lines:
        parts.append("Open questions (unproven; not facts):")
        parts.extend(suggestion_lines)
    parts.extend((
        "Concept context discipline:",
        "- Open questions are structural guesses from your own notes, not facts.",
        "- You may investigate one when relevant, but declare a relation only when an opened source explicitly states it.",
        "- Never cite this block as evidence; evidence comes only from open_url text.",
    ))
    block = "\n".join(parts)
    if len(block) > CONCEPT_CONTEXT_MAX_CHARS:
        block = block[:CONCEPT_CONTEXT_MAX_CHARS].rstrip()
    return block


def _matched_concepts(question: str, concepts: list[str]) -> set[str]:
    folded = str(question or "").lower()
    tokens = set(_TOKEN_RE.findall(folded))
    matched: set[str] = set()
    for concept in concepts:
        c = str(concept or "").lower().strip()
        if not c:
            continue
        if c in folded:
            matched.add(concept)
            continue
        concept_tokens = _TOKEN_RE.findall(c)
        if concept_tokens and all(token in tokens for token in concept_tokens):
            matched.add(concept)
    return matched


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureDocument:
    url: str
    title: str
    text: str
    snippet: str

    def result(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass(frozen=True)
class SeedNote:
    type: str
    title: str
    body: str
    sources: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    relations: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ConceptProbeCase:
    name: str
    question: str
    documents: tuple[FixtureDocument, ...]
    seed_notes: tuple[SeedNote, ...]
    expects_bridge: bool
    primary_url: str
    expected_report_terms: tuple[str, ...]
    expected_evidence_terms: tuple[str, ...]
    bridge_url: str = ""
    bridge_report_terms: tuple[str, ...] = ()
    bridge_evidence_terms: tuple[str, ...] = ()


HELIUM_URL = "https://heliumreview.org/helium-supply-review"
COPPER_URL = "https://cu-brief.net/copper-war-brief"
BRIDGE_URL = "https://tradecontrols.org/strategic-export-controls"

HELIUM_EVIDENCE = "the war closed two of the five major helium extraction hubs"
BRIDGE_EVIDENCE = "share the same sanctioned wartime logistics corridor"

HELIUM_DOC = FixtureDocument(
    HELIUM_URL,
    "Global helium supply review",
    (
        "Global helium supply review. The main current risk to helium supply is "
        "wartime export disruption: the war closed two of the five major helium "
        "extraction hubs, and remaining helium exports face wartime shipping "
        "constraints and allocation limits. Semiconductor and medical demand "
        "continues to grow, so industrial buyers now hold larger safety stocks."
    ),
    "Review of current helium supply risks and wartime export disruption.",
)

COPPER_DOC = FixtureDocument(
    COPPER_URL,
    "Copper supply war brief",
    (
        "Copper supply war brief. The war reduced refined copper supply because "
        "two smelters near the conflict zone shut down, and wartime logistics "
        "raised concentrate freight costs. Industrial buyers report longer "
        "delivery windows for refined copper this year."
    ),
    "Brief on how the war reduced refined copper supply.",
)

BRIDGE_DOC = FixtureDocument(
    BRIDGE_URL,
    "Copper trade compliance update",
    (
        "Copper trade compliance update. The new wartime export control regime "
        "lists both copper and helium as controlled strategic materials. Copper "
        "and helium shipments now share the same sanctioned wartime logistics "
        "corridor, so a single corridor disruption now affects copper supply "
        "and helium supply together. Exporters must file the same wartime "
        "control paperwork for both materials."
    ),
    # Copper-only searchable surface: helium-side queries must not surface
    # this document, so the bridge is only reachable via copper-driven search.
    "Compliance paperwork changes for copper shipments through the sanctioned corridor.",
)

SEED_NOTES = (
    SeedNote(
        "fact",
        "War disrupts helium exports",
        "The war disrupted helium exports from major extraction hubs.",
        sources=(HELIUM_URL,),
        tags=("war", "helium supply"),
        relations=({"src": "war", "dst": "helium supply", "kind": "affects"},),
    ),
    SeedNote(
        "fact",
        "War reduces copper supply",
        "The war reduced refined copper supply after smelters shut down.",
        sources=(COPPER_URL,),
        tags=("war", "copper supply"),
        relations=({"src": "war", "dst": "copper supply", "kind": "affects"},),
    ),
)

QUESTION = "Research the main current risks to helium supply."

CASES = (
    ConceptProbeCase(
        name="bridge-real",
        question=QUESTION,
        documents=(HELIUM_DOC, COPPER_DOC, BRIDGE_DOC),
        seed_notes=SEED_NOTES,
        expects_bridge=True,
        primary_url=HELIUM_URL,
        expected_report_terms=("wartime export disruption",),
        expected_evidence_terms=(HELIUM_EVIDENCE,),
        bridge_url=BRIDGE_URL,
        bridge_report_terms=("copper",),
        bridge_evidence_terms=(BRIDGE_EVIDENCE,),
    ),
    ConceptProbeCase(
        name="bridge-none",
        question=QUESTION,
        documents=(HELIUM_DOC, COPPER_DOC),
        seed_notes=SEED_NOTES,
        expects_bridge=False,
        primary_url=HELIUM_URL,
        expected_report_terms=("wartime export disruption",),
        expected_evidence_terms=(HELIUM_EVIDENCE,),
    ),
)


class FixtureSearchProvider:
    name = "fixture-search"

    def __init__(self, case: ConceptProbeCase) -> None:
        self.case = case
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 8) -> list[dict]:
        self.queries.append(str(query or ""))
        query_tokens = _match_tokens(query)
        matched = [
            doc for doc in self.case.documents
            if query_tokens & _match_tokens(f"{doc.title} {doc.snippet}")
        ]
        return [doc.result() for doc in matched[:limit]]

    def fetch(self, url: str) -> dict:
        doc = self.document_for_url(url)
        if doc is None:
            return {"url": url, "title": "", "text": "ERROR: fixture URL not found", "truncated": False}
        return {"url": doc.url, "title": doc.title, "text": doc.text, "truncated": False}

    def document_for_url(self, url: str) -> FixtureDocument | None:
        normalized = str(url or "").strip()
        for doc in self.case.documents:
            if normalized == doc.url:
                return doc
        return None

    def close(self) -> None:
        pass


def _match_tokens(text: object) -> set[str]:
    """Search-matching tokens: title+snippet only, generic short words dropped.

    Requiring length >= 4 keeps stopword-ish tokens ("the", "war", "new") from
    surfacing every document for every query, so discovery depends on real
    topical overlap instead of shared filler vocabulary.
    """
    return {
        token
        for token in _TOKEN_RE.findall(str(text or "").lower())
        if len(token) >= 4
    }


# ---------------------------------------------------------------------------
# Probe runner wiring
# ---------------------------------------------------------------------------


class ProbeResearchTools(ResearchTools):
    """Production tools with fixture-only open_url (no network, no DNS)."""

    def open_url(
        self,
        url: str,
        offset: int = 0,
        limit: int = OPEN_DEFAULT_LIMIT,
        pages: str = "",
    ) -> str:
        url = str(url or "").strip()
        lookup = getattr(self.search, "document_for_url", lambda _url: None)
        doc = lookup(url)
        if doc is None:
            return "ERROR: fixture URL not found: " + url
        offset = max(0, _as_int(offset, 0))
        limit = min(OPEN_MAX_LIMIT, max(500, _as_int(limit, OPEN_DEFAULT_LIMIT)))
        document = SourceDocument.html(
            requested_url=url,
            final_url=doc.url,
            title=doc.title,
            text=doc.text,
        )
        self.sources_read.add(url)
        self.ledger.record_open_document(document)
        window = document.text[offset : offset + limit]
        body = f"{doc.title}\n{doc.url}\n\n{window}".strip()
        if offset + limit < len(document.text):
            body += f"\n\n[more text available: open with offset={offset + limit}]"
        return body


_FIXTURE_FRONT = """Probe fixture hard boundary:
- Reply only with one JSON tool call. Do not write the research answer directly.
- Do not use this chat website's built-in web search, browsing, plugins, or outside knowledge.
- Use only these local JSON tools for evidence: web_search/open_url/source_search/knowledge_search/knowledge_read/knowledge_write/knowledge_link.
- Tool outputs are the only evidence in this probe."""

_FIXTURE_REMINDER = """Probe fixture discipline:
- Do not use this chat website's built-in web search, browsing, plugins, or outside knowledge.
- All web and knowledge access in this probe goes only through the local JSON tools.
- For search query strings, use plain keywords; do not put literal double quote characters inside JSON strings.
- The source URLs and facts are deterministic local fixtures; they may not exist on the public internet.
- Treat tool outputs as the only evidence, even when a fixture domain looks fake or unreachable publicly."""


def _debrand(text: str) -> str:
    """Strip the Codey product name from everything sent to the web model.

    Probe-only wording experiment: the model is addressed as a plain local
    assistant/runtime, applied at the single send choke point so both arms
    and every follow-up (repair, results, quality review) stay identical.
    """
    return (
        str(text or "")
        .replace("Codey's", "the local runtime's")
        .replace("Codey Research", "The local research runtime")
        .replace("Codey", "the local runtime")
    )


class ProbeJsonToolCodec(JsonToolCodec):
    def system_prompt(self) -> str:
        return _FIXTURE_FRONT + "\n\n" + super().system_prompt() + "\n\n" + _FIXTURE_REMINDER

    def repair_prompt(self) -> str:
        return super().repair_prompt() + "\n\n" + _FIXTURE_REMINDER

    def format_results(self, results) -> str:
        return super().format_results(results) + "\n\n" + _FIXTURE_REMINDER


class ProbeResearchRunner(ResearchRunner):
    def __init__(
        self,
        provider,
        search,
        store: KnowledgeStore,
        *,
        arm: str,
        max_turns: int,
        provider_id: str = "",
        case_name: str = "",
        trace: "LiveTrace | None" = None,
    ) -> None:
        super().__init__(
            provider,
            search,
            store,
            max_turns=max_turns,
            codec=ProbeJsonToolCodec(),
            session_id=probe_session_id(arm),
            controller_enabled=False,
        )
        self.arm = arm
        self.provider_id = provider_id
        self.case_name = case_name
        self.trace = trace
        self.concept_block = ""
        self.sent_messages: list[str] = []
        self.received_replies: list[str] = []
        self.tools = ProbeResearchTools(
            search=search,
            store=store,
            changes=self.changes,
            session_id=probe_session_id(arm),
        )

    def _intro(self, question: str) -> str:
        base = super()._intro(question)
        if self.arm != "concept":
            return base
        self.concept_block = concept_context_block(self.store, self.session_id, question)
        if not self.concept_block:
            return base
        marker = "Research question:\n"
        if marker in base:
            return base.replace(marker, self.concept_block + "\n\n" + marker, 1)
        return base + "\n\n" + self.concept_block

    def _send_provider(self, message: str) -> str:
        message = _debrand(message)
        self.sent_messages.append(message)
        try:
            cancellation.check()
            if self.trace is not None:
                self.trace.record({
                    "event": "send_start",
                    "provider": self.provider_id,
                    "case": self.case_name,
                    "arm": self.arm,
                    "send_index": len(self.sent_messages),
                    "sent_chars": len(message or ""),
                    "message_preview": _clip(message, 800),
                })
            reply = self.provider.send(message)
            cancellation.check()
            text = str(reply or "")
            self.received_replies.append(text)
            if self.trace is not None:
                self.trace.record({
                    "event": "reply",
                    "provider": self.provider_id,
                    "case": self.case_name,
                    "arm": self.arm,
                    "send_index": len(self.sent_messages),
                    "reply_chars": len(text),
                    "reply": _clip(text, MAX_REPLY_CHARS),
                })
            return text
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            if self.trace is not None:
                self.trace.record({
                    "event": "send_error",
                    "provider": self.provider_id,
                    "case": self.case_name,
                    "arm": self.arm,
                    "send_index": len(self.sent_messages),
                    "error": f"{type(exc).__name__}: {exc}",
                })
            self._record_model_failure("send", exc)
            raise


def probe_session_id(arm: str) -> str:
    return f"concept-context-ab-{arm}"


class TimedProvider:
    def __init__(self, provider, *, send_timeout: float, new_chat_timeout: float) -> None:
        self.provider = provider
        self.send_timeout = max(1.0, float(send_timeout))
        self.new_chat_timeout = max(1.0, float(new_chat_timeout))
        self.name = getattr(provider, "name", "")
        self.location = getattr(provider, "location", "")

    def new_chat(self, timeout=None) -> None:
        effective = self.new_chat_timeout if timeout is None else timeout
        return self.provider.new_chat(timeout=effective)

    def send(self, text: str, timeout=None) -> str:
        effective = self.send_timeout if timeout is None else timeout
        return self.provider.send(text, timeout=effective)

    def close(self) -> None:
        return self.provider.close()


class LiveTrace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []

    def record(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        self.events.append(payload)
        _write_json_atomic(self.path, {
            "probe": "concept_context_ab_trace",
            "event_count": len(self.events),
            "events": self.events,
        })


def _seed_store(store: KnowledgeStore, case: ConceptProbeCase, arm: str) -> None:
    for seed in case.seed_notes:
        note = KnowledgeNote.create(
            type=seed.type,
            title=seed.title,
            body=seed.body,
            sources=list(seed.sources),
            tags=list(seed.tags),
            relations=[dict(item) for item in seed.relations],
            session_id=probe_session_id(arm),
        )
        store.write_note(note)


# ---------------------------------------------------------------------------
# Case execution and scoring
# ---------------------------------------------------------------------------


def run_case(
    provider,
    provider_id: str,
    case: ConceptProbeCase,
    arm: str,
    max_turns: int,
    trace: LiveTrace | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codey-concept-ab-") as td:
        store = KnowledgeStore(Path(td))
        try:
            _seed_store(store, case, arm)
            search = FixtureSearchProvider(case)
            runner = ProbeResearchRunner(
                provider,
                search,
                store,
                arm=arm,
                max_turns=max_turns,
                provider_id=provider_id,
                case_name=case.name,
                trace=trace,
            )
            events = []
            started = time.monotonic()
            for event in runner.run(case.question):
                events.append(event)
            result = runner.result
            if result is None:
                raise RuntimeError("research finished without result")
            row = {
                "provider": provider_id,
                "case": case.name,
                "arm": arm,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "send_count": len(runner.sent_messages),
                "sent_chars": sum(len(item) for item in runner.sent_messages),
                "first_prompt_chars": len(runner.sent_messages[0]) if runner.sent_messages else 0,
                "concept_block_chars": len(runner.concept_block),
                "protocol_repair_prompts": _protocol_repair_prompt_count(runner.sent_messages),
                "quality_repair_prompts": _sent_prompt_count(runner.sent_messages, "research quality review"),
                "stop_reason": result.stop_reason,
                "turns_used": result.turns,
                "queries": list(result.queries),
                "source_urls": list(result.source_urls),
                "evidence_items": result.evidence_items,
                "quality_warnings": result.quality_warnings,
                "summary_preview": _clip(result.summary, MAX_REPLY_CHARS),
                "raw_reply_previews": [
                    _clip(reply, 1_000) for reply in runner.received_replies[-3:]
                ],
                "tool_calls": _tool_calls(events),
            }
            row.update(_score(case, row, result))
            return row
        finally:
            store.close()


def _tool_calls(events: list[object]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        call = getattr(event, "call", None)
        if getattr(event, "kind", "") != "tool" or call is None:
            continue
        outcome = getattr(event, "outcome", None)
        rows.append({
            "name": call.name,
            "args": dict(call.args),
            "status": getattr(outcome, "status", ""),
            "result": outcome.presentation_result(240) if outcome is not None else "",
        })
    return rows


def _declared_relation_pairs(tool_calls: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    for call in tool_calls:
        if call.get("name") != "knowledge_write":
            continue
        args = call.get("args") or {}
        if not isinstance(args, dict):
            continue
        relations = args.get("relations")
        if isinstance(relations, dict):
            relations = [relations]
        if not isinstance(relations, list):
            continue
        for relation in relations:
            if not isinstance(relation, dict):
                continue
            src = normalize_concept(relation.get("src"))
            dst = normalize_concept(relation.get("dst"))
            kind = str(relation.get("kind") or "relates").strip().lower()
            if src and dst:
                pairs.append((src, dst, kind))
    return pairs


def _is_bridge_pair(src: str, dst: str) -> bool:
    endpoints = (src, dst)
    has_copper = any("copper" in item for item in endpoints)
    has_helium = any("helium" in item for item in endpoints)
    return has_copper and has_helium


def _model_claim_text(tool_calls: list[dict[str, Any]], summary: str) -> str:
    parts = [str(summary or "")]
    for call in tool_calls:
        if call.get("name") != "knowledge_write":
            continue
        args = call.get("args") or {}
        if not isinstance(args, dict):
            continue
        parts.extend((str(args.get("title") or ""), str(args.get("body") or "")))
        evidence = args.get("evidence")
        if isinstance(evidence, dict):
            evidence = [evidence]
        if isinstance(evidence, list):
            for item in evidence:
                if isinstance(item, dict):
                    parts.append(str(item.get("claim") or ""))
    return "\n".join(parts).lower()


def _score(case: ConceptProbeCase, row: dict[str, Any], result) -> dict[str, Any]:
    tool_calls = row["tool_calls"]
    claim_text = _model_claim_text(tool_calls, result.summary)
    evidence_text_by_url: dict[str, str] = {}
    for item in result.evidence_items:
        url = str(item.get("source_url") or "")
        evidence_text_by_url[url] = (
            evidence_text_by_url.get(url, "") + "\n" + str(item.get("excerpt") or "")
        ).lower()
    opened_urls = set(result.source_urls)
    final_done = result.stop_reason == "done"
    opened_primary_source = case.primary_url in opened_urls
    target_fact_reported = all(term.lower() in claim_text for term in case.expected_report_terms)
    all_evidence_text = "\n".join(evidence_text_by_url.values())
    saved_exact_evidence_snippet = all(
        term.lower() in all_evidence_text for term in case.expected_evidence_terms
    )
    declared_pairs = _declared_relation_pairs(tool_calls)
    bridge_relation_declared = any(_is_bridge_pair(src, dst) for (src, dst, _kind) in declared_pairs)
    opened_bridge_source = bool(case.bridge_url) and case.bridge_url in opened_urls
    bridge_fact_reported = bool(case.bridge_report_terms) and all(
        term.lower() in claim_text for term in case.bridge_report_terms
    )
    bridge_evidence_saved = bool(case.bridge_url) and any(
        term.lower() in evidence_text_by_url.get(case.bridge_url, "")
        for term in case.bridge_evidence_terms
    )
    if case.expects_bridge:
        bridge_found_and_supported = (
            opened_bridge_source and bridge_fact_reported and bridge_evidence_saved
        )
        false_bridge_relation = bridge_relation_declared and not bridge_evidence_saved
    else:
        bridge_found_and_supported = False
        false_bridge_relation = bridge_relation_declared
    unsupported_citation_count = _unsupported_citation_count(result.summary, opened_urls)
    max_turns_failure = result.stop_reason == "max_turns"
    fixture_urls = {doc.url for doc in case.documents}
    nonfixture_open_count = sum(
        1
        for call in tool_calls
        if call.get("name") == "open_url"
        and str((call.get("args") or {}).get("url") or "").strip() not in fixture_urls
    )
    return {
        "nonfixture_open_count": nonfixture_open_count,
        "final_done": final_done,
        "opened_primary_source": opened_primary_source,
        "target_fact_reported": target_fact_reported,
        "saved_exact_evidence_snippet": saved_exact_evidence_snippet,
        "bridge_applicable": case.expects_bridge,
        "opened_bridge_source": opened_bridge_source,
        "bridge_fact_reported": bridge_fact_reported,
        "bridge_evidence_saved": bridge_evidence_saved,
        "bridge_relation_declared": bridge_relation_declared,
        "bridge_found_and_supported": bridge_found_and_supported,
        "false_bridge_relation": false_bridge_relation,
        "declared_relation_pairs": [list(pair) for pair in declared_pairs],
        "unsupported_citation_count": unsupported_citation_count,
        "max_turns_failure": max_turns_failure,
    }


def _unsupported_citation_count(summary: str, opened_urls: set[str]) -> int:
    count = 0
    for url in re.findall(r"https?://[^\s<>)\]]+", str(summary or "")):
        clean = url.rstrip(".,;:)")
        if clean and clean not in opened_urls:
            count += 1
    return count


def _sent_prompt_count(messages: list[str], needle: str) -> int:
    folded = str(needle or "").lower()
    if not folded:
        return 0
    return sum(1 for message in messages if folded in str(message or "").lower())


def _protocol_repair_prompt_count(messages: list[str]) -> int:
    return sum(
        1
        for message in messages
        if (
            "your last reply was not a valid tool call" in str(message or "").lower()
            or "your last reply did not satisfy codey's research tool contract"
            in str(message or "").lower()
        )
    )


# ---------------------------------------------------------------------------
# Provider loop and summary
# ---------------------------------------------------------------------------


def run_provider(
    provider_id: str,
    *,
    port: int,
    open_if_missing: bool,
    arms: tuple[str, ...],
    cases: tuple[ConceptProbeCase, ...],
    max_turns: int,
    timeout: float,
    new_chat_timeout: float,
    trace: LiveTrace | None = None,
) -> dict[str, Any]:
    try:
        provider = connect_provider(
            provider_id,
            port=port,
            open_if_missing=open_if_missing,
            bring_to_front=open_if_missing,
        )
    except Exception as exc:
        message = f"connect {type(exc).__name__}: {exc}"
        print(f"[{provider_id}] {message}")
        rows = [
            {"provider": provider_id, "case": case.name, "arm": arm, "error": message}
            for case in cases
            for arm in arms
        ]
        return {"provider": provider_id, "rows": rows, "summary": _summarize(rows), "error": message}
    provider = TimedProvider(provider, send_timeout=timeout, new_chat_timeout=new_chat_timeout)
    rows: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        for case in cases:
            for arm in arms:
                if trace is not None:
                    trace.record({
                        "event": "case_start",
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                    })
                try:
                    row = run_case(provider, provider_id, case, arm, max_turns, trace=trace)
                except Exception as exc:
                    row = {
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                rows.append(row)
                if trace is not None:
                    trace.record({
                        "event": "case_result",
                        "provider": provider_id,
                        "case": case.name,
                        "arm": arm,
                        "row": row,
                    })
                if "error" in row:
                    print(f"[{provider_id} {case.name} {arm}] {row['error']}")
                else:
                    print(
                        f"[{provider_id} {case.name} {arm}] "
                        f"done={row['final_done']} "
                        f"primary={row['opened_primary_source']} "
                        f"fact={row['target_fact_reported']} "
                        f"evidence={row['saved_exact_evidence_snippet']} "
                        f"bridge_opened={row['opened_bridge_source']} "
                        f"bridge_fact={row['bridge_fact_reported']} "
                        f"bridge_supported={row['bridge_found_and_supported']} "
                        f"false_relation={row['false_bridge_relation']} "
                        f"nonfixture_opens={row['nonfixture_open_count']} "
                        f"turns={row['turns_used']} elapsed={row['elapsed_seconds']}s"
                    )
    finally:
        provider.close()
    return {
        "provider": provider_id,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "rows": rows,
        "summary": _summarize(rows),
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"arms": {}}
    for arm in ARMS:
        arm_rows = [row for row in rows if row.get("arm") == arm and "error" not in row]
        if not arm_rows:
            summary["arms"][arm] = {"count": 0}
            continue
        bridge_rows = [row for row in arm_rows if row.get("bridge_applicable")]
        control_rows = [row for row in arm_rows if not row.get("bridge_applicable")]
        summary["arms"][arm] = {
            "count": len(arm_rows),
            "final_done_rate": _rate(arm_rows, "final_done"),
            "opened_primary_source_rate": _rate(arm_rows, "opened_primary_source"),
            "target_fact_reported_rate": _rate(arm_rows, "target_fact_reported"),
            "saved_exact_evidence_snippet_rate": _rate(arm_rows, "saved_exact_evidence_snippet"),
            "bridge_found_and_supported_rate": _rate(bridge_rows, "bridge_found_and_supported"),
            "opened_bridge_source_rate": _rate(bridge_rows, "opened_bridge_source"),
            "false_bridge_relation_count": sum(
                1 for row in arm_rows if row.get("false_bridge_relation")
            ),
            "control_false_bridge_relation_count": sum(
                1 for row in control_rows if row.get("false_bridge_relation")
            ),
            "unsupported_citation_count": sum(
                int(row.get("unsupported_citation_count") or 0) for row in arm_rows
            ),
            "nonfixture_open_count": sum(
                int(row.get("nonfixture_open_count") or 0) for row in arm_rows
            ),
            "max_turns_failure_rate": _rate(arm_rows, "max_turns_failure"),
            "avg_turns_used": _avg(arm_rows, "turns_used"),
            "avg_first_prompt_chars": _avg(arm_rows, "first_prompt_chars"),
            "avg_concept_block_chars": _avg(arm_rows, "concept_block_chars"),
            "avg_protocol_repair_prompts": _avg(arm_rows, "protocol_repair_prompts"),
        }
    baseline = summary["arms"].get("baseline", {})
    concept = summary["arms"].get("concept", {})
    if baseline.get("count") and concept.get("count"):
        summary["concept_delta_vs_baseline"] = {
            "bridge_found_and_supported_rate": round(
                float(concept.get("bridge_found_and_supported_rate") or 0)
                - float(baseline.get("bridge_found_and_supported_rate") or 0),
                3,
            ),
            "false_bridge_relation_count": (
                int(concept.get("false_bridge_relation_count") or 0)
                - int(baseline.get("false_bridge_relation_count") or 0)
            ),
            "final_done_rate": round(
                float(concept.get("final_done_rate") or 0)
                - float(baseline.get("final_done_rate") or 0),
                3,
            ),
            "avg_turns_used": round(
                float(concept.get("avg_turns_used") or 0)
                - float(baseline.get("avg_turns_used") or 0),
                3,
            ),
            "avg_first_prompt_chars": round(
                float(concept.get("avg_first_prompt_chars") or 0)
                - float(baseline.get("avg_first_prompt_chars") or 0),
                3,
            ),
        }
    return summary


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(1 for row in rows if row.get(key)) / len(rows), 3)


def _avg(rows: list[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return round(sum(float(row.get(key) or 0) for row in rows) / len(rows), 3)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _clip(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "\n[truncated]"


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Self test
# ---------------------------------------------------------------------------


class ScriptedProvider:
    name = "scripted"
    location = "fixture://scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []

    def new_chat(self, timeout=None) -> None:
        pass

    def send(self, text: str, timeout=None) -> str:
        self.sent.append(text)
        if not self.replies:
            return json.dumps({"tool": "done", "args": {"answer": "done"}})
        return self.replies.pop(0)

    def close(self) -> None:
        pass


_SELF_TEST_REPORT_BRIDGE = (
    "## Conclusion\n"
    "- The main current risk to helium supply is wartime export disruption [1].\n"
    "- Copper supply shares that wartime risk: copper and helium shipments use "
    "the same sanctioned wartime logistics corridor [2].\n\n"
    "## Key evidence\n"
    "- [1] the war closed two of the five major helium extraction hubs.\n"
    "- [2] Copper and helium shipments now share the same sanctioned wartime "
    "logistics corridor.\n\n"
    "## Counter-evidence\n"
    "- No strong counter-evidence was found; the fixtures describe only wartime risk.\n\n"
    "## Source quality\n"
    "- [1] market review source; [2] export control update.\n\n"
    "## Search coverage\n"
    "- query: helium supply risk; query: copper export controls\n\n"
    "## Sources\n"
    f"[1] Global helium supply review - {HELIUM_URL}\n"
    f"[2] Copper trade compliance update - {BRIDGE_URL}"
)

_SELF_TEST_REPORT_CONTROL = (
    "## Conclusion\n"
    "- The main current risk to helium supply is wartime export disruption [1].\n\n"
    "## Key evidence\n"
    "- [1] the war closed two of the five major helium extraction hubs.\n\n"
    "## Counter-evidence\n"
    "- No strong counter-evidence was found; the fixture describes only wartime risk.\n\n"
    "## Source quality\n"
    "- [1] market review source.\n\n"
    "## Search coverage\n"
    "- query: helium supply risk\n\n"
    "## Sources\n"
    f"[1] Global helium supply review - {HELIUM_URL}"
)


def self_test() -> int:
    bridge_case = CASES[0]
    control_case = CASES[1]

    with tempfile.TemporaryDirectory(prefix="codey-concept-ab-self-") as td:
        store = KnowledgeStore(Path(td))
        _seed_store(store, bridge_case, "concept")
        edges = store.index.concept_edge_rows(64, session_id=probe_session_id("concept"))
        assert len(edges) >= 2, f"expected seeded concept edges, got {len(edges)}"
        block = concept_context_block(store, probe_session_id("concept"), QUESTION)
        assert "helium supply" in block
        assert "copper supply ? helium supply" in block, block
        assert "Open questions (unproven; not facts):" in block
        assert "declare a relation only when an opened source explicitly states it" in block
        assert len(block) <= CONCEPT_CONTEXT_MAX_CHARS
        unrelated = concept_context_block(
            store, probe_session_id("concept"), "Research quantum chip packaging yields."
        )
        assert unrelated == "", unrelated
        store.close()

    with tempfile.TemporaryDirectory(prefix="codey-concept-ab-self-") as td:
        store = KnowledgeStore(Path(td))
        _seed_store(store, bridge_case, "baseline")
        _seed_store(store, bridge_case, "concept")
        search = FixtureSearchProvider(bridge_case)
        baseline_runner = ProbeResearchRunner(
            ScriptedProvider(), search, store, arm="baseline", max_turns=4
        )
        concept_runner = ProbeResearchRunner(
            ScriptedProvider(), search, store, arm="concept", max_turns=4
        )
        baseline_intro = baseline_runner._intro(QUESTION)
        concept_intro = concept_runner._intro(QUESTION)
        assert "Concept context" not in baseline_intro
        assert "Concept context" in concept_intro
        assert concept_intro.index("Concept context") < concept_intro.index("Research question:")
        assert "Probe fixture hard boundary" in baseline_runner.codec.system_prompt()
        assert "codey" not in _debrand(concept_intro).lower()
        assert "codey" not in _debrand(
            "Your last reply did not satisfy Codey's Research tool contract.\n"
            "Codey Research executes exactly one action per turn."
        ).lower()
        store.close()

    results = FixtureSearchProvider(bridge_case).search("helium supply risk")
    assert any(row["url"] == HELIUM_URL for row in results)
    assert not any(
        row["url"] == COPPER_URL for row in FixtureSearchProvider(control_case).search("helium hubs")
    )
    bridge_search = FixtureSearchProvider(bridge_case)
    assert not any(
        row["url"] == BRIDGE_URL
        for row in bridge_search.search("helium supply wartime export disruption")
    ), "bridge doc must stay invisible to helium-only searches"
    assert any(
        row["url"] == BRIDGE_URL for row in bridge_search.search("copper supply war")
    ), "bridge doc must surface for copper-driven searches"

    bridge_provider = ScriptedProvider(
        json.dumps({"tool": "web_search", "args": {"query": "helium supply risk"}}),
        json.dumps({"tool": "open_url", "args": {"url": HELIUM_URL}}),
        json.dumps({"tool": "open_url", "args": {"url": BRIDGE_URL}}),
        json.dumps({
            "tool": "knowledge_write",
            "args": {
                "type": "fact",
                "title": "Helium supply risk and shared copper corridor",
                "body": (
                    "Helium supply faces wartime export disruption, and copper and "
                    "helium shipments share one sanctioned wartime corridor."
                ),
                "sources": [HELIUM_URL, BRIDGE_URL],
                "relations": [
                    {"src": "copper supply", "dst": "helium supply", "kind": "relates"}
                ],
                "evidence": [
                    {
                        "claim": "War closed major helium extraction hubs.",
                        "source_url": HELIUM_URL,
                        "excerpt": HELIUM_EVIDENCE,
                        "stance": "supports",
                    },
                    {
                        "claim": "Copper and helium share one wartime logistics corridor.",
                        "source_url": BRIDGE_URL,
                        "excerpt": (
                            "Copper and helium shipments now share the same sanctioned "
                            "wartime logistics corridor"
                        ),
                        "stance": "supports",
                    },
                ],
            },
        }),
        json.dumps({"tool": "done", "args": {"answer": _SELF_TEST_REPORT_BRIDGE}}),
    )
    row = run_case(bridge_provider, "scripted", bridge_case, "concept", max_turns=8)
    assert row["final_done"], row["stop_reason"]
    assert row["opened_primary_source"]
    assert row["opened_bridge_source"]
    assert row["target_fact_reported"]
    assert row["saved_exact_evidence_snippet"]
    assert row["bridge_fact_reported"]
    assert row["bridge_evidence_saved"]
    assert row["bridge_relation_declared"]
    assert row["bridge_found_and_supported"]
    assert not row["false_bridge_relation"]
    assert row["concept_block_chars"] > 0
    assert row["nonfixture_open_count"] == 0

    control_provider = ScriptedProvider(
        json.dumps({"tool": "web_search", "args": {"query": "helium supply risk"}}),
        json.dumps({"tool": "open_url", "args": {"url": HELIUM_URL}}),
        json.dumps({
            "tool": "knowledge_write",
            "args": {
                "type": "fact",
                "title": "Helium supply risk",
                "body": "Helium supply faces wartime export disruption.",
                "sources": [HELIUM_URL],
                "relations": [
                    {"src": "copper supply", "dst": "helium supply", "kind": "relates"}
                ],
                "evidence": {
                    "claim": "War closed major helium extraction hubs.",
                    "source_url": HELIUM_URL,
                    "excerpt": HELIUM_EVIDENCE,
                    "stance": "supports",
                },
            },
        }),
        json.dumps({"tool": "done", "args": {"answer": _SELF_TEST_REPORT_CONTROL}}),
    )
    row = run_case(control_provider, "scripted", control_case, "concept", max_turns=8)
    assert row["final_done"], row["stop_reason"]
    assert row["false_bridge_relation"], row["declared_relation_pairs"]

    summary = _summarize([
        {"arm": "baseline", "bridge_applicable": True, "final_done": True, "turns_used": 5},
        {"arm": "concept", "bridge_applicable": True, "final_done": True,
         "bridge_found_and_supported": True, "turns_used": 6},
    ])
    assert summary["concept_delta_vs_baseline"]["bridge_found_and_supported_rate"] == 1.0

    print("self-test passed")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    choices = (*provider_ids(), "all")
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=choices, help="provider to test")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--arm", action="append", default=[], help="arm or comma list; default both")
    parser.add_argument("--case", action="append", default=[], help="case name; default all cases")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-send timeout seconds")
    parser.add_argument("--new-chat-timeout", type=float, default=45.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--open-if-missing",
        action="store_true",
        help="allow the probe to open or foreground a provider page; default only attaches",
    )
    parser.add_argument("--no-live-trace", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.provider:
        raise SystemExit("--provider is required unless --self-test is used")

    arms: list[str] = []
    for value in args.arm:
        for item in str(value or "").split(","):
            item = item.strip()
            if item and item not in arms:
                arms.append(item)
    invalid = [arm for arm in arms if arm not in ARMS]
    if invalid:
        raise SystemExit(f"unknown arm(s): {', '.join(invalid)}")
    selected_arms = tuple(arms) if arms else ARMS

    cases = CASES
    if args.case:
        wanted = set(args.case)
        cases = tuple(case for case in CASES if case.name in wanted)
        missing = wanted - {case.name for case in cases}
        if missing:
            raise SystemExit(f"unknown case(s): {', '.join(sorted(missing))}")

    provider_list = provider_ids() if args.provider == "all" else (args.provider,)
    trace = None
    if not args.no_live_trace:
        trace_path = args.output.with_name(f"{args.output.stem}.trace.json")
        trace = LiveTrace(trace_path)

    provider_results = []
    all_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    for provider_id in provider_list:
        result = run_provider(
            provider_id,
            port=args.port,
            open_if_missing=args.open_if_missing,
            arms=selected_arms,
            cases=cases,
            max_turns=args.max_turns,
            timeout=args.timeout,
            new_chat_timeout=args.new_chat_timeout,
            trace=trace,
        )
        provider_results.append(result)
        all_rows.extend(result["rows"])
    payload = {
        "probe": "concept_context_ab",
        "providers": list(provider_list),
        "arms": list(selected_arms),
        "cases": [case.name for case in cases],
        "max_turns": args.max_turns,
        "open_if_missing": args.open_if_missing,
        "trace_output": str(trace.path) if trace is not None else "",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "summary": _summarize(all_rows),
        "provider_results": provider_results,
    }
    _write_json_atomic(args.output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=True, indent=2))
    print(f"report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())