from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import codey.policies.action as action_policy
from codey.policies.action import (
    ActionPolicyDecision,
    ActionPolicyPipeline,
    ActionSubject,
    DECISION_ALLOW,
    DECISION_ASK_USER,
    DECISION_DENY,
    MAX_MANAGED_OUTPUT_BYTES,
    MAX_MANAGED_OUTPUTS_PER_RUN,
    evaluate_action,
    merge_decisions,
    research_url_denial_reason,
)


class ActionPolicyTests(unittest.TestCase):
    def test_public_star_import_surface_is_narrow(self) -> None:
        self.assertEqual(
            tuple(action_policy.__all__),
            (
                "ActionPolicyDecision",
                "ActionPolicyPipeline",
                "ActionSubject",
                "DECISION_ALLOW",
                "DECISION_ASK_USER",
                "DECISION_DENY",
                "MAX_MANAGED_OUTPUT_BYTES",
                "MAX_MANAGED_OUTPUTS_PER_RUN",
                "evaluate_action",
            ),
        )
        self.assertNotIn("is_allowed_run_command", action_policy.__all__)
        self.assertNotIn("strip_python_flags", action_policy.__all__)

    def test_merge_decisions_is_monotonic(self) -> None:
        subject = ActionSubject("run_command", permission_profile="coding_writer")
        denied = ActionPolicyDecision.deny(
            subject,
            guard_id="first_guard",
            reason_code="denied_first",
            display="denied",
        )
        allowed = ActionPolicyDecision.allow(subject, guard_id="later_guard")
        asked = ActionPolicyDecision.ask_user(
            subject,
            guard_id="ask_guard",
            reason_code="needs_user",
            display="needs user",
        )

        self.assertIs(merge_decisions(denied, allowed), denied)
        self.assertIs(merge_decisions(denied, asked), denied)
        self.assertIs(merge_decisions(allowed, asked), asked)
        self.assertIs(merge_decisions(asked, denied), denied)

    def test_guard_exception_denies_dangerous_action(self) -> None:
        def broken(_subject: ActionSubject):
            raise RuntimeError("boom")

        decision = ActionPolicyPipeline((broken,)).evaluate(
            ActionSubject("edit_file", permission_profile="coding_writer"),
        )

        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "guard_exception")

    def test_unknown_action_kind_is_denied(self) -> None:
        decision = evaluate_action(ActionSubject(
            "delete_file",
            permission_profile="coding_writer",
        ))

        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "unknown_action")

    def test_workspace_escape_is_denied_without_raw_path_in_audit_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            secret = "../secret-token.txt"
            decision = evaluate_action(ActionSubject(
                "read_file",
                phase="writer",
                permission_profile="coding_writer",
                project=td,
                path=secret,
            ))

        payload = decision.to_audit_payload()
        serialized = json.dumps(payload)

        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "workspace_escape")
        self.assertNotIn(secret, serialized)

    def test_permission_profile_denies_write_and_run_for_planning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_decision = evaluate_action(ActionSubject(
                "edit_file",
                permission_profile="planning_readonly",
                project=td,
                path="app.py",
            ))
            run_decision = evaluate_action(ActionSubject(
                "run_command",
                permission_profile="planning_readonly",
                project=td,
                path=".",
                command="python -m pytest",
            ))

        self.assertEqual(write_decision.decision, DECISION_DENY)
        self.assertEqual(run_decision.decision, DECISION_DENY)
        self.assertEqual(write_decision.reason_code, "permission_profile_denied")
        self.assertEqual(run_decision.reason_code, "permission_profile_denied")

    def test_missing_permission_profile_denies_local_action_subjects(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cases = (
                ActionSubject("read_file", permission_profile="", project=td, path="app.py"),
                ActionSubject("write_file", permission_profile="", project=td, path="app.py"),
                ActionSubject("managed_output", permission_profile="", byte_count=1),
                ActionSubject(
                    "run_command",
                    permission_profile="",
                    project=td,
                    path=".",
                    command="python -m pytest",
                ),
                ActionSubject(
                    "shell",
                    permission_profile="",
                    project=td,
                    path=".",
                    command="npm install",
                    approval_available=True,
                ),
            )

            decisions = [evaluate_action(subject) for subject in cases]

        self.assertTrue(all(decision.decision == DECISION_DENY for decision in decisions))
        self.assertTrue(
            all(decision.reason_code == "unknown_permission_profile" for decision in decisions)
        )

    def test_run_command_guard_matches_existing_allowlist_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            allowed = evaluate_action(ActionSubject(
                "run_command",
                permission_profile="coding_writer",
                project=td,
                path=".",
                command="python -m pytest",
            ))
            denied = evaluate_action(ActionSubject(
                "run_command",
                permission_profile="coding_writer",
                project=td,
                path=".",
                command="python -m pip install requests",
            ))

        self.assertEqual(allowed.decision, DECISION_ALLOW)
        self.assertEqual(denied.decision, DECISION_DENY)
        self.assertEqual(denied.reason_code, "command_not_allowed")

    def test_run_command_guard_denies_python_script_paths_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project"
            project.mkdir()
            outside = base / "outside.py"
            outside.write_text("print('outside')\n", encoding="utf-8")
            (project / "inside.py").write_text("print('inside')\n", encoding="utf-8")

            allowed_py_compile = evaluate_action(ActionSubject(
                "run_command",
                permission_profile="coding_writer",
                project=str(project),
                path=".",
                command="python -m py_compile inside.py",
            ))
            denied_direct_script = evaluate_action(ActionSubject(
                "run_command",
                permission_profile="coding_writer",
                project=str(project),
                path=".",
                command="python inside.py",
            ))
            denied = [
                evaluate_action(ActionSubject(
                    "run_command",
                    permission_profile="coding_writer",
                    project=str(project),
                    path=".",
                    command=command,
                ))
                for command in (
                    "python ../outside.py",
                    f"python {outside.as_posix()}",
                    "python inside.py ../outside",
                    "python inside.py --config=../outside.py",
                    "python -m py_compile ../outside.py",
                )
            ]

        self.assertEqual(allowed_py_compile.decision, DECISION_ALLOW)
        self.assertEqual(denied_direct_script.decision, DECISION_DENY)
        self.assertEqual(denied_direct_script.reason_code, "command_not_allowed")
        self.assertTrue(all(decision.decision == DECISION_DENY for decision in denied))
        self.assertTrue(all(decision.reason_code == "command_path_escape" for decision in denied))

    def test_run_command_guard_denies_direct_python_scripts_even_inside_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "evil.py").write_text(
                "import os\nos.system('echo should-not-run')\n",
                encoding="utf-8",
            )

            decision = evaluate_action(ActionSubject(
                "run_command",
                permission_profile="coding_writer",
                project=str(root),
                path=".",
                command="python evil.py",
            ))

        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "command_not_allowed")

    def test_run_command_guard_denies_pytest_paths_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            project = base / "project"
            project.mkdir()
            outside = base / "outside"
            outside.mkdir()
            outside_ini = base / "pytest.ini"
            outside_ini.write_text("[pytest]\n", encoding="utf-8")

            allowed = evaluate_action(ActionSubject(
                "run_command",
                permission_profile="coding_writer",
                project=str(project),
                path=".",
                command="python -m pytest -q tests",
            ))
            denied = [
                evaluate_action(ActionSubject(
                    "run_command",
                    permission_profile="coding_writer",
                    project=str(project),
                    path=".",
                    command=command,
                ))
                for command in (
                    "pytest ../outside",
                    f"pytest {outside.as_posix()}",
                    "pytest -c ../pytest.ini",
                    "pytest --rootdir=..",
                    "pytest -o addopts=../outside",
                    "pytest -oaddopts=--basetemp=../outside/tmp -q",
                    "pytest -o pythonpath=../outside",
                    "pytest --override-ini=testpaths=../outside",
                    "python -m pytest --rootdir=../outside",
                    "python -m pytest -o addopts=../outside",
                    "python -m pytest -oaddopts=--rootdir=../outside",
                )
            ]

        self.assertEqual(allowed.decision, DECISION_ALLOW)
        self.assertTrue(all(decision.decision == DECISION_DENY for decision in denied))
        self.assertTrue(all(decision.reason_code == "command_path_escape" for decision in denied))

    def test_shell_requires_approval_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ask = evaluate_action(ActionSubject(
                "shell",
                permission_profile="coding_writer",
                project=td,
                path=".",
                command="npm install",
                approval_available=True,
            ))
            deny = evaluate_action(ActionSubject(
                "shell",
                permission_profile="coding_writer",
                project=td,
                path=".",
                command="npm install",
                approval_available=False,
            ))

        self.assertEqual(ask.decision, DECISION_ASK_USER)
        self.assertEqual(ask.reason_code, "requires_user_approval")
        self.assertEqual(deny.decision, DECISION_DENY)
        self.assertEqual(deny.reason_code, "approval_unavailable")

    def test_research_url_guard_rejects_invalid_port_without_exception(self) -> None:
        decision = evaluate_action(ActionSubject(
            "research_url",
            phase="research",
            permission_profile="research",
            url="http://example.com:99999/path",
        ))

        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertEqual(decision.reason_code, "invalid_url_port")
        self.assertEqual(decision.display, "invalid URL port")

    def test_research_url_guard_rejects_invalid_port_without_dns_resolution(self) -> None:
        self.assertEqual(
            research_url_denial_reason("http://example.com:99999/path", resolve=False),
            "invalid URL port",
        )

    def test_research_url_guard_fails_closed_on_malformed_hostnames(self) -> None:
        # Malformed hosts used to escape as UnicodeError from the resolver;
        # they must return a denial reason on every path, with or without
        # DNS resolution.
        for url in (
            "https://.gov/x",
            "https://evil..gov/x",
            "https://.edu/",
            "https://under_score.example/x",
        ):
            for resolve in (True, False):
                with self.subTest(url=url, resolve=resolve):
                    reason = research_url_denial_reason(url, resolve=resolve)
                    self.assertEqual(reason, "invalid URL host")

    def test_research_url_guard_still_allows_well_formed_hosts(self) -> None:
        self.assertIsNone(
            research_url_denial_reason("https://example.com/doc", resolve=False)
        )
        self.assertIsNone(
            research_url_denial_reason("https://sec.gov/report", resolve=False)
        )

    def test_research_url_guard_rejects_local_targets(self) -> None:
        decision = evaluate_action(ActionSubject(
            "research_url",
            phase="research",
            permission_profile="research",
            url="http://127.0.0.1/private",
        ))

        payload = decision.to_audit_payload()

        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertIn("non_public", decision.reason_code)
        self.assertNotIn("127.0.0.1", json.dumps(payload))

    def test_research_url_guard_rejects_cgnat_and_non_global_ips(self) -> None:
        # 100.64.0.0/10 (Shared Address Space / CGNAT) is not global and must be rejected
        self.assertEqual(
            research_url_denial_reason("http://100.64.0.1/", resolve=False),
            "refusing to open a non-public address",
        )

        with unittest.mock.patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("100.64.0.1", 443))]
            self.assertEqual(
                research_url_denial_reason("https://cgnat-domain.example/", resolve=True),
                "refusing to open a non-public address",
            )

    def test_managed_output_guards_count_and_size(self) -> None:
        count = evaluate_action(ActionSubject(
            "managed_output",
            permission_profile="coding_writer",
            item_count=MAX_MANAGED_OUTPUTS_PER_RUN,
            byte_count=1,
        ))
        size = evaluate_action(ActionSubject(
            "managed_output",
            permission_profile="coding_writer",
            item_count=0,
            byte_count=MAX_MANAGED_OUTPUT_BYTES + 1,
        ))

        self.assertEqual(count.decision, DECISION_DENY)
        self.assertEqual(count.reason_code, "managed_output_count_limit")
        self.assertEqual(size.decision, DECISION_DENY)
        self.assertEqual(size.reason_code, "managed_output_size_limit")

    def test_managed_output_requires_writer_verification_profile(self) -> None:
        allowed = evaluate_action(ActionSubject(
            "managed_output",
            permission_profile="coding_writer",
            byte_count=1,
        ))
        denied = evaluate_action(ActionSubject(
            "managed_output",
            permission_profile="planning_readonly",
            byte_count=1,
        ))

        self.assertEqual(allowed.decision, DECISION_ALLOW)
        self.assertEqual(denied.decision, DECISION_DENY)
        self.assertEqual(denied.reason_code, "permission_profile_denied")

    def test_policy_audit_payload_has_digests_not_raw_command_or_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            subject = ActionSubject(
                "run_command",
                phase="writer",
                permission_profile="coding_writer",
                project=td,
                path=".",
                command="python -m pip install secret-package",
                url="https://example.com/secret",
            )
            decision = evaluate_action(subject)

        payload = decision.to_audit_payload()
        serialized = json.dumps(payload)

        self.assertEqual(decision.decision, DECISION_DENY)
        self.assertIn("subject_ref", payload)
        self.assertIn("display_digest", payload)
        self.assertNotIn("secret-package", serialized)
        self.assertNotIn("example.com/secret", serialized)


if __name__ == "__main__":
    unittest.main()
