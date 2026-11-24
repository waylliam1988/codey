import pytest

from codey.automation.browser_query import changed_query_region
from codey.types.json_value import JsonObject, JsonValue


@pytest.mark.parametrize("key", [None, "", 7])
def test_query_change_requires_the_recorded_region_identity(key: JsonValue) -> None:
    previous: JsonObject = {"text": "old", "visible": True, "busy": False}
    current: JsonObject = {"text": "new", "visible": True, "busy": False}
    if key is not None:
        previous["key"] = current["key"] = key

    assert changed_query_region({"regions": [previous]}, {"regions": [current]}) == ""


def test_query_change_accepts_updated_text_for_the_same_region() -> None:
    previous: JsonObject = {"key": "7", "text": "old", "visible": True, "busy": False}
    current: JsonObject = {"key": "7", "text": "new", "visible": True, "busy": False}

    assert changed_query_region({"regions": [previous]}, {"regions": [current]}) == "new"
