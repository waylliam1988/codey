"""Tool schemas can contain a property literally named type."""
from codey.task.attachments import image_estimate_view


def test_nested_tool_schema_type_properties_are_not_image_inputs() -> None:
    schema = {"type": "object", "properties": {"type": {"type": "string"},
        "options": {"type": "array", "items": {"type": "object", "properties": {"type": {"type": "string"}}}}}}
    payload = {"response_format": {"json_schema": {"schema": schema}},
        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}
    view, allowance = image_estimate_view(payload)
    assert allowance == 16_384
    assert isinstance(view, dict)
    assert view["response_format"]["json_schema"]["schema"] == schema
    assert "base64" not in str(view["messages"])
