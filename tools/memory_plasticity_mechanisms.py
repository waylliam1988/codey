"""Bounded offline plasticity mechanisms using the neutral memory ports.

Never composed into production. Inputs, snapshots and outcomes are supplied by
the experiment host; no task loop, provider, clock or executable authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from codey.memory.codec import digest, event_payload
from codey.memory.contracts import DeleteResult, DeleteScopeRequest, MemoryEvent, MemoryItem, MemoryQuery, MemoryResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string, require_json_strings

Mechanism = Literal["frequency", "lexical", "hebbian", "temporal", "eligibility", "decay", "normalization",
                    "inhibition", "sparse", "contextual", "recurrent", "hierarchical", "neuromodulation"]
MECHANISMS: tuple[Mechanism, ...] = ("frequency", "lexical", "hebbian", "temporal", "eligibility", "decay",
    "normalization", "inhibition", "sparse", "contextual", "recurrent", "hierarchical", "neuromodulation")
Edge = tuple[str, str, str, str]
Node = tuple[str, str, str]


@dataclass(frozen=True)
class PlasticityLimits:
    nodes: int = 512
    edges: int = 2048
    active: int = 8
    trace_decay: float = 0.8
    synaptic_decay: float = 0.98


_DEFAULT_LIMITS = PlasticityLimits()


class ResearchMemory:
    def __init__(self, mechanism: Mechanism, limits: PlasticityLimits = _DEFAULT_LIMITS) -> None:
        if min(limits.nodes, limits.edges, limits.active) < 1:
            raise ValueError("research capacities must be positive")
        self.mechanism, self.limits = mechanism, limits
        self.counts: dict[Node, float] = {}
        self.weights: dict[Edge, float] = {}
        self.traces: dict[Edge, float] = {}
        self.previous: dict[tuple[str, str], tuple[str, ...]] = {}
        self.seen: dict[str, str] = {}
        self.updates = 0
        self.operations = 0
        self.at = ""

    def observe(self, event: MemoryEvent) -> None:
        fingerprint = digest(event_payload(event))
        if event.event_id in self.seen:
            if self.seen[event.event_id] != fingerprint:
                raise ValueError("research event identity changed")
            return
        symbols = require_json_strings(event.payload.get("symbols", []), "symbols")
        if len(symbols) > 32 or any(not value or len(value) > 128 for value in symbols):
            raise ValueError("research symbols exceed their bounds")
        context = require_json_string(event.payload.get("context", ""), "context") if self.mechanism == "contextual" else ""
        group = (event.scope, context)
        reward = event.payload.get("reward")
        if reward is not None and (isinstance(reward, bool) or not isinstance(reward, (float, int)) or not math.isfinite(reward) or not -1 <= reward <= 1):
            raise ValueError("research reward must be finite and in [-1, 1]")
        if self.mechanism == "decay":
            self.weights = {edge: weight * self.limits.synaptic_decay for edge, weight in self.weights.items()}
        if self.mechanism == "eligibility":
            self.traces = {edge: trace * self.limits.trace_decay for edge, trace in self.traces.items()
                           if trace * self.limits.trace_decay > 0.001}
            if reward is not None:
                for edge, trace in self.traces.items():
                    self.weights[edge] = max(0.0, self.weights.get(edge, 0.0) + reward * trace)
                    self.updates += 1
        previous = self.previous.get(group, ())
        sources: list[str] = symbols
        if self.mechanism in {"temporal", "eligibility", "recurrent"}:
            sources = list(previous[-1:]) if self.mechanism != "recurrent" else ["|".join(previous[-2:])] if previous else []
        if self.mechanism == "sparse":
            sources = sorted(set(sources))[:self.limits.active]
        for target in symbols:
            node = (*group, target)
            self.counts[node] = self.counts.get(node, 0.0) + 1.0
            for source in (() if self.mechanism in {"frequency", "lexical"} else sources):
                self._associate(group, source, target, reward)
            if self.mechanism == "hierarchical" and "/" in target:
                parent = target.rsplit("/", 1)[0]
                self.weights[(*group, parent, target)] = self.weights.get((*group, parent, target), 0.0) + 1.0
                self.updates += 1
        if symbols:
            self.previous[group] = (*previous, *symbols)[-2:]
        if self.mechanism == "normalization":
            totals: dict[tuple[str, str, str], float] = {}
            for edge, weight in self.weights.items():
                source_key = (edge[0], edge[1], edge[2])
                totals[source_key] = totals.get(source_key, 0.0) + weight
            self.weights = {edge: weight / max(1.0, totals[(edge[0], edge[1], edge[2])]) for edge, weight in self.weights.items()}
        self.counts = dict(sorted(self.counts.items(), key=_node_rank)[:self.limits.nodes])
        self.weights = dict(sorted(self.weights.items(), key=_edge_rank)[:self.limits.edges])
        self.traces = {edge: trace for edge, trace in self.traces.items() if edge in self.weights}
        self.previous = dict(list(self.previous.items())[-self.limits.nodes:])
        self.seen[event.event_id] = fingerprint
        self.seen = dict(list(self.seen.items())[-self.limits.nodes:])
        self.operations += len(self.weights) + len(symbols)
        self.at = event.occurred_at

    def _associate(self, group: tuple[str, str], source: str, target: str, reward: float | None) -> None:
        if source == target:
            return
        edge = (*group, source, target)
        strength = 1.0
        if self.mechanism == "neuromodulation":
            strength = (1.0 / self.counts[(*group, target)]) * (1.0 + abs(reward or 0.0))
        self.weights[edge] = self.weights.get(edge, 0.0) + strength
        if self.mechanism == "eligibility":
            self.traces[edge] = 1.0
        self.updates += 1

    def retrieve(self, query: MemoryQuery) -> MemoryResult:
        words = query.text.split()
        sources = ["|".join(words[-2:])] if self.mechanism == "recurrent" else words
        context = query.context if self.mechanism == "contextual" else ""
        scores: dict[Node, float] = {}
        for node, count in self.counts.items():
            if node[0] not in query.scopes or node[1] != context:
                continue
            if self.mechanism == "frequency":
                scores[node] = count
            elif self.mechanism == "lexical":
                scores[node] = float(node[2] in words)
            else:
                scores[node] = sum(self.weights.get((node[0], context, source, node[2]), 0.0) for source in sources)
                if self.mechanism == "inhibition":
                    scores[node] -= count / max(1.0, sum(self.counts.values()))
        def rank(node: Node) -> tuple[float, Node]:
            return -scores[node], node

        ranked = sorted(scores, key=rank)[:max(0, query.max_items)]
        items = tuple(MemoryItem("research:" + digest({"node": list(node)}), "hypothesis", node[2], node[0],
            ("offline-research:" + self.mechanism,), self.at) for node in ranked if scores[node] > 0)
        return MemoryResult(items)

    def delete_scope(self, request: DeleteScopeRequest) -> DeleteResult:
        removed = sum(node[0] == request.scope for node in self.counts)
        self.counts = {node: value for node, value in self.counts.items() if node[0] != request.scope}
        self.weights = {edge: value for edge, value in self.weights.items() if edge[0] != request.scope}
        self.traces = {edge: value for edge, value in self.traces.items() if edge[0] != request.scope}
        self.previous = {key: value for key, value in self.previous.items() if key[0] != request.scope}
        return DeleteResult(removed)

    def snapshot(self) -> JsonObject:
        return {"schema_version": 1, "mechanism": self.mechanism, "at": self.at, "updates": self.updates,
            "limits": {"nodes": self.limits.nodes, "edges": self.limits.edges, "active": self.limits.active,
                "trace_decay": self.limits.trace_decay, "synaptic_decay": self.limits.synaptic_decay},
            "operations": self.operations, "counts": [[*node, count] for node, count in sorted(self.counts.items())],
            "weights": [[*edge, weight] for edge, weight in sorted(self.weights.items())],
            "traces": [[*edge, trace] for edge, trace in sorted(self.traces.items())],
            "previous": [[*key, list(value)] for key, value in sorted(self.previous.items())], "seen": dict(self.seen)}

    def restore(self, value: JsonObject) -> None:
        if value.get("schema_version") != 1 or value.get("mechanism") != self.mechanism or value.get("limits") != self.snapshot()["limits"]:
            raise ValueError("research snapshot identity differs")
        restored: dict[str, list[list[JsonValue]]] = {}
        for key in ("counts", "weights", "traces", "previous"):
            raw = value.get(key)
            maximum = self.limits.edges if key in {"weights", "traces"} else self.limits.nodes
            width = 5 if key in {"weights", "traces"} else 4 if key == "counts" else 3
            if not isinstance(raw, list) or len(raw) > maximum:
                raise ValueError("research snapshot exceeds its capacity")
            arrays: list[list[JsonValue]] = []
            for row in raw:
                if not isinstance(row, list) or len(row) != width:
                    raise ValueError("invalid research snapshot row")
                arrays.append(row)
            restored[key] = arrays
        counts = {(require_json_string(row[0], "scope"), require_json_string(row[1], "context"), require_json_string(row[2], "symbol")): _weight(row[3]) for row in restored["counts"]}
        weights, traces = _edges(restored["weights"]), _edges(restored["traces"])
        previous = {(require_json_string(row[0], "scope"), require_json_string(row[1], "context")): tuple(require_json_strings(row[2], "previous")) for row in restored["previous"]}
        seen = {key: require_json_string(item, "digest") for key, item in decode_json_object(value.get("seen")).items()}
        if len(seen) > self.limits.nodes or any(len(items) > 2 for items in previous.values()):
            raise ValueError("research snapshot history exceeds its bounds")
        at = require_json_string(value.get("at"), "at")
        from codey.types.json_value import require_json_integer

        updates = require_json_integer(value.get("updates"), "updates")
        operations = require_json_integer(value.get("operations"), "operations")
        self.counts, self.weights, self.traces, self.previous, self.seen = counts, weights, traces, previous, seen
        self.at, self.updates, self.operations = at, updates, operations


def _weight(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError("research weights must be finite and nonnegative")
    return float(value)


def _node_rank(row: tuple[Node, float]) -> tuple[float, Node]:
    return -row[1], row[0]


def _edge_rank(row: tuple[Edge, float]) -> tuple[float, Edge]:
    return -row[1], row[0]


def _edges(values: list[list[JsonValue]]) -> dict[Edge, float]:
    result: dict[Edge, float] = {}
    for row in values:
        if len(row) != 5:
            raise ValueError("invalid research edge")
        key = (require_json_string(row[0], "scope"), require_json_string(row[1], "context"),
               require_json_string(row[2], "source"), require_json_string(row[3], "target"))
        result[key] = _weight(row[4])
    return result
