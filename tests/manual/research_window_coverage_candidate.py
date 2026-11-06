"""Coverage-preserving research context candidate; experiment-only."""
from __future__ import annotations

import re

from codey.research import answer_context as production
from tests.manual.research_window_retention_candidate import groups


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z][a-z0-9_-]*|\d+(?:\.\d+)*", text.lower()))


def _facet_terms(task: str) -> tuple[set[str], set[str], set[str]]:
    terms = _terms(task)
    version = {t for t in terms if re.fullmatch(r"v\d+|\d{4}|version", t)}
    limits = terms & {"limit", "limits", "limitation", "limitations", "exception", "exceptions", "only", "scope", "boundary"}
    objects = terms - version - limits
    return objects, version, limits


def _coverage_order(session, ledger, scope):
    windows = groups(session, scope)
    objects, versions, limits = _facet_terms(session.task_text)
    covered = [set() for _ in windows]
    for i, window in enumerate(windows):
        body = "\n".join(ledger.read_passages[ref].excerpt.lower() for ref in window)
        title = "\n".join(ledger.read_passages[ref].source.title.lower() for ref in window)
        terms = _terms(body + " " + title)
        if terms & objects:
            covered[i].add("object")
        if terms & versions:
            covered[i].add("version")
        if terms & limits:
            covered[i].add("limit")
    all_facets = {"object", "version", "limit"}
    order = {ref: i for i, (_, ref) in enumerate(scope.aliases)}
    selected: set[str] = set()
    remaining = set(all_facets)
    ranked = []
    while remaining:
        candidates = [i for i, facets in enumerate(covered) if i not in selected]
        if not candidates:
            break
        best = max(candidates, key=lambda i: (len(covered[i] & remaining), -min(order[r] for r in windows[i])))
        selected.add(best)
        remaining -= covered[best]
        ranked.append(best)
    ranked.extend(i for i in sorted(range(len(windows)), key=lambda j: min(order[r] for r in windows[j])) if i not in selected)
    return [windows[i] for i in ranked]


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
