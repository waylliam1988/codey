"""Freeze evaluation source identity and stop before inference on any change."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def source_identity():
    from codey.operations.provider_receipts import runtime_implementation_id

    runtime_implementation_id.cache_clear()
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in sorted((root / "tests/manual").glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return {"runtime_id": runtime_implementation_id(), "harness_digest": digest.hexdigest(),
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, encoding="utf-8").strip()}


def verify_experiment(root):
    path = Path(root) / "experiment-source.json"
    expected = json.loads(path.read_text(encoding="utf-8"))
    if {**source_identity(), "materials": material_identity(root)} != expected:
        raise RuntimeError("Evaluation source changed; stop this matrix before further inference and use a new frozen directory.")


def freeze_experiment(root):
    path = Path(root) / "experiment-source.json"
    if path.exists():
        verify_experiment(root)
    else:
        with path.open("x", encoding="utf-8") as stream:
            json.dump({**source_identity(), "materials": material_identity(root)}, stream, indent=2)


def material_identity(root):
    names = ("fixtures.json", "wal-material.json", "asyncio-material.json", "sqlite-sources.json",
             "material-channel.json", "limits.json")
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name in names
            if (path := Path(root) / name).exists()}
