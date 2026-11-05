"""Clear Cline credentials, caches and preferences before removing its package.

Stop the running desktop backend first. The app store lease rejects concurrent
offline maintenance; history is retained. No credentials are printed.
"""
from __future__ import annotations

import json
from contextlib import closing

from codey.app.connection_actions import action_response
from codey.app.context import AppContext
from codey.storage.local_store import DEFAULT_STATE_HOME


def main() -> int:
    with closing(AppContext(DEFAULT_STATE_HOME)) as ctx:
        status, result = action_response(ctx, {"source": "cline", "action": "clear_data"})
    print(json.dumps({key: result[key] for key in ("ok", "code", "error", "pending_cleanup") if key in result}))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
