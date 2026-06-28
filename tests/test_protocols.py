from __future__ import annotations

import unittest

from codey.models import ToolCall, ToolResult
from codey.protocols import JsonToolCodec


class JsonToolCodecTests(unittest.TestCase):
    def test_parse_json_tool_call(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"grep","args":{"path":".","pattern":"LOGIN_BUG"}}'
        )

        self.assertEqual(len(plan.calls), 1)
        self.assertEqual(plan.calls[0].name, "search")
        self.assertEqual(plan.calls[0].args["path"], ".")
        self.assertEqual(plan.calls[0].args["query"], "LOGIN_BUG")
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_parse_done_control(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse('{"tool":"done","args":{"summary":"finished"}}')

        self.assertEqual(plan.calls, [])
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "done")
        self.assertEqual(plan.control.body, "finished")

    def test_parse_read_files_as_multiple_reads(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse('{"tool":"read_files","args":{"paths":["a.py","b.py"]}}')

        self.assertEqual([call.name for call in plan.calls], ["read", "read"])
        self.assertEqual([call.args["path"] for call in plan.calls], ["a.py", "b.py"])
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_parse_parallel_tool_calls(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"parallel","args":{"calls":['
            '{"tool":"grep","args":{"pattern":"login","path":"."}},'
            '{"tool":"list_dir","args":{"path":"src"}}'
            ']}}'
        )

        self.assertEqual([call.name for call in plan.calls], ["search", "ls"])
        self.assertEqual(plan.calls[0].args["query"], "login")
        self.assertEqual(plan.calls[1].args["path"], "src")

    def test_parse_parallel_tolerates_read_files_with_misnested_paths(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"parallel","args":{"calls":['
            '{"tool":"read_files","paths":["pricing.py","test_pricing.py"]}'
            ']}}'
        )

        self.assertEqual([call.name for call in plan.calls], ["read", "read"])
        self.assertEqual([call.args["path"] for call in plan.calls], ["pricing.py", "test_pricing.py"])
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_extracts_json_from_web_reply_noise(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '已深度思考（用时 1 秒）\n'
            '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        )

        self.assertEqual(len(plan.calls), 1)
        self.assertEqual(plan.calls[0].name, "edit")
        self.assertEqual(plan.calls[0].args["replacements"], [{"search": "old", "replace": "new"}])

    def test_parse_accidental_multiple_tool_objects_as_parallel_calls(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"edit","args":{"path":"a.py","content":"A = 1\\n"}}\n\n'
            '{"tool":"edit","args":{"path":"b.py","content":"B = 2\\n"}}'
        )

        self.assertEqual([call.name for call in plan.calls], ["edit", "edit"])
        self.assertEqual([call.args["path"] for call in plan.calls], ["a.py", "b.py"])
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_non_json_protocol_is_not_parsed(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse("tool: read_file path=app.py")

        self.assertEqual(plan.calls, [])
        self.assertIsNone(plan.control)

    def test_format_results_and_repair_prompt_remind_json_only(self) -> None:
        codec = JsonToolCodec()
        call = ToolCall("read", {"path": "app.py"})
        prompt = codec.format_results([ToolResult(call, "content")])

        self.assertIn("[tool_result tool=read_file path=app.py]\n---\ncontent\n---", prompt)
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn("not native website tools", prompt)
        self.assertIn("valid JSON tool call", codec.repair_prompt())

    def test_system_prompt_does_not_claim_model_is_codey(self) -> None:
        prompt = JsonToolCodec().system_prompt()

        self.assertIn("You are a careful local coding agent.", prompt)
        self.assertIn("The local runner executes tools", prompt)
        self.assertIn("not native website tools", prompt)
        self.assertIn("Do not say that a tool does not exist", prompt)
        self.assertIn("Do not output multiple JSON objects", prompt)
        self.assertIn("Do not wrap read_files inside parallel", prompt)
        self.assertIn("Do not use pipes, redirects", prompt)
        self.assertNotIn("You are Codey", prompt)


if __name__ == "__main__":
    unittest.main()
