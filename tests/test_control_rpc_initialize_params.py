from io import BytesIO
from pathlib import Path

from codey.control.client import ControlClient
from codey.control.rpc import RpcAdapter
from codey.types.json_value import parse_json_object


def test_initialize_requires_named_parameters(tmp_path: Path) -> None:
    source = BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":[]}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"initialize","params":{}}\n'
    )
    output = BytesIO()

    assert RpcAdapter(ControlClient(tmp_path), output).serve(source) == 0

    invalid, valid = (parse_json_object(line.decode("utf-8")) for line in output.getvalue().splitlines())
    assert invalid["id"] == 1
    error = invalid["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32602
    assert valid["id"] == 2
    assert isinstance(valid["result"], dict)
