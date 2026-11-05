"""Checks for the import audit's package resolution and startup evidence limits."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.kernel_import_audit import module_paths, scan_imports, static_report


def write_modules(root: Path, sources: dict[str, str]) -> None:
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def test_relative_package_imports_and_type_guards_have_separate_graphs(tmp_path: Path) -> None:
    write_modules(tmp_path, {
        "codey/__init__.py": "",
        "codey/fixture/__init__.py": "",
        "codey/fixture/a.py": "from codey.fixture import b\n",
        "codey/fixture/b.py": (
            "from typing import TYPE_CHECKING\n"
            "def restore():\n    from . import a\n"
            "if TYPE_CHECKING:\n    import codey.fixture.c\n"
        ),
        "codey/fixture/c.py": "import codey.fixture.b\n",
    })
    modules = module_paths(tmp_path)
    graphs = static_report(modules, scan_imports(modules))
    assert graphs["top_level_runtime"]["components"] == []
    assert graphs["runtime_including_deferred"]["components"] == [["codey.fixture.a", "codey.fixture.b"]]
    assert graphs["including_type_checking"]["components"] == [[
        "codey.fixture.a", "codey.fixture.b", "codey.fixture.c",
    ]]


@pytest.mark.parametrize("define_before_call", [False, True])
def test_acyclic_top_level_graph_can_execute_a_deferred_import_during_startup(
    tmp_path: Path, define_before_call: bool,
) -> None:
    definition = "SENTINEL = 1\n"
    callback = "def load():\n    from codey.fixture import b\nload()\n"
    write_modules(tmp_path, {
        "codey/__init__.py": "",
        "codey/fixture/__init__.py": "",
        "codey/fixture/a.py": definition + callback if define_before_call else callback + definition,
        "codey/fixture/b.py": "from codey.fixture.a import SENTINEL\n",
    })
    modules = module_paths(tmp_path)
    graphs = static_report(modules, scan_imports(modules))
    assert graphs["top_level_runtime"]["scc_count"] == 0
    assert graphs["runtime_including_deferred"]["scc_count"] == 1
    script = (
        "import json, sys\n"
        "from tools.kernel_import_audit import cold_import\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "print(json.dumps(cold_import('codey.fixture.a')))\n"
    )
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True, timeout=20,
    )
    result = json.loads(child.stdout)
    assert bool(result["error"]) is not define_before_call
    assert any(event["function"] == "load" and event["source"] == "codey.fixture.a"
               for event in result["initialization_time_function_imports"])
    assert any(request[1] == "codey.fixture.a" for request in result["initializing_module_requests"])
