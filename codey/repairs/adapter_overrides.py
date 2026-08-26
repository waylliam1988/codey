"""Versioned local Provider adapter overrides with rollback.

An installed override carries ONLY the provider's adapter repair surface
plus generated package shims; every other module keeps loading from the
live Codey installation through the shims' extended ``__path__``. A repair
therefore can never snapshot unrelated runtime code into an override, and
a candidate that is missing surface files fails closed instead of
installing a partial web adapter.
"""

from __future__ import annotations

import shutil
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import codey
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

_PACKAGE_SHIM_TEMPLATE = '''"""Generated adapter-override package shim (do not edit).

Extends this package's module search path with the installed Codey package
so the override only carries the adapter repair surface; every other module
keeps loading from the live installation.
"""

import os as _os

_base = {base}
_norm = _os.path.normcase(_os.path.normpath(_base)) if _base else ""
if _norm and all(_os.path.normcase(_os.path.normpath(p)) != _norm for p in __path__):
    __path__.append(_base)

_base_init = _os.path.join(_base, "__init__.py")
if _os.path.isfile(_base_init):
    with open(_base_init, "rb") as _f:
        exec(compile(_f.read(), _base_init, "exec"))
'''


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
    if not adapter_repair_surface(provider_id):
        raise ValueError("provider has no adapter repair surface")
    source_root = Path(source_root).resolve()
    base_version = str(base_version or __version__)
    base_package = running_package_root()
    if base_package is None:
        raise ValueError("running codey package root could not be determined")
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
    _copy_adapter_surface(
        source_root,
        generation_dir,
        provider_id,
        base_package=base_package,
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
        "base_package": str(base_package),
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
    _trim_generations(index, state_home)
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


def running_package_root() -> Path | None:
    """The live Codey package directory this process is executing from."""

    package_file = getattr(codey, "__file__", None)
    if not package_file:
        return None
    root = Path(package_file).resolve().parent
    return root if root.is_dir() else None


def adapter_base_hash(provider_id: str, source_root: str | Path | None = None) -> str:
    """Hash exactly the repair surface an override was generated against.

    The override only shadows its surface files, so validity tracks those
    files alone: unrelated runtime changes must not invalidate (or silently
    keep) an override whose shadowed behavior did not move.
    """

    root = Path(source_root).resolve() if source_root is not None else Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    digest.update(normalize_provider_id(provider_id).encode("utf-8"))
    for rel in sorted(set(adapter_repair_surface(normalize_provider_id(provider_id)))):
        digest.update(rel.encode("utf-8"))
        try:
            digest.update((root / rel).read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _assert_inside(base: Path, target: Path, label: str) -> None:
    base_resolved = base.resolve()
    target_resolved = Path(str(target)).resolve()
    if target_resolved != base_resolved and base_resolved not in target_resolved.parents:
        raise ValueError(f"{label} escapes {base_resolved}")


def _package_init_paths_for(rels: tuple[str, ...]) -> tuple[str, ...]:
    inits: list[str] = []
    seen: set[str] = set(rels)
    for rel in rels:
        parts = PurePosixPath(rel).parts[:-1]
        for depth in range(1, len(parts) + 1):
            init = "/".join((*parts[:depth], "__init__.py"))
            if init not in seen:
                seen.add(init)
                inits.append(init)
    return tuple(inits)


def _package_shim_text(base_package: Path) -> str:
    return _PACKAGE_SHIM_TEMPLATE.format(base=json.dumps(str(base_package)))


def _copy_adapter_surface(
    source_root: Path,
    destination_root: Path,
    provider_id: str,
    *,
    base_package: Path,
) -> None:
    surface = adapter_repair_surface(provider_id)
    # Validate every source before touching the destination at all: a
    # candidate missing a surface file must fail closed without leaving a
    # partial override behind.
    for rel in surface:
        if not (source_root / rel).is_file():
            raise ValueError(f"candidate source is missing adapter surface file: {rel}")
    destination_root.mkdir(parents=True, exist_ok=True)
    _assert_inside(destination_root, destination_root / "codey", "override package")
    for init in _package_init_paths_for(surface):
        shim = destination_root / Path(*PurePosixPath(init).parts)
        _assert_inside(destination_root, shim, "override shim")
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(_package_shim_text(base_package), encoding="utf-8")
    for rel in surface:
        target = destination_root / Path(*PurePosixPath(rel).parts)
        _assert_inside(destination_root, target, "override file")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / rel, target)


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
    stored_package = str(record.get("base_package") or "")
    running_package = running_package_root()
    return bool(
        base_version
        and base_hash
        and base_version == __version__
        # The shims extend __path__ with this exact directory; if the
        # installation moved (or the record predates the field), the
        # overlay would resolve against a stale base.
        and running_package is not None
        and stored_package
        and Path(stored_package).resolve() == running_package
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


def _trim_generations(
    index: dict[str, Any],
    state_home: str | Path | None = DEFAULT_STATE_HOME,
) -> None:
    generations = _generations(index)
    keep = {
        str(index.get("current_generation") or ""),
        str(index.get("previous_generation") or ""),
    }
    provider_id = normalize_provider_id(str(index.get("provider_id") or ""))
    provider_dir = (
        _provider_dir(provider_id, state_home)
        if provider_id
        else None
    )
    ordered = sorted((int(key), key) for key in generations if str(key).isdigit())
    for _number, key in ordered[:-MAX_GENERATIONS]:
        if key in keep:
            continue
        record = generations.pop(key, None)
        if not isinstance(record, dict):
            continue
        raw_path = str(record.get("path") or "").strip()
        if not raw_path or not provider_dir:
            continue
        try:
            _assert_inside(provider_dir, Path(raw_path), "generation path")
            shutil.rmtree(Path(raw_path))
        except (ValueError, OSError):
            # Outside the provider override dir the index entry alone is
            # dropped; disk content owned elsewhere is never touched.
            pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()