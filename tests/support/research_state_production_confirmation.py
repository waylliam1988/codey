"""Compare real production with the frozen original functions on fresh seeds."""

import argparse
import hashlib
import runpy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import research_partial_edit_candidate as partial

_root = None


@contextmanager
def treatment(arm):
    if arm != "frozen_control":
        if arm not in {"production", "baseline"}:
            raise ValueError(arm)
        yield
        return
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research import answer_context, answer_contract
    from tests.manual.research_framework_comparison import read_json

    files = read_json(_root / "original-runtime-sha256.json")
    loaded = {}
    for name, expected in files.items():
        path = _root / "original-runtime" / Path(name).name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Original source control changed: " + name)
        loaded[path.stem] = runpy.run_path(str(path))
    with patch.object(answer_context, "_render_context", loaded["answer_context"]["_render_context"]), \
            patch.object(answer_contract, "answer_spec", loaded["answer_contract"]["answer_spec"]), \
            patch.object(ExecutionDelegate, "_commit_research_knowledge",
                         loaded["task_execution"]["ExecutionDelegate"]._commit_research_knowledge):
        yield


def main():
    global _root
    from tests.manual import research_framework_comparison as framework
    from tests.manual import research_state_fidelity_ab as first

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, required=True)
    args, _ = parser.parse_known_args()
    _root = args.root.resolve()
    original_freeze = first.freeze

    def freeze(root):
        original_freeze(root)
        hashes = framework.read_json(root / "frozen-inputs.json")
        for path in [root / "original-runtime-sha256.json", *sorted((root / "original-runtime").glob("*.py"))]:
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        framework.write_json(root / "frozen-inputs.json", hashes)

    with patch.object(first, "freeze", freeze), patch.object(partial, "treatment", treatment), \
            patch.object(partial, "__spec__", SimpleNamespace(name=__spec__.name)), \
            patch.object(partial, "__file__", str(Path(__file__).resolve())):
        partial.main()


if __name__ == "__main__":
    main()
