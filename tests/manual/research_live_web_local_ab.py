"""Real-network Research A/B through the current local production entry."""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from codey.research import connector_search as production
from codey.runtime.core import cancellation
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


class BrowserAfterConnector(production.ConnectorAwareSearchProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = {"connector_calls": 0, "browser_calls": 0}

    def _connector_search_results(self, *args, **kwargs):
        self.observations["connector_calls"] += 1
        return super()._connector_search_results(*args, **kwargs)

    def _base_search(self, *args, **kwargs):
        self.observations["browser_calls"] += 1
        return super()._base_search(*args, **kwargs)

    def search(self, query: str, limit: int = 8):  # type: ignore[no-untyped-def]
        query = " ".join((query or "").split())
        if not query:
            return []
        result_limit = production._bounded_limit(limit, default=8, upper=production.MAX_CONNECTOR_HITS)
        self.last_search_failures = []
        self.last_search_failure = {}
        connector_rows = self._connector_search_results(
            query, limit=min(self.connector_limit, result_limit),
            safe_query=production.safe_connector_query(query, limit=12),
        )
        try:
            browser_rows = self._base_search(query, limit=result_limit)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            if not connector_rows:
                raise
            browser_rows = []
        return production._merge_results(connector_rows, browser_rows, limit=result_limit)


class ObservedProduction(production.ConnectorAwareSearchProvider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observations = {"connector_calls": 0, "browser_calls": 0}

    def _connector_search_results(self, *args, **kwargs):
        self.observations["connector_calls"] += 1
        return super()._connector_search_results(*args, **kwargs)

    def _base_search(self, *args, **kwargs):
        self.observations["browser_calls"] += 1
        return super()._base_search(*args, **kwargs)


def run(root: Path, case: str, arm: str, seed: int, base_url: str, model: str,
        *, constrained_text: bool = False) -> None:
    with urlopen(base_url + "/api/extra/perf", timeout=10) as response:
        perf = json.load(response)
    if perf.get("idle") != 1 or perf.get("queue") != 0:
        raise RuntimeError("Backend is busy; do not queue another experimental run")
    directory = root / f"{case}-{seed}-{arm}"
    directory.mkdir(parents=True, exist_ok=False)

    holder = {}

    def provider_factory():
        from codey.research.browser_search import BrowserSearchProvider

        base = BrowserSearchProvider(isolated=False, bring_to_front=False)
        provider = ObservedProduction(base) if arm == "baseline" else BrowserAfterConnector(base)
        holder["provider"] = provider
        return provider

    with ExitStack() as stack:
        from codey.providers import local_config

        save_config = local_config.save_local_config

        def save_with_output_budget(config):
            context = config.context or local_config.LocalContextBudget(
                32768, OUTPUT_TOKENS, 12000, source="2a-live-contract")
            return save_config(replace(config, context=context))

        stack.enter_context(patch.object(local_config, "save_local_config", save_with_output_budget))
        stack.enter_context(patch("codey.operations.task_execution.default_research_search_provider", provider_factory, create=True))
        stack.enter_context(patch("codey.operations.task_phases.dispatch.default_research_search_provider", provider_factory))
        old_context = ssl._create_default_https_context
        ssl._create_default_https_context = ssl._create_unverified_context
        started = time.monotonic()
        try:
            stage_module.stage(directory, "research", base_url, model, native=False, live_web=True,
                               question=QUESTIONS[case], max_turns=12, seed=seed, provider_timeout=180,
                               constrained_text=constrained_text)
        finally:
            ssl._create_default_https_context = old_context
            observations = holder["provider"].observations if "provider" in holder else {}
            (directory / "search-observations.json").write_text(
                json.dumps({**observations, "elapsed_seconds": time.monotonic() - started,
                            "case": case, "arm": arm, "seed": seed, "model": model,
                            "constrained_text": constrained_text,
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
    with urlopen(args.base_url + "/v1/models", timeout=5) as response:
        model = json.load(response)["data"][0]["id"]
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
