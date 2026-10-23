"""Build the same runtime descriptor for a checkout and a wheel."""
from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

from codey.launcher.runtime import DESCRIPTOR, package_files

ROOT = Path(__file__).resolve().parents[1]


def publish_runtime(root: Path, project_file: Path, *, version: str | None = None) -> None:
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))["project"]
    if version is None:
        version = project.get("version")
    if version is None:
        tree = ast.parse((root / "codey/__init__.py").read_text(encoding="utf-8"))
        version = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets))
    data = {"schema": 1, "version": version, "requires_python": project["requires-python"],
            "dependencies": project.get("dependencies", []),
            "mcp_dependencies": project.get("optional-dependencies", {}).get("mcp", []),
            "files": package_files(root)}
    path = root / DESCRIPTOR
    content = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if not path.exists() or path.read_bytes() != content:
        path.write_bytes(content)


if __name__ == "__main__":
    publish_runtime(ROOT, ROOT / "pyproject.toml")
