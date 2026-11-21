"""Read native HTML disclosures without dispatching an event to the website."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession




@dataclass(frozen=True)
class DetailControl:
    index: int
    name: str
    owner: str

    def payload(self) -> JsonObject:
        return {"index": self.index, "name": self.name, "owner": self.owner}

    def choice(self, document: JsonObject) -> str:
        identity: list[JsonObject] = [document, self.payload()]
        raw = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        return "detail-" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def observe_details(browser: BrowserSession, document: JsonObject, task: str) -> tuple[DetailControl, ...]:
    row = browser.inspect_details(document, re.sub(r"https?://[^\s<>，。；]+", "", task))
    controls = row.get("controls") if row is not None else None
    if not isinstance(controls, list):
        return ()
    return tuple(DetailControl(require_json_integer(item["index"], "index"),
                               require_json_string(item["name"], "name"),
                               require_json_string(item["owner"], "owner"))
                 for raw in controls for item in (decode_json_object(raw),))


def read_detail(browser: BrowserSession, document: JsonObject, task: str, choice: str) -> dict[str, object] | None:
    selected = next((control for control in observe_details(browser, document, task)
                     if control.choice(document) == choice), None)
    if selected is None:
        return None
    value = browser.inspect_details(document, re.sub(r"https?://[^\s<>，。；]+", "", task), selected.payload())
    if value is None:
        return None
    return {"status": "ok", "url": document["url"], "title": value["title"], "text": value["text"],
            "truncated": False, "observation": "current_page", "record_context": selected.owner}
