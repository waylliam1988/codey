"""Recovery accepts the declared Mapping payload contract."""
from __future__ import annotations

import types
import unittest


class RecoveryMappingFieldsAcceptMappingTests(unittest.TestCase):
    def test_mapping_proxy_payloads_are_rebuilt(self) -> None:
        from codey.operations.kernel_recovery_result import RecoveredResultSpec, build_recovered_result
        from codey.runtime.core.models import ToolCall

        result = build_recovered_result(
            RecoveredResultSpec(
                ok=True, call=ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1"),
                model_text="ok",
                audit=types.MappingProxyType({"ok": True}),
                presentation=types.MappingProxyType({"result": "ok"}),
                canonical=types.MappingProxyType({"value": "ok"}),
            )
        )
        self.assertEqual(dict(result.audit), {"ok": True})


if __name__ == "__main__":
    unittest.main()
