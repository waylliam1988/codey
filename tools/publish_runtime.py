"""Build the same runtime descriptor for a checkout and a wheel."""
from __future__ import annotations

import ast
import json
import tomllib
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from codey.launcher.runtime import DESCRIPTOR, package_files
from codey.repairs.policy import PROVIDER_TEST_FILES, readonly_reference_files
from codey.repairs.sandbox import REPAIR_REFERENCE_BUNDLE
from codey.storage.atomic_io import write_bytes_atomic

ROOT = Path(__file__).resolve().parents[1]


def publish_runtime(root: Path, project_file: Path, *, version: str | None = None) -> None:
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
    if version is None:
        version = project.get("version")
    if version is None:
        tree = ast.parse((root / "codey/__init__.py").read_text(encoding="utf-8"))
        version = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))
    if (root / "codey/repairs/policy.py").is_file():
        _publish_repair_references(root, project_file)
    data = {"schema": 1, "version": version, "requires_python": project["requires-python"],
            "dependencies": project.get("dependencies", []),
            "mcp_dependencies": project.get("optional-dependencies", {}).get("mcp", []),
            "files": package_files(root)}
    path = root / DESCRIPTOR
    content = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


def _publish_repair_references(root: Path, project_file: Path) -> None:
    reference_paths = {name for provider in PROVIDER_TEST_FILES for name in readonly_reference_files(provider)}
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        for name in (*sorted(reference_paths), "pyproject.toml"):
            source = project_file.parent / name
            archive.writestr(ZipInfo(name), source.read_bytes().replace(b"\r\n", b"\n"), compress_type=ZIP_DEFLATED)
    target = root / REPAIR_REFERENCE_BUNDLE
    data = content.getvalue()
    if not target.exists() or target.read_bytes() != data:
        write_bytes_atomic(target, data)


if __name__ == "__main__":
    publish_runtime(ROOT, ROOT / "pyproject.toml")
