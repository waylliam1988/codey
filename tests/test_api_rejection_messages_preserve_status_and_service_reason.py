"""Known HTTP rejections expose bounded service facts rather than JSON syntax."""
import json

import pytest

from codey.providers.api_transport import GenerationRejectedError


def test_free_tier_rejection_preserves_status_type_and_service_reason():
    reason = "Error from provider (Console): OpenCode's free tier can only be used from within OpenCode"
    error = GenerationRejectedError(403, json.dumps({"type": "error", "error": {"type": "FreeTierError", "message": reason}}))
    assert error.status == 403 and error.error_type == "FreeTierError"
    assert str(error) == f"model HTTP 403 [FreeTierError]: {reason}"


@pytest.mark.parametrize("detail", ["Forbidden", "{broken", '{"error":null}', '{"error":{"message":{"nested":"value"}}}'])
def test_unstructured_rejection_is_not_coerced_into_a_service_message(detail):
    error = GenerationRejectedError(403, detail)
    assert str(error) == f"model HTTP 403: {detail}"
