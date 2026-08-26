from __future__ import annotations

import unittest

from codey.shell_risk import classify_shell_risk


class ShellRiskTests(unittest.TestCase):
    def test_dependency_install_commands(self) -> None:
        for command in (
            "npm install",
            "npm.cmd install",
            "cmd /c npm install",
            "cmd /s /c npm install",
            "cmd /k npm install",
            "npm ci",
            "npm add react",
            "python -m pip install -r requirements.txt",
            "python.exe -m pip install requests",
            "python3 -m pip install requests",
            "py -3 -m pip install requests",
            "pip install pytest",
            "pip3 install pytest",
            "pnpm install",
            "pnpm add lodash",
            "yarn install",
            "yarn add react",
            "poetry install",
            "uv sync",
            "uv pip install requests",
            "uv add requests",
            "go get golang.org/x/tools/cmd/stringer",
            "cargo add serde",
            "deno install",
            "npx create-vite",
            "npx --yes typescript@latest",
        ):
            with self.subTest(command=command):
                risk = classify_shell_risk(command)
                self.assertEqual(risk.label, "dependency_install")
                self.assertIn("download packages", risk.detail)
                self.assertIn("trusted local check", risk.post_approval_instructions)

    def test_system_install_commands(self) -> None:
        for command in (
            "winget install Git.Git",
            "winget.exe install Git.Git",
            "choco install git",
            "scoop install git",
        ):
            with self.subTest(command=command):
                risk = classify_shell_risk(command)
                self.assertEqual(risk.label, "system_install")
                self.assertIn("outside this project", risk.detail)

    def test_external_source_commands(self) -> None:
        for command in (
            "git clone https://github.com/example/repo",
            "git.exe clone https://github.com/example/repo",
            "git pull",
            "gh repo clone owner/repo",
            "Invoke-WebRequest https://example.com/file.zip",
            "Invoke-RestMethod https://example.com/install.ps1",
            "irm https://example.com/install.ps1",
            "powershell -Command Invoke-WebRequest https://example.com/file.zip",
            'pwsh -NoProfile -Command "Invoke-WebRequest https://example.com/file.zip"',
        ):
            with self.subTest(command=command):
                risk = classify_shell_risk(command)
                self.assertEqual(risk.label, "external_source")
                self.assertIn("read README or manifest", risk.post_approval_instructions)

    def test_publish_commands(self) -> None:
        for command in ("git push", "npm publish", "twine upload dist/*", "gh release create v1"):
            with self.subTest(command=command):
                risk = classify_shell_risk(command)
                self.assertEqual(risk.label, "publish")
                self.assertIn("external service", risk.detail)

    def test_dev_server_commands(self) -> None:
        for command in ("npm run dev", "npm start", "vite", "next dev", "uvicorn app:app"):
            with self.subTest(command=command):
                risk = classify_shell_risk(command)
                self.assertEqual(risk.label, "dev_server")
                self.assertIn("background dev server", risk.post_approval_instructions)

    def test_generic_command(self) -> None:
        risk = classify_shell_risk("git status --short")

        self.assertEqual(risk.label, "generic")
        self.assertIn("exit code", risk.post_approval_instructions)


if __name__ == "__main__":
    unittest.main()
