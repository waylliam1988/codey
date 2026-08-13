"""UI asset architecture ratchet.

Keeps the zero-build web UI honest: index.html stays a thin core
(state/SSE/composer/boot) while reusable UI lives in versioned assets.
Budgets may only go DOWN as more code moves into assets; never raise them.
"""

import re
import unittest
from pathlib import Path


WEB_DIR = Path(__file__).resolve().parents[1] / "codey" / "web"
ASSET_DIR = WEB_DIR / "assets"
HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")

# Ratchet budgets. Lower these as checkpoints land; never increase.
INLINE_STYLE_LINE_BUDGET = 0
INLINE_SCRIPT_LINE_BUDGET = 1950

VERSION_SUFFIX = "?v=__CODEY_VERSION__"

ASSET_REFS = re.findall(r'(?:src|href)="(/assets/[^"]+)"', HTML)
INLINE_SCRIPTS = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", HTML, re.S)
INLINE_STYLES = re.findall(r"<style[^>]*>(.*?)</style>", HTML, re.S)


class AssetReferenceTests(unittest.TestCase):
    def test_index_references_at_least_the_core_assets(self) -> None:
        names = [ref.split("?")[0] for ref in ASSET_REFS]
        self.assertIn("/assets/tokens.css", names)
        self.assertIn("/assets/app.css", names)
        self.assertIn("/assets/render.js", names)
        self.assertIn("/assets/research_graph.js", names)
        self.assertIn("/assets/research_drawer.js", names)
        self.assertIn("/assets/changes_drawer.js", names)
        self.assertIn("/assets/local_context_drawer.js", names)
        self.assertIn("/assets/provider_ui.js", names)

    def test_every_referenced_asset_file_exists(self) -> None:
        for ref in ASSET_REFS:
            name = ref.split("?")[0].removeprefix("/assets/")
            self.assertTrue(
                (ASSET_DIR / name).is_file(),
                f"index.html references missing asset: {ref}",
            )

    def test_every_asset_reference_carries_version_placeholder(self) -> None:
        for ref in ASSET_REFS:
            self.assertTrue(
                ref.endswith(VERSION_SUFFIX),
                f"asset reference missing {VERSION_SUFFIX}: {ref}",
            )

    def test_no_stray_asset_files_outside_known_extensions(self) -> None:
        for path in ASSET_DIR.iterdir():
            self.assertIn(
                path.suffix,
                {".js", ".css"},
                f"unexpected asset file (server only serves js/css): {path.name}",
            )

    def test_asset_scripts_load_in_fixed_order_before_inline_core(self) -> None:
        # Synchronous, dependency-ordered loading; no defer / modules.
        scripts = [ref.split("?")[0] for ref in ASSET_REFS if ref.split("?")[0].endswith(".js")]
        self.assertEqual(
            scripts,
            [
                "/assets/render.js",
                "/assets/research_graph.js",
                "/assets/research_drawer.js",
                "/assets/changes_drawer.js",
                "/assets/local_context_drawer.js",
                "/assets/provider_ui.js",
            ],
        )


class AssetNamespaceTests(unittest.TestCase):
    def test_each_asset_js_declares_exactly_one_codey_namespace(self) -> None:
        for path in sorted(ASSET_DIR.glob("*.js")):
            source = path.read_text(encoding="utf-8")
            assigned = re.findall(r"window\.(Codey\w+)\s*=", source)
            self.assertEqual(
                len(assigned),
                1,
                f"{path.name} must assign exactly one window.Codey* namespace, got {assigned}",
            )

    def test_asset_js_uses_iife_strict_module_pattern(self) -> None:
        for path in sorted(ASSET_DIR.glob("*.js")):
            source = path.read_text(encoding="utf-8")
            self.assertIn("(function () {", source, f"{path.name} must be an IIFE module")
            self.assertIn("'use strict';", source, f"{path.name} must use strict mode")


class InlineBudgetTests(unittest.TestCase):
    def test_inline_style_stays_at_zero_lines(self) -> None:
        lines = sum(block.count("\n") for block in INLINE_STYLES)
        self.assertLessEqual(
            lines,
            INLINE_STYLE_LINE_BUDGET,
            "inline <style> crept back into index.html; put CSS in assets/*.css",
        )

    def test_inline_script_stays_within_ratchet_budget(self) -> None:
        lines = sum(block.count("\n") for block in INLINE_SCRIPTS)
        self.assertLessEqual(
            lines,
            INLINE_SCRIPT_LINE_BUDGET,
            "inline <script> grew past the ratchet budget; move UI modules into "
            "assets/*.js instead of growing index.html",
        )

    def test_index_keeps_a_single_inline_script_block(self) -> None:
        self.assertEqual(
            len(INLINE_SCRIPTS),
            1,
            "index.html should keep exactly one inline core script block",
        )

    def test_script_tags_stay_synchronous_classic_scripts(self) -> None:
        self.assertNotIn("<script defer", HTML)
        self.assertNotIn('type="module"', HTML)


if __name__ == "__main__":
    unittest.main()
