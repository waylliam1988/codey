"""Live reports bind the temporary connection adapter as well as shared codecs."""
import hashlib

from tools import local_model_gate_attempts as attempts
from tools import local_model_release_gate as release


def test_zen_live_metadata_fingerprints_the_actual_adapter_and_protocol_sources():
    target = attempts.GateTarget("https://opencode.ai/zen/v1", "fixture", 32768, 8192, 12000,
                                 provider_id="zen", api_selection={})
    metadata = release._metadata(target)
    for path in ("codey/providers/zen/connection.py", "codey/providers/zen/declarations.py",
                 "codey/providers/zen/identity.py", "codey/providers/zen/catalog.py", "codey/providers/zen/access.py",
                 "codey/providers/api_responses.py", "codey/providers/api_chat.py", "codey/providers/api_transport.py"):
        assert metadata["production_hashes"][path] == hashlib.sha256((attempts.REPO_ROOT / path).read_bytes()).hexdigest()
