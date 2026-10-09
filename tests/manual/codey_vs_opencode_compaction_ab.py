"""Local algorithm comparison. Uses OpenCode core source, not its full runtime.

Example:
python -m tests.manual.codey_vs_opencode_compaction_ab --repeats 3
"""
import sys
from pathlib import Path

from tests.manual.context_compaction_benchmark_ab import main

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    sys.argv += ["--opencode-reference", str(root / "reference-projects/opencode")]
    if "--cases" not in sys.argv:
        sys.argv += ["--cases", "repeated,correction,test-result,tool-heavy,segmented,incremental"]
    if "--output" not in sys.argv:
        sys.argv += ["--output", "artifacts/context-compaction-ab/codey-vs-opencode.json"]
    raise SystemExit(main())
