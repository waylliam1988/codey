from __future__ import annotations

import unittest
from pathlib import Path

from codey.capabilities import KNOWN_DURABLE_STATES, KNOWN_UI_SURFACES, builtin_capability_registry


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "docs" / "codey_event_matrix.md"
DISALLOWED_PRIVACY_TERMS = {
    "raw_prompt",
    "raw_stdout",
    "webpage_body",
    "source_body",
    "raw_provider_error",
}
REQUIRED_RUN_EVENTS = {
    "run_event.turn",
    "run_event.tool_start",
    "run_event.tool",
    "run_event.info",
    "run_event.status",
}
REQUIRED_RUN_TRACE_EVENTS = {
    "run_trace.prompt_sections",
    "run_trace.policy_decisions",
    "run_trace.fallbacks",
    "run_trace.provider_failures",
}
REQUIRED_TOOL_OUTCOME_EVENTS = {
    "tool_outcome.model_text",
    "tool_outcome.presentation",
    "tool_outcome.audit",
    "tool_outcome.canonical",
}
REQUIRED_MODEL_VISIBLE_EVENT_PROJECTIONS = {
    "review.recent_log",
}
BOOLEAN_COLUMNS = (
    "model_visible",
    "policy_required",
    "trace_required",
)


def _event_matrix_rows() -> list[dict[str, str]]:
    lines = MATRIX_PATH.read_text(encoding="utf-8").splitlines()
    table_lines = [line for line in lines if line.startswith("| ")]
    if len(table_lines) < 3:
        raise AssertionError("event matrix table is missing")
    headers = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in table_lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != len(headers):
            raise AssertionError(f"malformed event matrix row: {line}")
        rows.append(dict(zip(headers, cells, strict=True)))
    return rows


class EventMatrixTests(unittest.TestCase):
    def test_event_ids_are_unique_and_core_fields_are_present(self) -> None:
        rows = _event_matrix_rows()
        event_ids = [row["event_id"] for row in rows]

        self.assertEqual(len(event_ids), len(set(event_ids)))
        for row in rows:
            with self.subTest(event_id=row["event_id"]):
                self.assertTrue(row["event_id"])
                self.assertTrue(row["producer"])
                self.assertTrue(row["consumers"])
                self.assertTrue(row["privacy_boundary"])

    def test_capabilities_and_durable_state_use_known_names(self) -> None:
        rows = _event_matrix_rows()
        known_capabilities = set(builtin_capability_registry().ids()) | {"none"}
        known_states = set(KNOWN_DURABLE_STATES) | {"none"}

        for row in rows:
            with self.subTest(event_id=row["event_id"]):
                self.assertIn(row["capability"], known_capabilities)
                for state in _csv_values(row["durable_state"]):
                    self.assertIn(state, known_states)

    def test_visibility_policy_and_trace_requirements_are_declared(self) -> None:
        capabilities = {
            spec.id: spec for spec in builtin_capability_registry().all()
        }
        for row in _event_matrix_rows():
            combined = " ".join((
                row["consumers"],
                row["capability"],
                row["privacy_boundary"],
            ))
            capability = capabilities.get(row["capability"])
            with self.subTest(event_id=row["event_id"]):
                if row["model_visible"] == "true":
                    self.assertIn("prompt_envelope", row["consumers"])
                    self.assertIn("run_trace", row["consumers"])
                if row["policy_required"] == "true":
                    self.assertTrue(
                        "policy_guard" in combined
                        or "action_policy" in combined
                        or (capability is not None and capability.requires_policy),
                        f"{row['event_id']} must declare action policy or policy guard",
                    )
                if row["trace_required"] == "true":
                    self.assertIn("run_trace", row["consumers"])

    def test_ui_visible_rows_declare_known_surface_or_sse(self) -> None:
        surfaces = set(KNOWN_UI_SURFACES) - {"none"}

        for row in _event_matrix_rows():
            with self.subTest(event_id=row["event_id"]):
                if row["ui_visible"] == "false":
                    continue
                ui_values = _csv_values(row["ui_visible"])
                self.assertTrue(ui_values)
                for value in ui_values:
                    self.assertTrue(value.startswith("sse:") or value in surfaces)

    def test_boolean_columns_use_lowercase_true_or_false(self) -> None:
        invalid = [
            (row["event_id"], column, row[column])
            for row in _event_matrix_rows()
            for column in BOOLEAN_COLUMNS
            if row[column] not in {"true", "false"}
        ]

        self.assertEqual([], invalid)

    def test_privacy_boundaries_do_not_allow_raw_persisted_payloads(self) -> None:
        for row in _event_matrix_rows():
            lower = row["privacy_boundary"].lower()
            with self.subTest(event_id=row["event_id"]):
                for term in DISALLOWED_PRIVACY_TERMS:
                    self.assertNotIn(term, lower)

    def test_matrix_covers_core_event_families(self) -> None:
        event_ids = {row["event_id"] for row in _event_matrix_rows()}

        self.assertTrue(REQUIRED_RUN_EVENTS.issubset(event_ids))
        self.assertTrue(REQUIRED_RUN_TRACE_EVENTS.issubset(event_ids))
        self.assertTrue(REQUIRED_TOOL_OUTCOME_EVENTS.issubset(event_ids))
        self.assertTrue(REQUIRED_MODEL_VISIBLE_EVENT_PROJECTIONS.issubset(event_ids))


def _csv_values(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


if __name__ == "__main__":
    unittest.main()
