"""Coverage-preserving research context candidate; experiment-only."""
from __future__ import annotations

import re

from codey.research import answer_context as production
from tests.manual.research_window_retention_candidate import groups


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_-]*|\d+(?:\.\d+)*", text.lower()))


def _facet_terms(task: str) -> tuple[set[str], set[str], set[str]]:
    terms = _terms(task)
    version = {t for t in terms if re.fullmatch(r"v\d+|\d{4}", t)}
    limits = terms & {"limit", "limits", "limitation", "limitations", "exception", "exceptions", "only", "scope", "boundary"}
    objects = terms - version - limits
    return objects, version, limits


def _required_facets(task: str) -> set[str]:
    objects, versions, limits = _facet_terms(task)
    required = {f"version:{term}" for term in versions}
    if limits:
        required.add("limit")
    if objects:
        required.add("object")
    if any(marker in task.casefold() for marker in ("source", "来源", "分别", "各自", "对照")):
        required.add("source")
    return required


def _coverage_order(session, ledger, scope):
    windows = groups(session, scope)
    objects, versions, limits = _facet_terms(session.task_text)
    required = _required_facets(session.task_text)
    source_required = "source" in required
    required.discard("source")
    if source_required:
        required.update(f"source:{i}" for i in range(len(windows)))
    covered = [set() for _ in windows]
    for i, window in enumerate(windows):
        body = "\n".join(ledger.read_passages[ref].excerpt.lower() for ref in window)
        title = "\n".join(ledger.read_passages[ref].source.title.lower() for ref in window)
        terms = _terms(body + " " + title)
        if terms & objects:
            covered[i].add("object")
        covered[i].update(f"version:{term}" for term in terms & versions)
        if terms & limits or "limit" in required and terms & {
            "requires", "required", "unavailable", "only", "not", "rule", "policy",
            "condition", "conditions", "offline", "measurement", "does",
        }:
            covered[i].add("limit")
        if source_required:
            covered[i].add(f"source:{i}")
    order = {ref: i for i, (_, ref) in enumerate(scope.aliases)}
    # Return *eviction* order.  A window is removable first when dropping it
    # leaves every required object/version/limit represented elsewhere.
    remaining = set(range(len(windows)))
    result = []
    while remaining:
        removable = [i for i in remaining if required <= set().union(*(covered[j] for j in remaining if j != i))]
        candidates = removable or list(remaining)
        def key(i):
            overlap = sum(bool(covered[i] & covered[j]) for j in remaining if j != i)
            return (len(covered[i] & required), -overlap, min(order[r] for r in windows[i]))
        chosen = min(candidates, key=key)
        result.append(windows[chosen])
        remaining.remove(chosen)
    return result


def answer_context(session, ledger, scope, *, count_tokens=production.estimate_tokens, token_budget=16384):
    if token_budget <= 0:
        raise ValueError("research work view needs a positive token budget")
    visible = scope.aliases
    text = production._render_context(session, ledger, scope, visible)
    if count_tokens(text) <= token_budget:
        return text
    for remove in _coverage_order(session, ledger, scope):
        visible = tuple(row for row in visible if row[1] not in remove)
        text = production._render_context(session, ledger, scope, visible)
        if count_tokens(text) <= token_budget:
            return text
    raise ValueError("coverage-preserving research view exceeds token budget")
