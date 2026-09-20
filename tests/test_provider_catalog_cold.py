"""Cold-start catalog: static ids never pay the Playwright tax."""

from __future__ import annotations

import subprocess
import sys
import unittest


class ProviderCatalogColdTests(unittest.TestCase):
    def test_catalog_import_pulls_no_browser_driver(self) -> None:
        script = (
            "import sys; "
            "import codey.providers.catalog as catalog; "
            "catalog.provider_ids(); "
            "heavy = sorted(m for m in sys.modules "
            "if 'playwright' in m or m == 'codey.automation.browser' "
            "or m == 'codey.providers.web_provider' "
            "or m == 'codey.providers.worker'); "
            "print(','.join(heavy))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "")

    def test_package_statics_resolve_to_catalog(self) -> None:
        import codey.providers as providers
        import codey.providers.catalog as catalog

        self.assertIs(providers.DEFAULT_PROVIDER_ID, catalog.DEFAULT_PROVIDER_ID)
        self.assertIs(providers.PROVIDER_LABELS, catalog.PROVIDER_LABELS)
        self.assertEqual(providers.provider_ids(), catalog.provider_ids())

    def test_registry_statics_match_catalog(self) -> None:
        import codey.providers.catalog as catalog
        import codey.providers.registry as registry

        self.assertEqual(registry.DEFAULT_PROVIDER_ID, catalog.DEFAULT_PROVIDER_ID)
        self.assertEqual(registry.PROVIDER_LABELS, catalog.PROVIDER_LABELS)
        self.assertEqual(registry.WEB_PROVIDER_LABELS, catalog.WEB_PROVIDER_LABELS)
        self.assertEqual(
            registry.PROVIDER_WORKER_PORT_OFFSETS, catalog.PROVIDER_WORKER_PORT_OFFSETS
        )
        self.assertEqual(registry.WORKER_CHILD_ENV, catalog.WORKER_CHILD_ENV)
        self.assertEqual(registry.provider_ids(), catalog.provider_ids())
        self.assertEqual(set(registry.PROVIDER_TYPES), set(catalog.PROVIDER_LABELS))


if __name__ == "__main__":
    unittest.main()
