from __future__ import annotations

import unittest

from codey.models import ToolCall, ToolResult
from codey.protocols import XmlToolCodec


class XmlToolCodecTests(unittest.TestCase):
    def test_parse_xml_tool_plan(self) -> None:
        codec = XmlToolCodec()
        plan = codec.parse(
            """<codey>
  <tool name="search"><path>.</path><query>LOGIN_BUG</query></tool>
  <control type="continue">need results</control>
</codey>"""
        )

        self.assertEqual(len(plan.calls), 1)
        self.assertEqual(plan.calls[0].name, "search")
        self.assertEqual(plan.calls[0].args["path"], ".")
        self.assertEqual(plan.calls[0].args["query"], "LOGIN_BUG")
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_old_marker_protocol_is_not_parsed(self) -> None:
        codec = XmlToolCodec()
        plan = codec.parse(
            """```text
# === codey: read path=app.py ===
```"""
        )

        self.assertEqual(plan.calls, [])
        self.assertIsNone(plan.control)

    def test_format_results_and_repair_prompt_remind_xml_only(self) -> None:
        codec = XmlToolCodec()
        call = ToolCall("read", {"path": "app.py"})
        prompt = codec.format_results([ToolResult(call, "content")])

        self.assertIn("<codey>...</codey>", prompt)
        self.assertIn("[read app.py]\ncontent", prompt)
        self.assertIn("<codey>...</codey>", codec.repair_prompt())


if __name__ == "__main__":
    unittest.main()
