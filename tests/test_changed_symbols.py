from __future__ import annotations

import unittest

from codey.workspace.changed_symbols import changed_symbol_names, changed_symbols_from_changes


class ChangedSymbolsTests(unittest.TestCase):
    def test_extracts_renamed_export_with_old_lookup_name(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/format.ts", "status": "M"}],
            "diff": (
                "diff --git a/src/format.ts b/src/format.ts\n"
                "--- a/src/format.ts\n"
                "+++ b/src/format.ts\n"
                "@@ -1,3 +1,3 @@\n"
                "-export function formatTotal(cents: number): string {\n"
                "+export function formatCurrency(cents: number): string {\n"
                "   return `$${(cents / 100).toFixed(2)}`;\n"
                " }\n"
            ),
        }

        symbols = changed_symbols_from_changes(changes)

        self.assertEqual(len(symbols), 1)
        symbol = symbols[0]
        self.assertEqual(symbol.path, "src/format.ts")
        self.assertEqual(symbol.name, "formatCurrency")
        self.assertEqual(symbol.old_name, "formatTotal")
        self.assertEqual(symbol.lookup_name, "formatTotal")
        self.assertEqual(symbol.label, "formatTotal -> formatCurrency")
        self.assertEqual(symbol.hunk_index, 1)
        self.assertEqual(symbol.new_line, 1)
        self.assertEqual(
            changed_symbol_names(symbols, include_old_names=True),
            ("formatTotal", "formatCurrency"),
        )

    def test_normalizes_git_rename_display_path_to_new_path(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "old.py -> new.py", "status": "R"}],
            "diff": (
                "diff --git a/old.py b/new.py\n"
                "similarity index 85%\n"
                "rename from old.py\n"
                "rename to new.py\n"
                "--- a/old.py\n"
                "+++ b/new.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-def old_name():\n"
                "+def new_name():\n"
            ),
        }

        symbols = changed_symbols_from_changes(changes)

        self.assertEqual(symbols[0].path, "new.py")
        self.assertEqual(symbols[0].old_name, "old_name")
        self.assertEqual(symbols[0].name, "new_name")

    def test_single_file_loose_diff_still_finds_symbol(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": "+def validate_token(value):\n+    return value\n",
        }

        symbols = changed_symbols_from_changes(changes)

        self.assertEqual(symbols[0].path, "app.py")
        self.assertEqual(symbols[0].name, "validate_token")
        self.assertEqual(symbols[0].hunk_index, 1)

    def test_does_not_pair_different_kinds_as_rename(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": (
                "diff --git a/app.py b/app.py\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-def build_user():\n"
                "-    return {}\n"
                "+class UserBuilder:\n"
                "+    pass\n"
            ),
        }

        symbols = changed_symbols_from_changes(changes)

        self.assertEqual(
            [(item.kind, item.name, item.old_name) for item in symbols],
            [
                ("function", "build_user", "build_user"),
                ("class", "UserBuilder", ""),
            ],
        )
        self.assertNotIn("build_user -> UserBuilder", [item.label for item in symbols])

    def test_extracts_python_constant(self) -> None:
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "settings.py", "status": "M"}],
            "diff": (
                "diff --git a/settings.py b/settings.py\n"
                "--- a/settings.py\n"
                "+++ b/settings.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-MAX_RETRIES = 2\n"
                "+MAX_ATTEMPTS = 3\n"
            ),
        }

        symbols = changed_symbols_from_changes(changes)

        self.assertEqual(symbols[0].kind, "constant")
        self.assertEqual(symbols[0].name, "MAX_ATTEMPTS")
        self.assertEqual(symbols[0].old_name, "MAX_RETRIES")


if __name__ == "__main__":
    unittest.main()
