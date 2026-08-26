"""Versioned local Provider adapter overrides with rollback."""

from __future__ import annotations

import shutil
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codey import __version__
from codey.repairs.adapter_surface import adapter_repair_surface
from codey.storage.local_store import DEFAULT_STATE_HOME, read_json, write_json_atomic
from codey.providers.diagnostics import (
    FAILURE_CONTROL_MISSING,
    FAILURE_READINESS_STALE,
    FAILURE_RESPONSE_MISSING,
)
from codey.providers.ids import normalize_provider_id


STATUS_CANDIDATE = "candidate"
STATUS_PROVISIONAL = "provisional"
STATUS_ACTIVE = "active"
STATUS_ROLLED_BACK = "rolled_back"
ENABLED_STATUSES = {STATUS_PROVISIONAL, STATUS_ACTIVE}
STRUCTURAL_FAILURES = {
    FAILURE_CONTROL_MISSING,
    FAILURE_READINESS_STALE,
    FAILURE_RESPONSE_MISSING,
}
MAX_INDEX_BYTES = 128 * 1024
MAX_GENERATIONS = 6
SUCCESS_PROMOTION_COUNT = 2
FAILURE_ROLLBACK_COUNT = 2


@dataclass(frozen=True)
class AdapterOverride:
    provider_id: str
    generation: int
    status: str
    root: Path
    success_count: int = 0
    failure_count: int = 0


def overrides_root(state_home: str | Path | None = DEFAULT_STATE_HOME) -> Path:
    return Path(state_home or DEFAULT_STATE_HOME) / "adapter-overrides"


def install_candidate(
    provider_id: str,
    source_root: str | Path,
    *,
    state_home: str | Path | None = DEFAULT_STATE_HOME,
    base_version: str = "",
    base_hash: str = "",
    tests: tuple[str, ...] = (),
) -> AdapterOverride:
    provider_id = normalize_provider_id(provider_id)
    if not provider_id:
        raise ValueError("provider_id required")
    source_root = Path(source_root).resolve()
    base_version = str(base_version or __version__)
    base_hash = str(base_hash or adapter_base_hash(provider_id, source_root))
    source_codey = source_root / "codey"
    if not source_codey.is_dir():
        raise ValueError("candidate source does not contain codey package")
    index = _load_index(provider_id, state_home)
    generation = _next_generation(index)
    provider_dir = _provider_dir(provider_id, state_home)
    generation_dir = provider_dir / str(generation)
    if generation_dir.exists():
        shutil.rmtree(generation_dir)
    shutil.copytree(
        source_codey,
        generation_dir / "codey",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    current = _current_generation(index)
    record = {
        "generation": generation,
        "status": STATUS_CANDIDATE,
        "path": str(generation_dir),
        "success_count": 0,
        "failure_count": 0,
        "base_version": str(base_version or ""),
        "base_hash": str(base_hash or ""),
        "tests": list(tests[:20]),
        "created_at": _now(),
    }
    generations = _generations(index)
    generations[str(generation)] = record
    index = {
        "schema_version": 1,
        "provider_id": provider_id,
        "current_generation": current or 0,
        "previous_generation": current or 0,
        "generations": generations,
    }
    _trim_generations(index)
    _save_index(provider_id, state_home, index)
    return _override_from_record(provider_id, generation, record)


def mark_provisional(
    provider_id: str,
    generation: int,
    *,
    state_home: str | Path | None = DEFAULT_STATE_HOME,
) -> AdapterOverride | None:
    index = _load_index(provider_id, state_home)
    record = _record(index, generation)
    if record is None or record.get("status") == STATUS_ROLLED_BACK:
        return None
    current = _current_generation(index)
    record["status"] = STATUS_PROVISIONAL
    record["success_count"] = max(1, int(record.get("success_count") or 0))
    record["failure_count"] = 0
    index["previous_generation"] = current if current != generation else int(index.get("previous_generation") or 0)
    index["current_generation"] = generation
    _save_index(provider_id, state_home, index)
    return _override_from_record(normalize_provider_id(provider_id), generation, record)


def load_enabled_override(
    provider_id: str,
    *,
    state_home: str | Path | None = DEFAULT_STATE_HOME,
    current_root: str | Path | None = None,
) -> AdapterOverride | None:
    index = _load_index(provider_id, state_home)
    generation = _current_generation(index)
    if not generation:
        return None
    record = _record(index, generation)
    if record is None or record.get("status") not in ENABLED_STATUSES:
        return None
    if not _base_matches(provider_id, record, current_root):
        return None
    override = _override_from_record(normalize_provider_id(provider_id), generation, record)
    return override if override.root.is_dir() else None


def record_success(
    provider_id: str,
    generation: int,
    *,
    state_home: str | Path | None = DEFAULT_STATE_HOME,
    current_root: str | Path | None = None,
) -> AdapterOverride | None:
    index = _load_index(provider_id, state_home)
    record = _record(index, generation)
    if record is None or record.get("status") not in ENABLED_STATUSES:
        return None
    success_count = int(record.get("success_count") or 0) + 1
    record["success_count"] = success_count
    record["failure_count"] = 0
    if record.get("status") == STATUS_PROVISIONAL and success_count >= SUCCESS_PROMOTION_COUNT:
        record["status"] = STATUS_ACTIVE
        record["activated_at"] = _now()
    _save_index(provider_id, state_home, index)
    return load_enabled_override(provider_id, state_home=state_home, current_root=current_root)


def record_failure(
    provider_id: str,
    generation: int,
    failure_kind: str,
    *,
    state_home: str | Path | None = DEFAULT_STATE_HOME,
    current_root: str | Path | None = None,
) -> AdapterOverride | None:
    if failure_kind not in STRUCTURAL_FAILURES:
        return load_enabled_override(provider_id, state_home=state_home, current_root=current_root)
    index = _load_index(provider_id, state_home)
    record = _record(index, generation)
    if record is None or record.get("status") not in ENABLED_STATUSES:
        return None
    failures = int(record.get("failure_count") or 0) + 1
    record["failure_count"] = failures
    if failures >= FAILURE_ROLLBACK_COUNT:
        record["status"] = STATUS_ROLLED_BACK
        record["rolled_back_at"] = _now()
        previous = int(index.get("previous_generation") or 0)
        previous_record = _record(index, previous)
        if previous and previous_record is not None and Path(str(previous_record.get("path") or "")).is_dir():
            previous_record["status"] = STATUS_ACTIVE
            previous_record["failure_count"] = 0
            index["current_generation"] = previous
        else:
            index["current_generation"] = 0
    _save_index(provider_id, state_home, index)
    return load_enabled_override(provider_id, state_home=state_home, current_root=current_root)


def adapter_base_hash(provider_id: str, source_root: str | Path | None = None) -> str:
    """Hash the built-in Codey package that an override was generated against.

    Includes every JSON file on the provider's repair surface so a changed
    builtin profile invalidates overrides generated against the old data.
    """

    root = Path(source_root).resolve() if source_root is not None else Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    digest.update(normalize_provider_id(provider_id).encode("utf-8"))
    json_surface = {
        rel
        for rel in adapter_repair_surface(normalize_provider_id(provider_id))
        if rel.endswith(".json")
    }
    for path in sorted((root / "codey").rglob("*")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if path.suffix != ".py" and rel not in json_surface:
            continue
        digest.update(rel.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _provider_dir(provider_id: str, state_home: str | Path | None) -> Path:
    return overrides_root(state_home) / normalize_provider_id(provider_id)


def _index_path(provider_id: str, state_home: str | Path | None) -> Path:
    return _provider_dir(provider_id, state_home) / "index.json"


def _load_index(provider_id: str, state_home: str | Path | None) -> dict[str, Any]:
    data = read_json(_index_path(provider_id, state_home), max_bytes=MAX_INDEX_BYTES) or {}
    if data.get("schema_version") != 1 or not isinstance(data.get("generations"), dict):
        return {
            "schema_version": 1,
            "provider_id": normalize_provider_id(provider_id),
            "current_generation": 0,
            "previous_generation": 0,
            "generations": {},
        }
    return data


def _save_index(provider_id: str, state_home: str | Path | None, index: dict[str, Any]) -> None:
    write_json_atomic(_index_path(provider_id, state_home), index, max_bytes=MAX_INDEX_BYTES)


def _generations(index: dict[str, Any]) -> dict[str, Any]:
    generations = index.get("generations")
    return generations if isinstance(generations, dict) else {}


def _current_generation(index: dict[str, Any]) -> int:
    try:
        return max(0, int(index.get("current_generation") or 0))
    except (TypeError, ValueError):
        return 0


def _next_generation(index: dict[str, Any]) -> int:
    generations = _generations(index)
    numbers = [int(key) for key in generations if str(key).isdigit()]
    return (max(numbers) if numbers else 0) + 1


def _record(index: dict[str, Any], generation: int) -> dict[str, Any] | None:
    record = _generations(index).get(str(int(generation or 0)))
    return record if isinstance(record, dict) else None


def _base_matches(
    provider_id: str,
    record: dict[str, Any],
    current_root: str | Path | None,
) -> bool:
    base_version = str(record.get("base_version") or "")
    base_hash = str(record.get("base_hash") or "")
    return bool(
        base_version
        and base_hash
        and base_version == __version__
        and base_hash == adapter_base_hash(provider_id, current_root)
    )


def _override_from_record(
    provider_id: str,
    generation: int,
    record: dict[str, Any],
) -> AdapterOverride:
    return AdapterOverride(
        provider_id=provider_id,
        generation=generation,
        status=str(record.get("status") or ""),
        root=Path(str(record.get("path") or "")),
        success_count=max(0, int(record.get("success_count") or 0)),
        failure_count=max(0, int(record.get("failure_count") or 0)),
    )


def _trim_generations(index: dict[str, Any]) -> None:
    generations = _generations(index)
    keep = {
        str(index.get("current_generation") or ""),
        str(index.get("previous_generation") or ""),
    }
    ordered = sorted((int(key), key) for key in generations if str(key).isdigit())
    for _number, key in ordered[:-MAX_GENERATIONS]:
        if key in keep:
            continue
        record = generations.pop(key, None)
        if isinstance(record, dict):
            try:
                shutil.rmtree(Path(str(record.get("path") or "")))
            except OSError:
                pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()