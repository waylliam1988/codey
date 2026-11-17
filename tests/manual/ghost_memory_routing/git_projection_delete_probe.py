"""Run the current projection probe against Ghost sources read from Git objects.

Only Python module contents are loaded from the requested commit. No checkout,
worktree, repository copy, or persistent state is created for those sources.
"""

from __future__ import annotations

import argparse
import importlib.abc
import importlib.util
import runpy
import subprocess
import sys
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[3]


class GitGhostLoader(importlib.abc.Loader):
    def __init__(self, source: str, filename: Path) -> None:
        self.source = source
        self.filename = filename

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        exec(compile(self.source, str(self.filename), "exec"), module.__dict__)


class GitGhostFinder(importlib.abc.MetaPathFinder):
    def __init__(self, revision: str) -> None:
        self.revision = subprocess.check_output(
            ["git", "rev-parse", "--verify", f"{revision}^{{commit}}"], cwd=ROOT, text=True,
        ).strip()
        paths = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", self.revision, "codey/ghost"], cwd=ROOT, text=True,
        ).splitlines()
        self.modules: dict[str, tuple[str, bool]] = {}
        for path in paths:
            if not path.endswith(".py"):
                continue
            package = path.endswith("/__init__.py")
            name = path.removesuffix("/__init__.py") if package else path.removesuffix(".py")
            self.modules[name.replace("/", ".")] = (path, package)

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        entry = self.modules.get(fullname)
        if entry is None:
            if fullname.startswith("codey.ghost."):
                raise ModuleNotFoundError(f"{fullname} does not exist at {self.revision}")
            return None
        relative, package = entry
        source = subprocess.check_output(
            ["git", "show", f"{self.revision}:{relative}"], cwd=ROOT, text=True, encoding="utf-8",
        )
        filename = ROOT / relative
        return importlib.util.spec_from_file_location(
            fullname, filename, loader=GitGhostLoader(source, filename),
            submodule_search_locations=[str(filename.parent)] if package else None,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    revision: object = arguments.revision
    output: object = arguments.output
    if not isinstance(revision, str) or not isinstance(output, Path):
        raise TypeError("revision must be text and output must be a path")
    finder = GitGhostFinder(revision)
    original_arguments = sys.argv
    sys.meta_path.insert(0, finder)
    sys.argv = [__file__, "--output", str(output)]
    try:
        print(f"Ghost production revision: {finder.revision}", flush=True)
        runpy.run_module("tests.manual.ghost_memory_routing.projection_delete_probe", run_name="__main__")
    finally:
        sys.argv = original_arguments
        sys.meta_path.remove(finder)


if __name__ == "__main__":
    main()
