"""Regression lock: pure-extraction equivalence for json_safe_projection helpers.

Covers every projection branch (scalar dispatch, string/key truncation,
non-string keys, reserved/duplicate keys, list/tuple, mappings, max depth,
item budget, warning cap, unsupported types, non-mapping top level) with
zero behavior change. Bug hunt probed all branches (bool/int order, NaN/Inf,
truncation boundaries, budget exactness, depth consistency, JSON round-trip):
no deterministic bug found, so these tests lock in current semantics.
Uncertain observations (recorded, not touched): count_item() generic warning
branch is unreachable via current callers (both loops pre-check the budget);
bool keys render as "True"/"False" (vs json.dumps "true"/"false"); depth gate
allows value-depth <= 6 (7 mapping levels).
"""

from __future__ import annotations

import json
import unittest

from codey.runtime.core.models import (
    PROJECTION_MAX_DEPTH,
    PROJECTION_MAX_ITEMS,
    PROJECTION_MAX_KEY_CHARS,
    PROJECTION_MAX_STRING_CHARS,
    PROJECTION_MAX_WARNINGS,
    PROJECTION_WARNING_KEY,
    json_safe_projection,
)


def _project(value: object) -> dict[str, object]:
    return json_safe_projection(value, label="t")


def _nest_mapping(levels: int) -> object:
    node: object = {"leaf": "v"}
    for _ in range(levels):
        node = {"n": node}
    return node


class TopLevelTests(unittest.TestCase):
    def test_none_gives_empty_without_warnings(self) -> None:
        self.assertEqual(_project(None), {})

    def test_empty_mapping_gives_empty_without_warnings(self) -> None:
        self.assertEqual(_project({}), {})

    def test_non_mapping_top_warns_and_drops(self) -> None:
        for value, typename in [([1, 2], "list"), ("hi", "str"), (7, "int"), (1.5, "float")]:
            with self.subTest(value=value):
                result = _project(value)
                self.assertEqual(result[PROJECTION_WARNING_KEY], [f"t projection replaced non-mapping {typename}"])
                self.assertEqual(set(result), {PROJECTION_WARNING_KEY})

    def test_output_is_strict_json_serializable(self) -> None:
        result = _project({"a": [1, "x", True, None, {"b": 2.5}]})
        json.dumps(result)


class ScalarDispatchTests(unittest.TestCase):
    def test_bool_identity_preserved_not_coerced_to_int(self) -> None:
        result = _project({"t1": True, "f": False})
        self.assertIs(result["t1"], True)
        self.assertIs(result["f"], False)
        self.assertNotIn(PROJECTION_WARNING_KEY, result)

    def test_int_passthrough(self) -> None:
        result = _project({"a": 1, "b": 0, "c": -2**63})
        self.assertEqual((result["a"], result["b"], result["c"]), (1, 0, -2**63))

    def test_finite_float_passthrough(self) -> None:
        result = _project({"a": 1.5})
        self.assertEqual(result["a"], 1.5)

    def test_non_finite_float_becomes_string_with_warning(self) -> None:
        result = _project({"a": float("nan"), "b": float("inf"), "c": float("-inf")})
        self.assertEqual((result["a"], result["b"], result["c"]), ("nan", "inf", "-inf"))
        warnings = result[PROJECTION_WARNING_KEY]
        assert isinstance(warnings, list)
        self.assertEqual(
            warnings,
            [
                "t.a converted non-finite float",
                "t.b converted non-finite float",
                "t.c converted non-finite float",
            ],
        )
        json.dumps(result)

    def test_tuple_becomes_list(self) -> None:
        result = _project({"a": (1, "x")})
        self.assertEqual(result["a"], [1, "x"])

    def test_unsupported_type_placeholder_with_warning(self) -> None:
        result = _project({"a": b"x", "b": {1, 2}})
        self.assertEqual(result["a"], "<non-json bytes>")
        self.assertEqual(result["b"], "<non-json set>")
        warnings = result[PROJECTION_WARNING_KEY]
        assert isinstance(warnings, list)
        self.assertIn("t.a converted non-json bytes", warnings)
        self.assertIn("t.b converted non-json set", warnings)

    def test_nested_unsupported_path(self) -> None:
        class _Foo:
            pass

        result = _project({"b": {"c": _Foo()}})
        inner = result["b"]
        assert isinstance(inner, dict)
        self.assertEqual(inner["c"], "<non-json _Foo>")
        self.assertIn("t.b.c converted non-json _Foo", result[PROJECTION_WARNING_KEY])


class TruncationTests(unittest.TestCase):
    def test_string_boundary_exact(self) -> None:
        self.assertEqual(len(_project({"a": "x" * (PROJECTION_MAX_STRING_CHARS - 1)})["a"]), PROJECTION_MAX_STRING_CHARS - 1)
        at_max = _project({"a": "x" * PROJECTION_MAX_STRING_CHARS})
        self.assertEqual(len(at_max["a"]), PROJECTION_MAX_STRING_CHARS)
        self.assertNotIn(PROJECTION_WARNING_KEY, at_max)
        over = _project({"a": "x" * (PROJECTION_MAX_STRING_CHARS + 1)})
        self.assertEqual(len(over["a"]), PROJECTION_MAX_STRING_CHARS)
        self.assertEqual(over[PROJECTION_WARNING_KEY], ["t.a string clipped"])

    def test_key_boundary_exact(self) -> None:
        ok = _project({"k" * PROJECTION_MAX_KEY_CHARS: 1})
        keys = [k for k in ok if k != PROJECTION_WARNING_KEY]
        self.assertEqual(len(keys[0]), PROJECTION_MAX_KEY_CHARS)
        self.assertNotIn(PROJECTION_WARNING_KEY, ok)
        over = _project({"k" * (PROJECTION_MAX_KEY_CHARS + 1): 1})
        keys = [k for k in over if k != PROJECTION_WARNING_KEY]
        self.assertEqual(len(keys[0]), PROJECTION_MAX_KEY_CHARS)
        self.assertEqual(over[PROJECTION_WARNING_KEY], ["t.key key clipped"])

    def test_empty_key_renamed(self) -> None:
        result = _project({"": 1})
        self.assertEqual(result["_"], 1)
        self.assertEqual(result[PROJECTION_WARNING_KEY], ["t.key empty key renamed"])


class KeySanitizationTests(unittest.TestCase):
    def test_scalar_keys_stringified_with_warning(self) -> None:
        result = _project({None: 1, True: 2, 7: 3, 1.5: 4})
        self.assertEqual(result["None"], 1)
        self.assertEqual(result["True"], 2)
        self.assertEqual(result["7"], 3)
        self.assertEqual(result["1.5"], 4)
        warnings = result[PROJECTION_WARNING_KEY]
        assert isinstance(warnings, list)
        self.assertEqual(len(warnings), 4)
        self.assertTrue(all(w == "t.key key converted to string" for w in warnings))

    def test_exotic_key_placeholder(self) -> None:
        result = _project({(1, 2): 4})
        self.assertEqual(result["<non-json-key tuple>"], 4)
        self.assertIn("t.key key converted to string", result[PROJECTION_WARNING_KEY])

    def test_reserved_warning_key_renamed(self) -> None:
        result = _project({PROJECTION_WARNING_KEY: 1, "x": 2})
        self.assertEqual(result["_input_projection_warnings"], 1)
        self.assertEqual(result["x"], 2)
        self.assertIn(f"t.{PROJECTION_WARNING_KEY} reserved key renamed", result[PROJECTION_WARNING_KEY])

    def test_duplicate_key_after_stringify_omitted(self) -> None:
        result = _project({1: "a", "1": "b"})
        self.assertEqual(result["1"], "a")
        warnings = result[PROJECTION_WARNING_KEY]
        assert isinstance(warnings, list)
        self.assertIn("t.key key converted to string", warnings)
        self.assertIn("t.1 duplicate key omitted", warnings)


class BudgetDepthWarningTests(unittest.TestCase):
    def test_item_budget_exact(self) -> None:
        full = _project({str(i): i for i in range(PROJECTION_MAX_ITEMS)})
        self.assertEqual(len([k for k in full if k != PROJECTION_WARNING_KEY]), PROJECTION_MAX_ITEMS)
        self.assertNotIn(PROJECTION_WARNING_KEY, full)
        over = _project({str(i): i for i in range(PROJECTION_MAX_ITEMS + 1)})
        self.assertEqual(len([k for k in over if k != PROJECTION_WARNING_KEY]), PROJECTION_MAX_ITEMS)
        self.assertEqual(over[PROJECTION_WARNING_KEY], ["t object omitted extra items"])

    def test_list_budget_counts_container_plus_items(self) -> None:
        # {'a': [...]} costs 1 for the list itself, so only MAX-1 items fit.
        fits = _project({"a": list(range(PROJECTION_MAX_ITEMS - 1))})
        self.assertEqual(len(fits["a"]), PROJECTION_MAX_ITEMS - 1)
        self.assertNotIn(PROJECTION_WARNING_KEY, fits)
        over = _project({"a": list(range(PROJECTION_MAX_ITEMS))})
        self.assertEqual(len(over["a"]), PROJECTION_MAX_ITEMS - 1)
        self.assertEqual(over[PROJECTION_WARNING_KEY], ["t.a list omitted extra items"])

    def test_depth_gate_allows_six_nesting_levels(self) -> None:
        self.assertNotIn(PROJECTION_WARNING_KEY, _project(_nest_mapping(PROJECTION_MAX_DEPTH)))
        deep = _project(_nest_mapping(PROJECTION_MAX_DEPTH + 1))
        dumped = json.dumps(deep)
        self.assertIn("<max-depth str>", dumped)
        warnings = deep[PROJECTION_WARNING_KEY]
        assert isinstance(warnings, list)
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].endswith(" exceeded max depth"))

    def test_list_depth_gate(self) -> None:
        value: object = "x"
        for _ in range(PROJECTION_MAX_DEPTH):
            value = [value]
        self.assertNotIn(PROJECTION_WARNING_KEY, _project({"a": value}))
        deeper: object = "x"
        for _ in range(PROJECTION_MAX_DEPTH + 1):
            deeper = [deeper]
        result = _project({"a": deeper})
        self.assertIn("max-depth", json.dumps(result))

    def test_warning_cap(self) -> None:
        result = _project({str(i): "x" * (PROJECTION_MAX_STRING_CHARS + 5) for i in range(PROJECTION_MAX_WARNINGS + 5)})
        warnings = result[PROJECTION_WARNING_KEY]
        assert isinstance(warnings, list)
        self.assertEqual(len(warnings), PROJECTION_MAX_WARNINGS)

    def test_label_used_in_messages(self) -> None:
        result = json_safe_projection({"a": "x" * (PROJECTION_MAX_STRING_CHARS + 1)}, label="presentation")
        self.assertEqual(result[PROJECTION_WARNING_KEY], ["presentation.a string clipped"])
        top = json_safe_projection([1], label="audit")
        self.assertEqual(top[PROJECTION_WARNING_KEY], ["audit projection replaced non-mapping list"])


if __name__ == "__main__":
    unittest.main()
