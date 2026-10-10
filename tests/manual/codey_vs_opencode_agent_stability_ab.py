# ruff: noqa: E402 -- direct execution adds the repository root.
"""Native Codey versus installed OpenCode; shared cases, budgets and scoring."""
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.manual.agent_stability_cases import TASK_CASES
from tests.manual.behavioral_verification_local_holdout_ab import holdout_cases
from tests.manual.codey_vs_pi_agent_stability_ab import main

if __name__ == '__main__':
    raise SystemExit(main(cases=(*TASK_CASES, *holdout_cases()), opponent='opencode'))
