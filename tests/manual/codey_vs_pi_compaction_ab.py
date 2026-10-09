"""Local Pi algorithm comparison; does not claim to run the complete Pi agent."""
import sys
from pathlib import Path

from tests.manual.context_compaction_benchmark_ab import main

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    sys.argv += ["--pi-reference", str(root / "reference-projects/pi")]
    if "--cases" not in sys.argv:
        sys.argv += ["--cases", "repeated,correction,test-result,tool-heavy,segmented,incremental"]
    if "--output" not in sys.argv:
        sys.argv += ["--output", "artifacts/context-compaction-ab/codey-vs-pi.json"]
    raise SystemExit(main())
