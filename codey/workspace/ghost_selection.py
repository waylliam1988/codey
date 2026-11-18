"""Pure, atomic selection of already-authorized Ghost source candidates."""
from __future__ import annotations

from dataclasses import replace

from codey.ghost.observation_index import history_refs, select_history_text
from codey.workspace.context_admission import GHOST_TOTAL_BUDGET_CHARS, GhostContextSource

# Character cap for the combined local projection, not a model/output gate.
# This bounds worst-case composition; it is not claimed to be optimal.
_GOALS = frozenset({"ghost_project_goal_history", "ghost_confirmed_project_goal"})


def select_ghost_sources(sources: tuple[GhostContextSource, ...], *,
                         represented: frozenset[str] = frozenset(),
                         budget: int = GHOST_TOTAL_BUDGET_CHARS) -> tuple[GhostContextSource, ...]:
    """Preserve source caps and goal pairs, charging actual text and separators.

    Reviewed requirements have priority over repeated historical material.
    No scope/deletion authority is granted here; existing validators and the
    outbound admission gate still own those decisions.
    """
    chosen: list[GhostContextSource] = []
    used = 0
    seen_refs = set(represented)
    goals = tuple(source for source in sources if source.key in _GOALS)
    groups = [tuple(source for source in sources if source.key == "ghost_directive"), goals]
    groups.extend((source,) for source in sources if source.key != "ghost_directive" and source.key not in _GOALS)
    for group in groups:
        if not group or (any(source.key in _GOALS for source in group) and {s.key for s in group} != _GOALS):
            continue
        selected: list[GhostContextSource] = []
        for source in group:
            room = min(source.budget, budget - used - (2 if chosen else 0))
            text = source.text
            if source.key == "ghost_experiences":
                text = select_history_text(text, represented=frozenset(seen_refs), budget=room)
            if text and len(text) <= room:
                selected.append(replace(source, text=text))
        if len(selected) != len(group):
            continue
        cost = sum(len(source.text) for source in selected) + 2 * (len(selected) - 1 + bool(chosen))
        if used + cost > budget:
            continue
        chosen.extend(selected)
        used += cost
        for source in selected:
            seen_refs.update(history_refs(source.text))
    return tuple(chosen)


def replace_ghost_text(text: str, original: tuple[GhostContextSource, ...],
                       selected: tuple[GhostContextSource, ...]) -> str:
    """Replace frozen blocks before prompt/request freezing, never on the wire."""
    kept = {source.key: source.text for source in selected}
    for source in original:
        if source.text:
            text = text.replace(source.text, kept.get(source.key, ""))
    return text
