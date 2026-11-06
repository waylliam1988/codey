"""Real-network 2A A/B with the local KoboldCPP writer.

The baseline uses the production connector early stop.  The candidate invokes
the existing browser provider after connector hits and keeps production merge
and deduplication.  This harness is experimental and never changes the
default provider factory.
"""

from __future__ import annotations

import argparse
import json
import ssl
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from codey.research import connector_search as production
from tests.manual import source_connector_ab as live

ARMS = ("baseline", "browser_after_connector")


def formal_cases() -> tuple[live.Case, ...]:
    rows: list[live.Case] = []
    queries = (
        ("pubmed", "immune checkpoint inhibitor hepatotoxicity management", ("hepatotoxicity", "checkpoint")),
        ("pubmed", "randomized trial immune therapy liver injury", ("randomized", "liver")),
        ("pubmed", "checkpoint inhibitor myocarditis clinical review", ("checkpoint", "myocarditis")),
        ("pubmed", "drug induced liver injury oncology biomarkers", ("drug", "liver")),
        ("pubmed", "immunotherapy adverse event treatment guideline", ("immunotherapy", "treatment")),
        ("pubmed", "cancer immunotherapy cholangitis evidence", ("cancer", "immunotherapy")),
        ("arxiv", "retrieval augmented generation evaluation methods", ("retrieval", "generation")),
        ("arxiv", "long context language model benchmark", ("long", "context")),
        ("arxiv", "agent tool use planning evaluation", ("agent", "evaluation")),
        ("arxiv", "multimodal retrieval benchmark", ("multimodal", "retrieval")),
        ("arxiv", "uncertainty calibration language models", ("uncertainty", "calibration")),
        ("arxiv", "database question answering retrieval", ("database", "retrieval")),
    )
    for index, (family, query, terms) in enumerate(queries, 1):
        host = "pubmed.ncbi.nlm.nih.gov" if family == "pubmed" else "arxiv.org"
        rows.append(live.Case(f"formal_{index:02d}",
                              f"Research {query}. Use opened original sources, preserve source scope and limitations, "
                              "then produce a concise cited report. Do not claim that result count proves quality.",
                              (host,), terms))
    return tuple(rows)


class BrowserAfterConnector(production.ConnectorAwareSearchProvider):
    def search(self, query: str, limit: int = 8):  # type: ignore[no-untyped-def]
        query = " ".join((query or "").split())
        if not query:
            return []
        result_limit = production._bounded_limit(limit, default=8, upper=production.MAX_CONNECTOR_HITS)
        connector_rows = self._connector_search_results(query, limit=min(self.connector_limit, result_limit))
        browser_rows = self._base_search(query, limit=result_limit)
        return production._merge_results(connector_rows, browser_rows, limit=result_limit)


@contextmanager
def experiment_patch():
    def provider_for_arm(arm: str):
        base = live.BrowserSearchProvider(isolated=False, bring_to_front=False)
        if arm == "baseline":
            return production.ConnectorAwareSearchProvider(base)
        if arm == "browser_after_connector":
            return BrowserAfterConnector(base)
        raise ValueError(arm)

    # The execution environment presents a local inspection CA. This scope is
    # limited to the experiment's connector HTTP calls; production TLS policy
    # is untouched and every raw response remains in the live trace.
    with ExitStack() as stack:
        stack.enter_context(patch.object(live, "_search_provider_for_arm", provider_for_arm))
        stack.enter_context(patch.object(live, "WEB_PROVIDERS", ("local",)))
        old_context = ssl._create_default_https_context
        ssl._create_default_https_context = ssl._create_unverified_context
        try:
            yield
        finally:
            ssl._create_default_https_context = old_context


def run(output: Path, *, cases: tuple[live.Case, ...], arms: tuple[str, ...]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with experiment_patch():
        live.run_provider("local", cases=cases, arms=arms, port=9222, output=output,
                          max_turns=18, send_timeout=180, new_chat_timeout=60,
                          open_if_missing=False, rerun_failed=True, trace=None,
                          run_id=output.stem)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--arm", action="append", choices=ARMS)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert len(formal_cases()) == 12
        print("self-test ok")
        return
    selected = formal_cases()
    if args.case:
        wanted = set(args.case)
        selected = tuple(case for case in selected if case.name in wanted)
    run(args.output, cases=selected, arms=tuple(args.arm or ARMS))
    print(json.dumps({"output": str(args.output), "cases": len(selected), "arms": list(args.arm or ARMS)}))


if __name__ == "__main__":
    main()
