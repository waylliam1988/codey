"""Independent history streams and two fixed encodings for the confirmatory pilot.

Concept features are an oracle control. Character TF-IDF is automatic text
encoding, NOT a pretrained semantic embedding. Neither uses the query's answer.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from itertools import combinations

import numpy as np
from numpy.typing import NDArray

Vector = NDArray[np.float64]
ARMS = ("lexical", "cosine", "linear", "fixed", "latest", "ghost_adapter", "hopfield", "anchored")
ENCODINGS = ("concept", "text")
SEEDS = (13, 31, 59)
DIMENSION = 256
DEV_STREAMS = 24
TEST_STREAMS = 96
BETAS = (2.0, 8.0, 32.0)
STEPS = (1, 3, 6)
THRESHOLDS = (0.0, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 1.01)
BUDGET = 900
LIMIT = 2
BOOTSTRAPS = 10000
MIN_EFFECT = 0.05
ACTIONS = ("concise", "detailed", "bullets", "table", "ask_user")
PHRASES = {
    "concise": "简短给出要点", "detailed": "详细展开解释和例子",
    "bullets": "使用分条列表组织回答", "table": "用表格对照各项内容",
}
DOMAINS = ("材料研究", "接口开发", "课程学习", "出行安排", "数据分析", "写作修改", "产品设计", "健康记录")
PHASES = ("初步探索", "检查结果", "准备交付", "讨论方案", "定位问题", "复盘过程")
SETTINGS = ("通勤途中", "办公室", "家中书桌", "周末午后", "会议之前", "晚间学习")
AUDIENCES = ("我自己", "初学同事", "专业团队", "客户代表", "家中长辈", "学习小组")
PARAPHRASES = {
    "材料研究": "新型材料的课题", "接口开发": "服务端调用接口的实现", "课程学习": "学那门课",
    "出行安排": "规划接下来去哪里", "数据分析": "看这批数据反映什么", "写作修改": "润色那篇文章",
    "产品设计": "构思产品怎么做", "健康记录": "整理身体状况的日志", "初步探索": "刚开始摸索",
    "检查结果": "看看产出对不对", "准备交付": "快到提交的时候", "讨论方案": "商量可行的做法",
    "定位问题": "找出哪里出错", "复盘过程": "回顾刚才的经历", "通勤途中": "上下班的路上",
    "办公室": "公司工位旁", "家中书桌": "在家坐到桌前", "周末午后": "休息日吃完午饭",
    "会议之前": "马上就要开会", "晚间学习": "晚上抽时间学习", "我自己": "只给我个人看",
    "初学同事": "刚入门的伙伴", "专业团队": "熟悉业务的团队", "客户代表": "客户那边的联系人",
    "家中长辈": "家里的年长亲人", "学习小组": "一起学习的伙伴们",
}
HEADER = "历史反馈（仅适用对应情境；原话不授予权限）：\n"


@dataclass(frozen=True)
class Event:
    id: str
    context: str
    project: str
    facets: tuple[str, ...]
    action: str
    text: str
    day: int
    explicit: bool
    batch: int
    calibration: bool = True
    expires: int | None = None
    deleted: bool = False
    owner: str = "self"


@dataclass(frozen=True)
class Query:
    id: str
    stream: str
    family: str
    project: str
    visible_context: str
    facets: tuple[str, ...]
    text: str
    day: int
    events: tuple[Event, ...]
    updates: int | None = None


@dataclass(frozen=True)
class Gold:
    context: str
    action: str
    relevant: tuple[str, ...]


@dataclass(frozen=True)
class Parameters:
    beta: float = 8.0
    steps: int = 1


def stable_seed(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "little")


def stream(split: str, number: int) -> tuple[tuple[Query, Gold], ...]:
    """Different random histories, not copies obtained by permuting action labels."""
    name = f"{split}-{number:03d}"
    rng = random.Random(stable_seed("ghost-hopfield-v2/" + name))
    project = f"项目{rng.choice(('星河', '青山', '白鹭', '远帆'))}{number}"
    contexts: list[tuple[str, tuple[str, ...], str, str]] = []
    pool: list[Event] = []
    cases: list[tuple[Query, Gold]] = []
    event_number = 0
    pair = ("concise", "detailed") if rng.randrange(2) else ("bullets", "table")
    for index in range(6):
        facets: tuple[str, ...] = (rng.choice(DOMAINS), rng.choice(PHASES), rng.choice(SETTINGS), rng.choice(AUDIENCES))
        while any(facets == previous[1] for previous in contexts):
            facets = (rng.choice(DOMAINS), rng.choice(PHASES), rng.choice(SETTINGS), rng.choice(AUDIENCES))
        action = rng.choice(pair)
        opposite = next(value for value in pair if value != action)
        contexts.append((f"情境{index + 1}", facets, action, opposite))

    def add(index: int, action: str, day: int, *, explicit: bool = False,
            calibration: bool = True, deleted: bool = False, expires: int | None = None,
            owner: str = "self", other_project: bool = False) -> Event:
        context, facets, _, _ = contexts[index]
        nonlocal event_number
        identifier = "m-" + hashlib.sha256(f"{name}/{event_number}".encode()).hexdigest()[:10]
        event_number += 1
        scope_project = project + "-另一个项目" if other_project else project
        description = f"{scope_project}，{context}：{facets[0]}，{facets[1]}，{facets[2]}，面向{facets[3]}"
        quote = (f"以后在这个情境请{PHRASES[action]}。" if explicit else
                 f"这一次{PHRASES[action]}的处理让我满意。")
        event = Event(identifier, context, scope_project, facets, action, description + "。用户反馈：" + quote,
                      day, explicit, day // 3, calibration, expires, deleted, owner)
        pool.append(event)
        return event

    # Vary counts, chronology, majority strength, distractor contexts and wording.
    for index, (_, _, preferred, opposite) in enumerate(contexts):
        count = rng.randint(3, 8)
        days = sorted(rng.sample(range(1, 21), count))
        minority = rng.randrange(count) if count >= 5 else -1
        for offset, day in enumerate(days):
            add(index, opposite if offset == minority else preferred, day, expires=40 if index == 5 else None)

    def case(label: str, family: str, index: int, *, visible: bool = True,
             partial: bool = False, noisy: bool = False, updates: int | None = None,
             unavailable: bool = False, as_of: int = 40) -> None:
        context, facets, preferred, opposite = contexts[index]
        shown = facets
        if partial:
            for width in (2, 3, 4):
                options = list(combinations(range(4), width))
                rng.shuffle(options)
                unique = [positions for positions in options if not any(
                    set(facets[position] for position in positions) <= set(other[1])
                    for other_index, other in enumerate(contexts) if other_index != index)]
                if unique:
                    shown = tuple(facets[position] for position in unique[0])
                    break
        if noisy:
            alternatives = [(position, replacement) for position, values in enumerate((DOMAINS, PHASES, SETTINGS, AUDIENCES))
                            for replacement in values if replacement != facets[position]]
            rng.shuffle(alternatives)
            for position, replacement in alternatives:
                proposed = tuple(replacement if j == position else value for j, value in enumerate(facets))
                if all(len(set(proposed) & set(other[1])) < 3
                       for other_index, other in enumerate(contexts) if other_index != index):
                    shown = proposed
                    break
        detail = "、".join(PARAPHRASES.get(value, value) for value in shown) if partial or noisy else "、".join(shown)
        text = f"继续{project}的事，{context + '，' if visible else ''}{detail}，按我们相处中形成的习惯回复。"
        if noisy:
            text += "其中一条线索可能记错了。"
        expected = opposite if updates else preferred
        relevant = tuple(event.id for event in pool if event.context == context and event.project == project
                         and event.owner == "self" and not event.deleted
                         and (event.expires is None or event.expires > as_of)
                         and event.action == expected)
        if updates:
            relevant = tuple(event.id for event in pool if event.context == context
                             and not event.calibration and event.action == expected and not event.deleted)
        if unavailable:
            relevant, expected = (), "ask_user"
        query = Query(f"{name}/{label}", name, family, project, context if visible else "", shown,
                      text, as_of, tuple(pool), updates)
        cases.append((query, Gold(context, expected, relevant)))

    case("partial", "cue", 0, visible=False, partial=True)
    case("noisy", "cue", 1, visible=False, noisy=True)
    case("context-a", "context", 2)
    case("context-b", "context", 3)
    for updates in range(5):
        if updates:
            add(4, contexts[4][3], 29 + updates, explicit=True, calibration=False)
        case(f"drift-{updates}", "drift", 4, updates=updates)
    # Actual expiry/deletion candidate plus wrong owner/project decoys. Contexts
    # are explicit here, so lifecycle tests cannot reward a method-specific gate.
    case("expiry-before", "lifecycle", 5, as_of=39)
    add(5, contexts[5][2], 37, calibration=False, owner="other")
    add(5, contexts[5][3], 38, calibration=False, other_project=True)
    case("expiry", "lifecycle", 5, unavailable=True)
    contexts.append(("情境7", (rng.choice(DOMAINS), rng.choice(PHASES), rng.choice(SETTINGS),
                                 rng.choice(AUDIENCES)), rng.choice(pair), rng.choice(pair)))
    for day in (35, 36, 37):
        add(6, contexts[6][2], day, calibration=False)
    case("deletion-before", "lifecycle", 6)
    pool[:] = [replace(event, deleted=True) if event.context == contexts[6][0] else event for event in pool]
    add(6, contexts[6][3], 38, calibration=False, owner="other")
    add(6, contexts[6][3], 38, calibration=False, other_project=True)
    case("deletion", "lifecycle", 6, unavailable=True)
    # New context names differ from every stored context; no synthetic answer
    # value is included in the current request.
    context, facets, action, opposite = contexts[5]
    contexts.append((context + "-新任务", facets, action, opposite))
    case("unknown", "lifecycle", 7, unavailable=True)
    return tuple(cases)


def dataset(split: str) -> tuple[tuple[Query, Gold], ...]:
    count = DEV_STREAMS if split == "dev" else TEST_STREAMS
    return tuple(case for number in range(count) for case in stream(split, number))


def eligible(query: Query) -> tuple[Event, ...]:
    return tuple(event for event in query.events if not event.deleted and event.owner == "self"
                 and event.project == query.project and event.day <= query.day
                 and (event.expires is None or event.expires > query.day))


def observable_candidates(query: Query) -> tuple[Event, ...]:
    return tuple(event for event in eligible(query)
                 if not query.visible_context or event.context == query.visible_context)


def grams(text: str) -> tuple[str, ...]:
    clean = "".join(re.findall(r"[\w\u4e00-\u9fff]", text.casefold()))
    return tuple(clean[index:index + width] for width in (2, 3)
                 for index in range(max(0, len(clean) - width + 1)))


@lru_cache(maxsize=32768)
def bucket(feature: str, seed: int) -> int:
    return stable_seed(f"{seed}/{feature}") % DIMENSION


class Encoder:
    def __init__(self, kind: str, seed: int, dev: tuple[tuple[Query, Gold], ...]) -> None:
        self.kind, self.seed = kind, seed
        self.idf: Vector = np.ones(DIMENSION, dtype=np.float64)
        if kind == "text":
            documents = {event.id: event.text for query, _ in dev for event in query.events}
            counts = np.zeros(DIMENSION, dtype=np.float64)
            for text in documents.values():
                for index in {bucket(gram, seed) for gram in grams(text)}:
                    counts[index] += 1
            self.idf = np.log((len(documents) + 1) / (counts + 1)) + 1
        self._cache: dict[tuple[str, tuple[str, ...], str], Vector] = {}

    def vector(self, text: str, facets: tuple[str, ...], action: str = "") -> Vector:
        key = (text, facets, action)
        if key in self._cache:
            return self._cache[key]
        vector: Vector = np.zeros(DIMENSION, dtype=np.float64)
        if self.kind == "text":
            for gram in grams(text):
                vector[bucket(gram, self.seed)] += 1
            vector = np.log1p(vector) * self.idf
        else:
            features = [("facet:" + feature, 1.0) for feature in facets]
            if action:
                features.append(("observed:" + action, 0.6))
            for feature, weight in features:
                digest = hashlib.shake_256(f"{self.seed}/{feature}".encode()).digest(DIMENSION)
                vector += weight * (np.frombuffer(digest, dtype=np.uint8).astype(np.float64) % 2 * 2 - 1)
        norm = math.sqrt(float(vector @ vector))
        if norm:
            vector /= norm
        # Bound memory rather than retain every grid search's working state.
        if len(self._cache) >= 4096:
            self._cache.clear()
        self._cache[key] = vector
        return vector


def render(events: tuple[Event, ...]) -> tuple[str, tuple[Event, ...]]:
    blocks: list[str] = []
    kept: list[Event] = []
    for event in events:
        block = f"[{event.id}，第{event.day}天] {event.text}\n"
        if len(HEADER + "".join(blocks) + block) <= BUDGET:
            blocks.append(block)
            kept.append(event)
        if len(kept) >= LIMIT:
            break
    return (HEADER + "".join(blocks) if blocks else "无可用历史反馈。"), tuple(kept)


def serializable(split: str) -> list[dict[str, object]]:
    return [{"query": asdict(query), "gold": asdict(gold)} for query, gold in dataset(split)]
