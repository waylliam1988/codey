"""Lock brand-free environment naming for a future project rename.

Single source of truth is :mod:`codey.env_names`. These tests pin the exact
strings (so a rename is one deliberate edit, never drift) and fail if a
``CODEY_``-prefixed name appears anywhere else in the repo. Append-only
history docs (CHANGELOG/TEST_REPORT) are exempt: they record the past.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from codey import env_names

ROOT = Path(__file__).resolve().parents[1]

# Files allowed to mention the retired prefix: the single source (which
# documents the ban), this lock test itself, and append-only history
# documents (their old entries must stay byte-identical).
CODEY_PREFIX_ALLOWLIST = frozenset({
    "codey/env_names.py",
    "tests/test_env_names.py",
    "CHANGELOG.md",
    "CHANGELOG.zh-CN.md",
    "TEST_REPORT.md",
})

_SKIP_DIRS = frozenset({
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".e2e-artifacts",
    "codey.egg-info",
    "node_modules",
    ".venv",
})


class EnvNameValueTests(unittest.TestCase):
    def test_canonical_values_have_no_brand_prefix(self) -> None:
        expected = {
            "NATIVE_TOOLS_ENV": "NATIVE_TOOLS",
            "BROWSER_PATH_ENV": "BROWSER_PATH",
            "LOCAL_OPENAI_BASE_URL_ENV": "LOCAL_OPENAI_BASE_URL",
            "LOCAL_OPENAI_MODEL_ENV": "LOCAL_OPENAI_MODEL",
            "LOCAL_OPENAI_API_KEY_ENV": "LOCAL_OPENAI_API_KEY",
            "LOCAL_OPENAI_CONTEXT_WINDOW_ENV": "LOCAL_OPENAI_CONTEXT_WINDOW",
            "LOCAL_OPENAI_CONTEXT_RESERVE_ENV": "LOCAL_OPENAI_CONTEXT_RESERVE",
            "LOCAL_OPENAI_CONTEXT_KEEP_ENV": "LOCAL_OPENAI_CONTEXT_KEEP",
            "PROVIDER_WORKER_CHILD_ENV": "PROVIDER_WORKER_CHILD",
            "PROVIDER_CDP_PORT_ENV": "PROVIDER_CDP_PORT",
            "REVIEW_POLICY_ENV": "REVIEW_POLICY",
            "RUN_BROWSER_E2E_ENV": "RUN_BROWSER_E2E",
        }
        for attr, value in expected.items():
            with self.subTest(attr=attr):
                self.assertEqual(getattr(env_names, attr), value)
        self.assertEqual(env_names.APP_VERSION_PLACEHOLDER, "__APP_VERSION__")

    def test_module_constants_match_single_source(self) -> None:
        from codey.automation import browser
        from codey.providers import catalog, local_config

        self.assertIs(browser.BROWSER_PATH_ENV, env_names.BROWSER_PATH_ENV)
        self.assertIs(catalog.WORKER_CHILD_ENV, env_names.PROVIDER_WORKER_CHILD_ENV)
        self.assertIs(local_config.LOCAL_OPENAI_BASE_URL_ENV, env_names.LOCAL_OPENAI_BASE_URL_ENV)
        self.assertIs(local_config.LOCAL_OPENAI_MODEL_ENV, env_names.LOCAL_OPENAI_MODEL_ENV)
        self.assertIs(local_config.LOCAL_OPENAI_API_KEY_ENV, env_names.LOCAL_OPENAI_API_KEY_ENV)

    def test_native_gate_reads_canonical_name(self) -> None:
        import inspect

        from codey.agents import loop
        from codey.providers import local_config
        from codey.research import native_bridge

        source = inspect.getsource(local_config)
        self.assertIn("NATIVE_TOOLS_ENV", source)
        self.assertNotIn("CODEY_", source)
        for module in (local_config, loop, native_bridge):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertIn("resolve_local_native_tools", source)
                self.assertNotIn("CODEY_", source)

    def test_no_brand_prefixed_name_outside_allowlist(self) -> None:
        import subprocess

        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        offenders: list[str] = []
        for raw in completed.stdout.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", errors="surrogateescape")
            if rel in CODEY_PREFIX_ALLOWLIST:
                continue
            if any(part in _SKIP_DIRS for part in Path(rel).parts):
                continue
            path = ROOT / rel
            if not path.is_file():
                continue
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if "CODEY_" in text:
                offenders.append(rel)
        self.assertEqual(sorted(offenders), [])


if __name__ == "__main__":
    unittest.main()
