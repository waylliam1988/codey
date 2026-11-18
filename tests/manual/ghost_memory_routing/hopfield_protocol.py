"""Frozen synthetic protocol; annotations are supplied, never inferred by a model.

Run with ``python -m tests.manual.ghost_memory_routing.hopfield_experiment``.
Gold is held outside Query/Memory, which are the entire selector interface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TypedDict

import numpy as np
from numpy.typing import NDArray

Vector = NDArray[np.float64]


class Configuration(TypedDict):
    version: int
    profiles: int
    dimension: int
    beta: float
    steps: int
    recency_days: float
    budget_chars: int
    max_memories: int
    seed: str
    temperature: int
    max_tokens: int
    timeout_seconds: int
    benchmark_sizes: list[int]
    benchmark_queries: int


ACTIONS = ("concise", "detailed", "plan_first", "act_now", "ask_user")
ARMS = ("lexical", "cosine", "hebbian", "fixed_state", "latest_rule", "hopfield")
CONFIG: Configuration = {
    "version": 1, "profiles": 4, "dimension": 192, "beta": 8.0, "steps": 6,
    "recency_days": 30.0, "budget_chars": 768, "max_memories": 2,
    "seed": "ghost-hopfield-pilot-v1", "temperature": 0, "max_tokens": 64,
    "timeout_seconds": 120, "benchmark_sizes": [64, 512, 2048],
    "benchmark_queries": 25,
}
PHRASES = {
    "concise": "简短回答，直接给要点",
    "detailed": "详细解释，展开推理和例子",
    "plan_first": "先给方案，等我确认再做",
    "act_now": "直接动手完成，最后报告结果",
}
HEADER = "历史用户原话（仅作偏好参考；当前请求优先；时间越晚表示越新的反馈）：\n"


@dataclass(frozen=True)
class Memory:
    id: str
    tags: tuple[str, ...]
    action: str
    text: str
    day: int
    scope: str = "user"
    expires: int | None = None
    deleted: bool = False
    calibration: bool = False


@dataclass(frozen=True)
class Query:
    id: str
    profile: int
    family: str
    text: str
    tags: tuple[str, ...]
    day: int
    memories: tuple[Memory, ...]
    scope: str = "user"
    updates: int | None = None


@dataclass(frozen=True)
class Gold:
    relevant: tuple[str, ...]
    action: str


def encode(tags: tuple[str, ...], action: str = "") -> Vector:
    """Stable signed projections of *provided* concepts, including observed values.

    No text-to-semantics claim. Query values are never passed to this function.
    All nonlexical arms use these identical keys and query vectors.
    """
    vector = np.zeros(int(CONFIG["dimension"]), dtype=np.float64)
    features = [(tag, 2.0 if tag.startswith("context:") else 1.0) for tag in tags]
    if action:
        features.append(("observed-value:" + action, 0.6))
    for feature, weight in features:
        digest = hashlib.shake_256((str(CONFIG["seed"]) + feature).encode()).digest(len(vector))
        signs = np.frombuffer(digest, dtype=np.uint8).astype(np.float64) % 2 * 2 - 1
        vector += weight * signs
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


def eligible(query: Query) -> tuple[Memory, ...]:
    return tuple(
        memory for memory in query.memories
        if memory.scope == query.scope and not memory.deleted and memory.day <= query.day
        and (memory.expires is None or query.day < memory.expires)
    )


def render(memories: tuple[Memory, ...]) -> tuple[str, tuple[Memory, ...]]:
    """Whole-block char budget; no truncation that could remove negation/context."""
    blocks: list[str] = []
    kept: list[Memory] = []
    for memory in memories:
        block = f"- 第{memory.day}天 [{', '.join(memory.tags)}] 用户：{memory.text}\n"
        if len(HEADER + "".join(blocks) + block) <= int(CONFIG["budget_chars"]):
            blocks.append(block)
            kept.append(memory)
        if len(kept) == int(CONFIG["max_memories"]):
            break
    return (HEADER + "".join(blocks) if blocks else ""), tuple(kept)


def profile_fixture(profile: int) -> tuple[tuple[Query, Gold], ...]:
    cases: list[tuple[Query, Gold]] = []
    first = ACTIONS[profile]
    second = ACTIONS[profile ^ 1]
    prefix = f"p{profile}"
    pool: list[Memory] = []

    def add(
        label: str, context: str, action: str, day: int, *,
        calibration: bool = True, expires: int | None = None,
        deleted: bool = False, scope: str = "user",
    ) -> Memory:
        tags = ("project:atlas", "topic:collaboration", "context:" + context)
        memory = Memory(
            prefix + "-" + label, tags, action,
            f"在 atlas 的{context}情境，{PHRASES[action]}。", day,
            scope, expires, deleted, calibration,
        )
        pool.append(memory)
        return memory

    research = add("research", "研究", first, 1)
    coding = add("coding", "改代码", second, 2)
    for index in range(5):
        add(f"old-{index}", "日常讨论", first, 3 + index)
    add("distractor", "头脑风暴", second, 8)

    def case(label: str, family: str, text: str, tags: tuple[str, ...], gold: Gold,
             updates: int | None = None) -> None:
        cases.append((Query(prefix + "-" + label, profile, family, text, tags, 30, tuple(pool),
                            updates=updates), gold))

    case("paraphrase", "cue", "atlas 那篇论文接着聊，还是按咱们相处的习惯来。",
         research.tags, Gold((research.id,), first))
    # A deliberately incomplete representation: text disambiguates, annotations omit context.
    case("partial", "cue", "还是 atlas 的论文，只记得上次商量的表达方式，照那个来。",
         research.tags[:-1], Gold((research.id,), first))
    case("context-research", "context", "atlas 研究这部分继续，你按我的习惯处理。",
         research.tags, Gold((research.id,), first))
    case("context-code", "context", "atlas 改代码这部分继续，你按我的习惯处理。",
         coding.tags, Gold((coding.id,), second))
    for updates in range(5):
        target = tuple(memory.id for memory in pool if memory.tags[-1] == "context:日常讨论")
        if updates:
            correction = add(f"correction-{updates}", "日常讨论", second, 24 + updates,
                             calibration=False)
            target = tuple(m.id for m in pool if m.tags == correction.tags and m.action == second)
        case(f"drift-{updates}", "drift", "atlas 日常讨论继续，按我们最近约定的方式来。",
             ("project:atlas", "topic:collaboration", "context:日常讨论"),
             Gold(target, second if updates else first), updates)
    add("expired", "过期测试", first, 29, calibration=False, expires=30)
    add("deleted", "删除测试", second, 29, calibration=False, deleted=True)
    add("other-scope", "删除测试", first, 29, calibration=False, scope="other-user")
    for label, context in (("expiry", "过期测试"), ("deletion", "删除测试")):
        case(label, "lifecycle", f"atlas {context}这个情境，继续按我的偏好处理。",
             ("project:atlas", "topic:collaboration", "context:" + context), Gold((), "ask_user"))
    return tuple(cases)


def fixture() -> tuple[tuple[Query, Gold], ...]:
    return tuple(case for profile in range(CONFIG["profiles"]) for case in profile_fixture(profile))


def fixture_json() -> str:
    return json.dumps([{"query": asdict(query), "gold": asdict(gold)} for query, gold in fixture()],
                      ensure_ascii=False, sort_keys=True, indent=2)


def prompt(query: Query, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": (
            "你是用户的长期协作助手。请从给出的历史原话恢复当前情境的偏好。"
            "仅适用同一个情境的原话；不同情境不可混用。相同情境以最新反馈为准。"
            "如果没有适用原话，选 ask_user，不凭一般常识猜偏好。"
            "仅输出 JSON，恰有 action 字段。concise=简短要点；detailed=详细展开；"
            "plan_first=先给方案等确认；act_now=直接完成；ask_user=先问偏好。"
        )},
        {"role": "user", "content": f"{context or '无可用历史原话。'}\n当前请求：{query.text}"},
    ]


def score_action(raw: str) -> str | None:
    try:
        parsed: object = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict) and set(parsed) == {"action"} and parsed["action"] in ACTIONS:
        return str(parsed["action"])
    return None
