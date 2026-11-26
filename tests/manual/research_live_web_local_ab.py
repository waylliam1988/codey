"""Real-network Research A/B through the current local production entry."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import ssl
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack
from dataclasses import replace
from http.client import HTTPResponse
from pathlib import Path
from typing import ParamSpec, Protocol
from unittest.mock import patch
from urllib.request import Request, urlopen

from codey.providers import api_provider
from codey.providers.api_provider import ApiProvider
from codey.providers.local_config import LocalProviderConfig
from codey.research import connector_search as production
from codey.research.browser_search import BrowserSearchProvider
from codey.research.search_contracts import SearchOutcome
from codey.research.web_sources import WebSourceProvider
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    encode_transport_value,
    parse_json_object,
    require_json_string,
)
from tests.manual import persistent_research_local as stage_module

ARMS = ("baseline", "browser_after_connector")
OUTPUT_TOKENS = 8192
QUESTIONS = {
    "pubmed_hepatotoxicity": "Research current PubMed evidence on immune checkpoint inhibitor hepatotoxicity management. Read original sources, cite exact passages, preserve limitations, and answer in Chinese.",
    "pubmed_myocarditis": "Research PubMed evidence on checkpoint inhibitor myocarditis management. Read original sources and provide a concise cited answer with uncertainty.",
    "arxiv_rag": "Research arXiv evidence about retrieval augmented generation evaluation methods. Read original papers, cite them, and preserve what the papers do not establish.",
    "arxiv_long_context": "Research arXiv evidence about long context language model benchmarks. Read original papers, compare evaluation limits, and cite exact evidence.",
    "pubmed_cholangitis": "Research PubMed evidence on immune checkpoint inhibitor associated cholangitis. Read original sources and preserve evidence limits.",
    "pubmed_dermatitis": "Research PubMed evidence on checkpoint inhibitor dermatologic adverse event management. Cite original sources and uncertainty.",
    "pubmed_endocrine": "Research PubMed evidence on endocrine immune related adverse events from checkpoint inhibitors. Read and cite original evidence.",
    "pubmed_nephritis": "Research PubMed evidence on immune checkpoint inhibitor nephritis management. Preserve scope and limitations.",
    "arxiv_agents": "Research arXiv evidence about tool using language model agents and evaluation. Read original papers and cite limits.",
    "arxiv_uncertainty": "Research arXiv evidence about uncertainty calibration for language models. Compare methods and limitations.",
    "arxiv_multimodal": "Research arXiv evidence about multimodal retrieval benchmarks. Read original papers and preserve dataset scope.",
    "arxiv_database": "Research arXiv evidence about database question answering with retrieval. Cite original evidence and unresolved points.",
}


def apply_experiment_output_budget(config: LocalProviderConfig) -> LocalProviderConfig:
    from codey.providers import local_config

    context = config.context or local_config.LocalContextBudget(
        32768, OUTPUT_TOKENS, 12000, source="2a-live-contract")
    return replace(config, context=replace(context, context_reserve_tokens=OUTPUT_TOKENS,
                                           source="2a-live-contract"))


class ObservedProduction(WebSourceProvider):
    """Observe the canonical coordinator; historical arm labels share adopted behavior."""
    def __init__(self, base: BrowserSearchProvider, *, connector_ids: tuple[str, ...] = production.CONNECTOR_SEARCH_IDS) -> None:
        self.base = base
        super().__init__(browser_factory=lambda: base.browser_source,
                         scholarly=production.ScholarlySources(connector_ids=connector_ids))
        self.observations: JsonObject = {"connector_calls": 0, "browser_calls": 0}

    def search(self, query: str, *, limit: int = 8) -> SearchOutcome:
        outcome = super().search(query, limit=limit)
        self.observations = {
            "connector_calls": sum(attempt.coverage == "scholarly" for attempt in outcome.attempts),
            "browser_calls": sum(attempt.engine in {"bing", "duckduckgo"} for attempt in outcome.attempts),
            "attempts": encode_transport_value(outcome.attempts)}
        return outcome

    def close(self) -> None:
        super().close()
        self.base.close()


BrowserAfterConnector = ObservedProduction


P = ParamSpec("P")


def _with_request_configuration(factory: Callable[P, ApiProvider], adjust: Callable[[dict[str, object]], None]) -> Callable[P, ApiProvider]:
    def create(*args: P.args, **kwargs: P.kwargs) -> ApiProvider:
        provider = factory(*args, **kwargs)
        original = provider.configure_request
        def configure(payload: dict[str, object]) -> None:
            original(payload)
            adjust(payload)
        provider.configure_request = configure
        return provider
    return create


class _ResearchStage(Protocol):
    def __call__(self, directory: Path, name: str, upstream: str, model: str, *, native: bool,
                 live_web: bool, question: str, max_turns: int, seed: int, provider_timeout: float,
                 constrained_text: bool) -> None: ...


def run(root: Path, case: str, arm: str, seed: int, base_url: str, model: str,
        *, constrained_text: bool = False) -> None:
    opened: AbstractContextManager[HTTPResponse] = urlopen(base_url + "/api/extra/perf", timeout=10)
    with opened as response:
        perf = parse_json_object(response.read().decode("utf-8"))
    if perf.get("idle") != 1 or perf.get("queue") != 0:
        raise RuntimeError("Backend is busy; do not queue another experimental run")
    directory = root / f"{case}-{seed}-{arm}"
    directory.mkdir(parents=True, exist_ok=False)

    holder: dict[str, ObservedProduction] = {}
    compiled_schemas: dict[str, JsonObject] = {}

    def zero_reasoning(payload: dict[str, object]) -> None:
        payload["reasoning_effort"] = "none"
        raw_format = payload.get("response_format")
        if not isinstance(raw_format, dict):
            return
        response_format = decode_json_object(raw_format)
        if response_format.get("type") == "json_schema":
            schema = decode_json_object(response_format["json_schema"])["schema"]
            digest = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()
            if digest not in compiled_schemas:
                request = Request(base_url + "/api/extra/json_to_grammar", json.dumps({"schema": schema}).encode(),
                                  {"Content-Type": "application/json"})
                opened: AbstractContextManager[HTTPResponse] = urlopen(request, timeout=10)
                with opened as response:
                    compilation = parse_json_object(response.read().decode("utf-8"))
                grammar = require_json_string(compilation.get("result", ""), "grammar")
                if not compilation.get("success") or not grammar:
                    raise RuntimeError("Backend discarded experimental tool schema; stop before inference")
                compiled_schemas[digest] = {"grammar_hash": hashlib.sha256(grammar.encode()).hexdigest(),
                                           "grammar_length": len(grammar)}

    configured_provider = _with_request_configuration(api_provider.ApiProvider, zero_reasoning)

    def provider_factory() -> ObservedProduction:
        from codey.research.browser_search import BrowserSearchProvider

        base = BrowserSearchProvider(isolated=False, bring_to_front=False)
        provider = ObservedProduction(base) if arm == "baseline" else BrowserAfterConnector(base)
        holder["provider"] = provider
        return provider

    with ExitStack() as stack:
        from codey.providers import local_config

        save_config = local_config.save_local_config

        def save_with_output_budget(config: LocalProviderConfig) -> None:
            return save_config(apply_experiment_output_budget(config))

        stack.enter_context(patch.object(local_config, "save_local_config", save_with_output_budget))
        stack.enter_context(patch.object(api_provider, "ApiProvider", configured_provider))
        stack.enter_context(patch("codey.operations.task_execution.default_research_search_provider", provider_factory, create=True))
        stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", provider_factory))
        stack.enter_context(patch.object(ssl, "_create_default_https_context", ssl._create_unverified_context))
        started = time.monotonic()
        try:
            stage: _ResearchStage = stage_module.stage
            stage(directory, "research", base_url, model, native=False, live_web=True,
                               question=QUESTIONS[case], max_turns=12, seed=seed, provider_timeout=180,
                               constrained_text=constrained_text)
        finally:
            observations = holder["provider"].observations if "provider" in holder else {}
            (directory / "search-observations.json").write_text(
                json.dumps({**observations, "elapsed_seconds": time.monotonic() - started,
                            "case": case, "arm": arm, "seed": seed, "model": model,
                            "constrained_text": constrained_text,
                            "reasoning_effort": "none",
                            "compiled_tool_schemas": compiled_schemas,
                            "output_tokens": OUTPUT_TOKENS}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(QUESTIONS), required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001")
    parser.add_argument("--constrained-text", action="store_true")
    args = parser.parse_args()
    opened: AbstractContextManager[HTTPResponse] = urlopen(args.base_url + "/v1/models", timeout=5)
    with opened as response:
        models = parse_json_object(response.read().decode("utf-8"))["data"]
    if not isinstance(models, list) or not models:
        raise ValueError("local endpoint did not advertise a model")
    model = require_json_string(decode_json_object(models[0])["id"], "model id")
    # A process-scoped socket releases automatically on failure or cancellation.
    # All live runs using this harness must share this single-writer lock.
    with socket.socket() as lock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        lock.bind(("127.0.0.1", 15001))
        run(args.root.resolve(), args.case, args.arm, args.seed, args.base_url, model,
            constrained_text=args.constrained_text)
    print(json.dumps({"case": args.case, "arm": args.arm, "seed": args.seed}, ensure_ascii=False))


if __name__ == "__main__":
    main()
