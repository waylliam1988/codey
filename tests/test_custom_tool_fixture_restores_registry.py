"""The third-task demonstration restores global tool registrations after running."""
import unittest
from unittest.mock import patch

from codey.toolchain import tool_spec


def test_third_task_fixture_restores_registry_and_is_repeatable():
    from tests.test_task_kernel_remaining import ThirdTaskTests

    baseline = dict(tool_spec.tool_specs())
    # Protect this outer test while reproducing the legacy fixture's leak.
    with patch.object(tool_spec, "_SPECS", dict(baseline)):
        for _ in range(2):
            case = ThirdTaskTests("test_third_task_runs_without_kernel_change")
            result = unittest.TestResult()
            case.run(result)
            assert result.testsRun == 1 and not result.skipped
            assert not result.errors and not result.failures
            assert tool_spec.tool_specs() == baseline
