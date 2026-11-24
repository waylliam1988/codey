"""Display receipts cannot turn a malformed verification flag into success."""

import pytest

from codey.app.event_payloads import bounded_receipt
from codey.utils.object_fields import require_object_fields


@pytest.mark.parametrize("value", ["false", "true", 1, [], {}])
def test_receipt_verification_never_promotes_non_boolean_to_success(value: object) -> None:
    projected = bounded_receipt({"verification": {"checks_passed": value}})
    assert require_object_fields(projected.get("verification", {})).get("checks_passed") is not True


@pytest.mark.parametrize("value", [True, False])
def test_receipt_verification_preserves_real_boolean(value: bool) -> None:
    projected = bounded_receipt({"verification": {"checks_passed": value}})
    assert require_object_fields(projected["verification"])["checks_passed"] is value
