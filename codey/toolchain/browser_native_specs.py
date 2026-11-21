"""Additional native browser operations in the existing ToolSpec registry."""
from __future__ import annotations

from codey.toolchain.tool_spec import ToolSpec


def browser_native_specs() -> dict[str, ToolSpec]:
    string: dict[str, object] = {"type": "string", "minLength": 1}
    ref = ("ref", string)
    definitions: tuple[tuple[str, str, str, str, tuple[tuple[str, dict[str, object]], ...], tuple[str, ...]], ...] = (
        ("frame_list", "observe", "read", "List actual frame IDs in the bound page.", (), ()),
        ("frame_select", "interact", "navigation", "Select an observed frame; subsequent operations use its document.", (("frame_id", string),), ("frame_id",)),
        ("locate", "observe", "read", "Find bounded visible candidates by observed role, name, text, placeholder or selector; ambiguity remains explicit.",
         tuple((key, string) for key in ("role", "name", "text", "placeholder", "selector")), ()),
        ("inspect", "observe", "read", "Inspect an observed node's attributes, ancestors, children and bounding box.", (ref,), ("ref",)),
        ("press", "interact", "page_mutation", "Send a native key or chord to an observed ref, for example Enter or Control+A.", (ref, ("key", string)), ("ref", "key")),
        ("hover", "interact", "page_mutation", "Move the pointer to an observed ref using native input.", (ref,), ("ref",)),
        ("select", "interact", "page_mutation", "Select native option values in an observed select control.", (ref, ("values", {"type": "array", "items": string, "minItems": 1})), ("ref", "values")),
        ("check", "interact", "page_mutation", "Set and read back a native checkbox or radio control.", (ref, ("checked", {"type": "boolean"})), ("ref", "checked")),
        ("dialog", "observe", "read", "Observe the current JavaScript dialog without accepting it.", (), ()),
        ("dialog_respond", "interact", "page_mutation", "Accept or dismiss the observed JavaScript dialog; optional text answers a prompt.", (("accept", {"type": "boolean"}), ("text", {"type": "string"})), ("accept",)),
        ("screenshot", "observe", "read", "Capture real PNG pixels for model vision, with observed ref mappings. Use clip for a smaller document region.",
         (("annotate", {"type": "boolean"}), ("full_page", {"type": "boolean"}), ("clip", {"type": "object", "additionalProperties": False,
          "properties": {key: {"type": "number", "minimum": 0 if key in {"x", "y"} else 1} for key in ("x", "y", "width", "height")},
          "required": ["x", "y", "width", "height"]})), ()),
        ("visual_click", "interact", "page_mutation", "Click CSS pixels in an observed screenshot after exact document, viewport and pixel validation. Prefer its ref mapping when available.",
         (("observation_ref", string), ("x", {"type": "number", "minimum": 0}), ("y", {"type": "number", "minimum": 0})), ("observation_ref", "x", "y")),
    )
    return {"browser_" + name: ToolSpec(name="browser_" + name, grant="browser." + grant, executor="browser",
        namespace="browser." + grant, effect=effect, description=description, parameters=parameters, required=required,
        preconditions=("browser_bound",)) for name, grant, effect, description, parameters, required in definitions}
