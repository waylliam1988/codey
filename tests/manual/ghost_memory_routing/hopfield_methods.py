"""Experimental selectors only: no production imports this module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import numpy as np

from codey.ghost.observation_index import score_observation, tokenize
from tests.manual.ghost_memory_routing.hopfield_protocol import CONFIG, Memory, Query, Vector, eligible, encode


def softmax(logits: Vector) -> Vector:
    weights = np.exp(logits - np.max(logits))
    return cast(Vector, weights / np.sum(weights))


def hopfield_update(keys: Vector, query: Vector, prior: Vector) -> Vector:
    return keys.T @ softmax(float(CONFIG["beta"]) * (keys @ query) + prior)


def energy(keys: Vector, query: Vector, prior: Vector) -> float:
    logits = float(CONFIG["beta"]) * (keys @ query) + prior
    peak = float(np.max(logits))
    return float(query @ query / 2 - (peak + np.log(np.exp(logits - peak).sum())) / float(CONFIG["beta"]))


class Index:
    """Every index is built from the same filtered candidate set and observed keys.

    Fixed state deliberately admits only the actual calibration prefix. Its
    lifecycle filtering is identical. No gold labels enter construction/query.
    """

    def __init__(self, query: Query, arm: str) -> None:
        self.arm = arm
        candidates = eligible(query)
        self.memories = tuple(m for m in candidates if m.calibration) if arm == "fixed_state" else candidates
        self.keys = np.array([encode(m.tags, m.action) for m in self.memories], dtype=np.float64)
        if not self.memories:
            self.keys = np.empty((0, int(CONFIG["dimension"])), dtype=np.float64)
        self.prior = np.array([-(query.day - m.day) / float(CONFIG["recency_days"])
                               for m in self.memories], dtype=np.float64)
        self.weights = self.keys.T @ self.keys / max(1, len(self.memories)) if arm == "hebbian" else None

    @property
    def array_bytes(self) -> int:
        return self.keys.nbytes + self.prior.nbytes + (self.weights.nbytes if self.weights is not None else 0)

    def ranked(self, query: Query) -> tuple[Memory, ...]:
        if not self.memories:
            return ()
        vector = encode(query.tags)
        if self.arm == "lexical":
            origin = datetime(2026, 1, 1, tzinfo=UTC)
            scores = np.array([score_observation(tokenize(query.text), {
                "user_text": m.text, "ts": (origin + timedelta(days=m.day)).isoformat(),
            }, (origin + timedelta(days=query.day)).timestamp()) for m in self.memories])
        elif self.arm == "latest_rule":
            scores = np.array([float(m.day) if set(query.tags) <= set(m.tags) else -np.inf
                               for m in self.memories])
        else:
            if self.arm == "hopfield":
                for _ in range(int(CONFIG["steps"])):
                    vector = hopfield_update(self.keys, vector, self.prior)
            elif self.arm == "hebbian":
                assert self.weights is not None
                for _ in range(int(CONFIG["steps"])):
                    vector = encode(query.tags) + self.weights @ vector
                    vector /= max(float(np.linalg.norm(vector)), 1e-12)
            scores = float(CONFIG["beta"]) * (self.keys @ vector) + self.prior
        order = sorted(range(len(self.memories)), key=lambda i: (-float(scores[i]), self.memories[i].id))
        return tuple(self.memories[i] for i in order
                     if np.isfinite(scores[i]) and (self.arm != "lexical" or scores[i] > 0))
