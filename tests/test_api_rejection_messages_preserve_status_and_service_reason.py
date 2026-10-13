"""Known HTTP rejections expose bounded service facts rather than JSON syntax."""
import json
from email.message import Message
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError

import pytest

from codey.providers.api_transport import GenerationRejectedError, generate


def test_free_tier_rejection_preserves_status_type_and_service_reason():
    reason = "Error from provider (Console): OpenCode's free tier can only be used from within OpenCode"
    error = GenerationRejectedError(403, json.dumps({"type": "error", "error": {"type": "FreeTierError", "message": reason}}))
    assert error.status == 403 and error.error_type == "FreeTierError"
    assert str(error) == f"model HTTP 403 [FreeTierError]: {reason}"


@pytest.mark.parametrize("detail", ["Forbidden", "{broken", '{"error":null}', '{"error":{"message":{"nested":"value"}}}'])
def test_unstructured_rejection_is_not_coerced_into_a_service_message(detail):
    error = GenerationRejectedError(403, detail)
    assert str(error) == f"model HTTP 403: {detail}"


@pytest.mark.parametrize("options,expected", [({}, ""), ({"request_id_headers": ("X-Connector-Receipt",)}, "req-connector")])
def test_vendor_request_id_is_owned_by_connection_options(options, expected):
    headers = Message()
    headers['X-Connector-Receipt'] = 'req-connector'
    body = b'{"error":{"message":"unavailable"}}'
    headers['Content-Length'] = str(len(body))
    rejection = HTTPError('http://localhost/v1/chat/completions', 429, 'rejected', headers, BytesIO(body))
    with mock.patch('codey.providers.api_transport.open_request', side_effect=rejection), pytest.raises(GenerationRejectedError) as failure:
        generate('http://localhost/v1/chat/completions', {}, {}, timeout=5, observe=lambda **_: None, transport_options=options)
    assert failure.value.upstream_request_id == expected
