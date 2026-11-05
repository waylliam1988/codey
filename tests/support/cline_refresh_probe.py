"""Test-only process using an injected temporary fixture token store."""
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from codey.providers.cline import auth as auth_module
from codey.providers.cline.auth import Auth
from codey.providers.cline.credentials import CredentialRecord
from codey.providers.cline.http import HttpClient
from codey.storage.local_store import write_json_atomic


class FixtureStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        return CredentialRecord(**json.loads(self.path.read_text(encoding="utf8")))

    def save(self, record):
        write_json_atomic(self.path, asdict(record))

    def delete(self):
        self.path.unlink(missing_ok=True)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    auth_module.API_ORIGIN = sys.argv[2]
    auth = Auth(FixtureStore(root / "fixture-tokens.json"), HttpClient(), root / "credentials")
    record = auth.credentials(time.monotonic() + 10, lambda: False, "account-A")
    print(json.dumps({"version": record.token_version, "epoch": record.account_epoch}))
