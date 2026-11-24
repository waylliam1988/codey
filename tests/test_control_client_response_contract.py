import io
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.control.client import ControlClient
from codey.control.protocol import PROTOCOL_VERSION, ControlError
from codey.storage.local_store import write_json_atomic


@pytest.mark.parametrize("response", [b'{"ok":true}', b'{"ok":true,"result":null}'])
def test_missing_service_result_is_an_unknown_outcome(tmp_path: Path, response: bytes) -> None:
    write_json_atomic(tmp_path / "service.json", {
        "url": "http://127.0.0.1:1111", "instance_id": "fixture", "protocol_version": PROTOCOL_VERSION,
    })
    write_json_atomic(tmp_path / "control-credentials.json", {"agent": "fixture-credential"})

    class Opener:
        def open(self, _request: object, *, timeout: float) -> nullcontext[io.BytesIO]:
            assert timeout > 0
            return nullcontext(io.BytesIO(response))

    with (
        patch("codey.control.client.urllib.request.build_opener", return_value=Opener()),
        pytest.raises(ControlError) as caught,
    ):
        ControlClient(tmp_path).call("get_health")

    assert caught.value.code == "OUTCOME_UNKNOWN"
