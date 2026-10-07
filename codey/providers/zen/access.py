"""Short-lived plain-channel access observations, scoped to the agreement."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from codey.storage.local_store import StoreCorruption, read_json_strict, write_json_atomic

OBSERVATION_TTL = 15 * 60


class ZenAccessObservations:
    def __init__(self, state_home: Path, agreement: str) -> None:
        self.path = state_home / "connections" / "zen-plain-access.json"
        self.agreement = agreement
        self._lock = threading.RLock()
        self._observations: dict[tuple[str, str], tuple[bool, float]] = {}
        try:
            stored = read_json_strict(self.path)
            if not isinstance(stored, dict) or stored.get("schema_version") != 1 or stored.get("agreement") != agreement:
                return
            for row in stored.get("observations", [])[:64]:
                if (isinstance(row, dict) and isinstance(row.get("model"), str) and len(row["model"]) <= 1000
                        and row.get("protocol") in {"openai-completions", "openai-responses"}
                        and type(row.get("allowed")) is bool and type(row.get("observed_at")) in {int, float}):
                    self._observations[(row["model"], row["protocol"])] = row["allowed"], float(row["observed_at"])
        except (StoreCorruption, TypeError, ValueError):
            self._observations = {}

    def plain_refused(self, model: str, protocol: str) -> bool:
        return self.plain_access(model, protocol) is False

    def plain_access(self, model: str, protocol: str) -> bool | None:
        with self._lock:
            observed = self._observations.get((model, protocol))
            return observed[0] if observed is not None and 0 <= time.time() - observed[1] < OBSERVATION_TTL else None

    def record_plain_success(self, model: str, protocol: str) -> None:
        self._record(model, protocol, True)

    def record_plain_refusal(self, model: str, protocol: str) -> None:
        self._record(model, protocol, False)

    def _record(self, model: str, protocol: str, allowed: bool) -> None:
        with self._lock:
            now = time.time()
            self._observations = {key: observation for key, observation in self._observations.items() if 0 <= now - observation[1] < OBSERVATION_TTL}
            self._observations[(model, protocol)] = allowed, now
            retained = sorted(self._observations.items(), key=lambda item: item[1][1], reverse=True)[:64]
            self._observations = dict(retained)
            write_json_atomic(self.path, {"schema_version": 1, "agreement": self.agreement,
                "observations": [{"model": key[0], "protocol": key[1], "allowed": value[0], "observed_at": value[1]} for key, value in retained]})
