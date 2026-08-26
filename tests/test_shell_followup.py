from __future__ import annotations

import unittest

from codey.policies.shell_followup import ShellFollowupInput, render_shell_followup
from codey.completion.verification_policy import VerificationCandidate


class ShellFollowupTests(unittest.TestCase):
    def test_dependency_install_success_suggests_checks_without_claiming_tests(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="dependency_install",
            exit_code=0,
            output="added 120 packages",
            verification_candidates=(
                VerificationCandidate("npm test", ".", "package", previously_passed=False),
                VerificationCandidate(
                    "npm run lint",
                    "frontend",
                    "previously successful check",
                    previously_passed=True,
                ),
            ),
        ))

        self.assertIn("Follow-up hints:", text)
        self.assertIn("exited with code 0", text)
        self.assertIn("manifest or lockfiles", text)
        self.assertIn("frontend/: npm run lint.", text)
        self.assertIn("Do not claim tests passed", text)
        self.assertIn("internal guidance", text)

    def test_dependency_install_success_keeps_safety_hint_when_output_is_noisy(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="dependency_install",
            exit_code=0,
            output=(
                "npm ERR! permission denied\n"
                "command timed out\n"
                "network registry error"
            ),
            truncated=True,
            verification_candidates=(
                VerificationCandidate("npm test", ".", "package"),
                VerificationCandidate("npm run lint", "frontend", "package"),
            ),
        ))

        self.assertIn("Do not claim tests passed until a run tool result shows it.", text)
        self.assertIn("A trusted local check is available: npm test.", text)
        self.assertNotIn("npm test in ..", text)

    def test_dependency_install_failure_with_missing_executable(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="dependency_install",
            exit_code=1,
            output="'npm' is not recognized as an internal or external command",
        ))

        self.assertIn("failed with exit code 1", text)
        self.assertIn("executable may be missing", text)
        self.assertIn("Inspect the install output", text)
        self.assertIn("Do not retry broader install commands", text)

    def test_dependency_install_failure_keeps_guardrails_before_noisy_output(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="dependency_install",
            exit_code=1,
            output=(
                "npm is not recognized\n"
                "permission denied\n"
                "command timed out\n"
                "network error"
            ),
        ))

        self.assertIn("Do not retry broader install commands", text)
        self.assertIn("Do not claim tests passed", text)
        self.assertIn("executable may be missing", text)
        self.assertLess(
            text.index("Do not retry broader install commands"),
            text.index("executable may be missing"),
        )

    def test_system_install_success_mentions_path_refresh(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="system_install",
            exit_code=0,
            output="Successfully installed",
        ))

        self.assertIn("new terminal or PATH refresh", text)
        self.assertIn("project is verified", text)

    def test_external_source_success_mentions_readme_before_running(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="external_source",
            exit_code=0,
            output="Cloning into repo",
        ))

        self.assertIn("Read README or manifest files", text)
        self.assertIn("Do not assume external source is safe", text)

    def test_dev_server_timeout_is_not_build_failure(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="dev_server",
            exit_code=None,
            output="command timed out after 60s",
        ))

        self.assertIn("did not return a normal exit code", text)
        self.assertIn("long-running server", text)
        self.assertIn("not managing a background dev server", text)

    def test_publish_success_requires_output_or_status_confirmation(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="publish",
            exit_code=0,
            output="pushed",
            verification_candidates=(VerificationCandidate("python -m pytest"),),
        ))

        self.assertIn("pushed or released", text)
        self.assertIn("A trusted local check is available: python -m pytest.", text)
        self.assertNotIn("python -m pytest in ..", text)

    def test_verification_candidates_keep_discovery_order_after_prior_success(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="publish",
            exit_code=0,
            verification_candidates=(
                VerificationCandidate("npm run z", "z"),
                VerificationCandidate("npm run a", "a"),
            ),
        ))

        self.assertLess(text.index("npm run z"), text.index("npm run a"))
        self.assertIn("z/: npm run z", text)
        self.assertIn("a/: npm run a", text)

    def test_not_found_text_does_not_imply_missing_executable(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="generic",
            exit_code=1,
            output="npm ERR! 404 Not Found\nNo tests found",
        ))

        self.assertNotIn("executable may be missing", text)

    def test_generic_is_minimal(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="generic",
            exit_code=0,
            output="clean",
        ))

        self.assertIn("exited with code 0", text)
        self.assertIn("Inspect the shell exit code and output", text)
        self.assertNotIn("manifest", text)
        self.assertNotIn("README", text)
        self.assertNotIn("internal guidance", text)

    def test_truncated_output_hint_is_explicit(self) -> None:
        text = render_shell_followup(ShellFollowupInput(
            risk_label="generic",
            exit_code=1,
            output="HEAD\nTAIL",
            truncated=True,
        ))

        self.assertIn("output was truncated", text)


if __name__ == "__main__":
    unittest.main()
