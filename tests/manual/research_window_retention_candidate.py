"""Experimental lexical window retention. Never imported by production."""

from __future__ import annotations

import math
import re

from codey.research import answer_context as production

STOP = {"the", "and", "for", "with", "that", "this", "from", "what", "when", "does", "are", "can", "only"}


def terms(text):
    result = set(re.findall(r"[a-z][a-z0-9_-]*|\d+(?:\.\d+)*", text.lower())) - STOP
    for chinese in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(chinese[i:i + 2] for i in range(len(chinese) - 1))
    return result


def groups(session, scope):
    """Connected observed windows stay whole, including overlapping rereads."""
    allowed = {ref for _, ref in scope.aliases}
    windows = []
    for result in session._memory_results.values():
        if not result.ok:
            continue
        refs = {row["ref"] for row in result.canonical.get("read_passages", [])} & allowed
        if not refs:
            continue
        overlap = [window for window in windows if window & refs]
        for window in overlap:
            refs |= window
            windows.remove(window)
        windows.append(refs)
    seen = set().union(*windows) if windows else set()
    windows.extend({ref} for _, ref in scope.aliases if ref not in seen)
    order = {ref: i for i, (_, ref) in enumerate(scope.aliases)}
    return sorted(windows, key=lambda window: min(order[ref] for ref in window))


def eviction_order(session, ledger, scope):
    windows = groups(session, scope)
    query = terms(session.task_text)
    documents, bodies = [], []
    for window in windows:
        passages = [ledger.read_passages[ref] for _, ref in scope.aliases if ref in window]
        body = "\n".join(p.excerpt for p in passages)
        bodies.append(" ".join(body.split()))
        documents.append(terms(body + "\n" + "\n".join(p.source.title for p in passages)))
    frequencies = {term: sum(term in doc for doc in documents) for term in query}
    scores = [sum(1 + math.log((len(documents) + 1) / (frequencies[term] + 1))
                  for term in query & doc) for doc in documents]
    # Identical observed bodies add no new text. Keep the most recent copy;
    # nothing about its authority, truth or citation rights follows from this.
    duplicate = [body in bodies[i + 1:] for i, body in enumerate(bodies)]
    return [windows[i] for i in sorted(range(len(windows)), key=lambda i: (not duplicate[i], scores[i], i))]


def answer_context(session, ledger, scope, *, count_tokens=production.estimate_tokens, token_budget=16384):
    if token_budget <= 0:
        raise ValueError("research work view needs a positive token budget")
    visible = scope.aliases
    text = production._render_context(session, ledger, scope, visible)
    tokens = count_tokens(text)
    if tokens <= token_budget:
        return text  # Exact production rendering, without scoring when all fit.
    for remove in eviction_order(session, ledger, scope):
        visible = tuple(row for row in visible if row[1] not in remove)
        text = production._render_context(session, ledger, scope, visible)
        tokens = count_tokens(text)
        if tokens <= token_budget:
            return text
    raise ValueError("non-evidence research work view exceeds the token budget")
