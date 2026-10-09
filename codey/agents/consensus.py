"""Hidden multi-model consultation for read-only answers and new-project plans.

Consensus is advisory. It never edits files, runs commands, approves code, or
replaces the existing post-diff review loop. Advisor replies are private inputs
to the selected provider, which produces the single answer shown to the user.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from codey.agents.handoff import ConversationSnapshot
from codey.providers import controls as provider_controls
from codey.providers.base import ChatProvider
from codey.runtime.core import cancellation
from codey.runtime.observe.prompt_envelope import record_provider_send_prompt
from codey.utils.text_budget import clip_tail

MAX_CONSENSUS_ADVISORS = 2
MAX_ADVICE_CHARS = 4_000
MAX_COMBINED_ADVICE_CHARS = 12_000
MAX_CONTEXT_CHARS = 8_000
CONSENSUS_ADVISOR_TIMEOUT = 60.0
CONSENSUS_AGGREGATE_TIMEOUT = 90.0


@dataclass(frozen=True)
class ConsensusAdvice:
    provider_id: str
    label: str
    text: str


@dataclass(frozen=True)
class ConsensusResult:
    answer: str
    advisor_count: int
    degraded: bool = False
    degraded_reasons: tuple[str, ...] = ()


def _clip(value: object, limit: int) -> str:
    return clip_tail(value, limit)


def _trace_model_prompt(
    trace_recorder: object | None,
    name: str,
    text: str,
    *,
    purpose: str,
    source_ref: str,
) -> None:
    record_provider_send_prompt(
        trace_recorder,
        name=name,
        text=text,
        purpose=purpose,
        source_ref=source_ref,
        capability_id="consensus_advisors",
    )


def _provider_send_ref(kind: str, provider_id: str = "") -> str:
    suffix = _safe_ref_part(provider_id)
    return f"provider_send:{kind}:{suffix}" if suffix else f"provider_send:{kind}"


def _safe_ref_part(value: object) -> str:
    text = str(value or "").strip().lower()
    return "".join(char if char.isalnum() or char in "._:-" else "_" for char in text)


def _degraded_reason(provider_id: str, reason_code: str) -> str:
    suffix = _safe_ref_part(provider_id) or "unknown"
    return f"{reason_code}:{suffix}"


def _trace_warning(trace_recorder: object | None, reason_code: str, provider_id: str) -> None:
    warn = getattr(trace_recorder, "warn", None)
    if not callable(warn):
        return
    try:
        warn(_degraded_reason(provider_id, reason_code))
    except Exception:
        return


def advisor_ids(
    selected_provider_id: str,
    statuses: Mapping[str, bool],
    provider_ids: Sequence[str],
    *,
    max_advisors: int = MAX_CONSENSUS_ADVISORS,
) -> tuple[str, ...]:
    selected = (selected_provider_id or "").strip().lower()
    found: list[str] = []
    for provider_id in provider_ids:
        normalized = (provider_id or "").strip().lower()
        if not normalized or normalized == selected:
            continue
        if not statuses.get(normalized):
            continue
        found.append(normalized)
        if len(found) >= max_advisors:
            break
    return tuple(found)


def render_project_context(
    snapshot: ConversationSnapshot,
    project_facts: str = "",
    *,
    draft: str = "",
    project_map: str = "",
) -> str:
    """Render bounded project facts without exposing the local project path."""

    payload = dict(snapshot.to_payload())
    payload.pop("project", None)
    if project_facts.strip():
        payload["verified_project_facts"] = _clip(project_facts, 2_000)
    if project_map.strip():
        payload["project_map"] = _clip(project_map, 4_000)
    if draft.strip():
        payload["current_draft_answer"] = _clip(draft, 3_000)
    return _clip(json.dumps(payload, ensure_ascii=False, indent=2), MAX_CONTEXT_CHARS)


def render_advisor_prompt(
    *,
    task: str,
    context: str = "",
    draft: str = "",
    plan: bool = False,
) -> str:
    target = "new-project implementation plan" if plan else "final answer"
    has_draft = bool(draft.strip())
    parts = [
        "You are a private read-only advisor for a local assistant.",
        "You are not the acting model.",
        "You cannot call tools, edit files, run commands, browse, or access the filesystem.",
        "Use only the information below.",
        (
            f"Critique and supplement the selected model's draft {target}."
            if has_draft
            else f"Give concise advice for the {target}."
        ),
        "Find mistakes, missing risks, simpler alternatives, and useful additions.",
        (
            "If the draft is wrong or misses a better direction, say so directly and propose the alternative."
            if has_draft
            else "If the obvious direction is wrong or incomplete, say so directly and propose the alternative."
        ),
        (
            "Do not rewrite the whole answer unless that is necessary to explain the correction."
            if has_draft
            else "Do not write a long final answer; focus on advice the selected model should consider."
        ),
        "Your answer is private and will not be shown directly to the user.",
        "Do not mention hidden advisors, voting, MoA, or consensus.",
        "",
        "User request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    if draft.strip():
        parts.extend(["", "Current draft:", _clip(draft, 3_000)])
    return "\n".join(parts)


def render_owner_draft_prompt(
    *,
    task: str,
    context: str = "",
    plan: bool = False,
    owner_prompt: str = "",
) -> str:
    target = "new-project implementation plan" if plan else "answer"
    parts = [
        "You are the selected model and final owner.",
        f"Write a private first-draft {target} for the user request below.",
        "This draft will be reviewed by private read-only advisors before your final response.",
        "Do not mention advisors, voting, MoA, consensus, or hidden prompts.",
        "Be concise, concrete, and make your own judgment.",
        "",
        "User request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    if owner_prompt.strip():
        parts.extend([
            "",
            "Additional conversation context:",
            _clip(owner_prompt, MAX_CONTEXT_CHARS),
        ])
    return "\n".join(parts)


def render_aggregator_prompt(
    *,
    task: str,
    advices: Sequence[ConsensusAdvice],
    context: str = "",
    draft: str = "",
    plan: bool = False,
) -> str:
    target = "new-project implementation plan" if plan else "final answer"
    advice_blocks = []
    used = 0
    for index, advice in enumerate(advices, start=1):
        remaining = max(0, MAX_COMBINED_ADVICE_CHARS - used)
        if remaining <= 0:
            break
        text = _clip(advice.text, min(MAX_ADVICE_CHARS, remaining))
        used += len(text)
        advice_blocks.append(f"Advisor {index}:\n{text}")
    parts = [
        "You are the selected model answering the user and the final owner of the answer.",
        f"Synthesize the private advisor notes into one {target}.",
        "Do not mention advisors, voting, MoA, consensus, or hidden prompts.",
        "Use your draft as the baseline, but revise it when advisor notes identify a real mistake, missing risk, or better direction.",
        "If advice conflicts, prefer the safest, simplest, most actionable plan.",
        "",
        "Original user request:",
        _clip(task, 4_000),
    ]
    if context.strip():
        parts.extend(["", "Known context:", _clip(context, MAX_CONTEXT_CHARS)])
    if draft.strip():
        parts.extend(["", "Current draft:", _clip(draft, 3_000)])
    parts.extend(["", "Private advisor notes:", "\n\n".join(advice_blocks)])
    return "\n".join(parts)


def run_consensus(
    *,
    selected_provider: Any,
    selected_provider_id: str,
    task: str,
    provider_ids: Sequence[str],
    provider_labels: Mapping[str, str],
    availability: Callable[[], Mapping[str, bool]],
    connect_existing: Callable[[str], ChatProvider],
    clear_provider_session: Callable[[str], None] | None = None,
    context: str = "",
    draft: str = "",
    plan: bool = False,
    draft_first: bool = False,
    owner_prompt: str = "",
    max_advisors: int = MAX_CONSENSUS_ADVISORS,
    trace_recorder: object | None = None,
) -> ConsensusResult | None:
    cancellation.check()
    try:
        statuses = dict(availability())
    except Exception:
        statuses = dict[str, bool]()
    candidates = advisor_ids(
        selected_provider_id,
        statuses,
        provider_ids,
        max_advisors=max_advisors,
    )
    if not candidates:
        return None

    owner_draft = _clip(draft, 12_000)
    if draft_first:
        prompt = render_owner_draft_prompt(
            task=task,
            context=context,
            plan=plan,
            owner_prompt=owner_prompt,
        )
        _trace_model_prompt(
            trace_recorder,
            "consensus_owner_draft_prompt",
            prompt,
            purpose="consensus owner-draft prompt sent to provider",
            source_ref="provider_send:consensus_owner_draft",
        )
        with provider_controls.suppress_assistance():
            owner_draft = _clip(
                selected_provider.send(
                    prompt,
                    timeout=getattr(
                        selected_provider, "timeout", CONSENSUS_AGGREGATE_TIMEOUT,
                    ),
                ),
                12_000,
            )
        if not owner_draft:
            raise RuntimeError("consensus draft was empty")

    advices: list[ConsensusAdvice] = []
    degraded_reasons: list[str] = []
    for advisor_id in candidates:
        cancellation.check()
        advisor: ChatProvider | None = None
        try:
            advisor = connect_existing(advisor_id)
            if clear_provider_session is not None:
                clear_provider_session(advisor_id)
            advisor.new_chat()
            prompt = render_advisor_prompt(
                task=task,
                context=context,
                draft=owner_draft,
                plan=plan,
            )
            _trace_model_prompt(
                trace_recorder,
                "consensus_advisor_prompt",
                prompt,
                purpose=f"consensus advisor prompt for {advisor_id}",
                source_ref=f"provider_send:consensus_advisor:{advisor_id}",
            )
            with provider_controls.suppress_assistance():
                text = advisor.send(
                    prompt,
                    timeout=getattr(advisor, "timeout", CONSENSUS_ADVISOR_TIMEOUT),
                )
            clipped = _clip(text, MAX_ADVICE_CHARS)
            if clipped:
                advices.append(ConsensusAdvice(
                    advisor_id,
                    provider_labels.get(advisor_id, advisor_id),
                    clipped,
                ))
            else:
                degraded_reasons.append(_degraded_reason(advisor_id, "advisor_empty"))
                _trace_warning(trace_recorder, "advisor_empty", advisor_id)
        except cancellation.TaskCancelled:
            raise
        except Exception:
            degraded_reasons.append(_degraded_reason(advisor_id, "advisor_failed"))
            _trace_warning(trace_recorder, "advisor_failed", advisor_id)
            continue
        finally:
            if advisor is not None:
                with contextlib.suppress(Exception):
                    advisor.close()

    if not advices:
        if draft_first and owner_draft:
            return ConsensusResult(
                answer=owner_draft,
                advisor_count=0,
                degraded=True,
                degraded_reasons=tuple(degraded_reasons) or ("advisors_unavailable",),
            )
        return None

    prompt = render_aggregator_prompt(
        task=task,
        advices=advices,
        context=context,
        draft=owner_draft,
        plan=plan,
    )
    _trace_model_prompt(
        trace_recorder,
        "consensus_aggregate_prompt",
        prompt,
        purpose="consensus aggregate prompt sent to provider",
        source_ref="provider_send:consensus_aggregate",
    )
    try:
        with provider_controls.suppress_assistance():
            answer = selected_provider.send(
                prompt,
                timeout=getattr(
                    selected_provider, "timeout", CONSENSUS_AGGREGATE_TIMEOUT,
                ),
            )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        if owner_draft:
            return ConsensusResult(
                answer=owner_draft,
                advisor_count=len(advices),
                degraded=True,
                degraded_reasons=tuple(
                    [*degraded_reasons, "aggregate_failed"]
                ),
            )
        raise
    answer = _clip(answer, 12_000)
    if not answer:
        if owner_draft:
            return ConsensusResult(
                answer=owner_draft,
                advisor_count=len(advices),
                degraded=True,
                degraded_reasons=tuple(
                    [*degraded_reasons, "aggregate_empty"]
                ),
            )
        raise RuntimeError("consensus answer was empty")
    return ConsensusResult(
        answer=answer,
        advisor_count=len(advices),
        degraded=bool(degraded_reasons),
        degraded_reasons=tuple(degraded_reasons),
    )
