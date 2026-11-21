"""Materialize immutable image refs only when constructing the actual API wire."""
from __future__ import annotations

import base64
import io
from collections.abc import Callable, Mapping

from PIL import Image

from codey.providers.text_result_views import TEXT_RESULTS_KEY, decode_text_result_spans
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import require_json_string
from codey.types.tool_image import TOOL_IMAGES_KEY as TOOL_IMAGES_KEY
from codey.types.tool_image import ToolImageRef


def message_images(message: Mapping[str, object]) -> tuple[ToolImageRef, ...]:
    images: list[ToolImageRef] = []
    if TOOL_IMAGES_KEY in message:
        raw = message[TOOL_IMAGES_KEY]
        if not isinstance(raw, list):
            raise ValueError("invalid tool images")
        images.extend(ToolImageRef.read(image) for image in raw)
    if TEXT_RESULTS_KEY in message:
        text = require_json_string(message.get("content"), "text result content")
        images.extend(image for span in decode_text_result_spans(text, message[TEXT_RESULTS_KEY]) for image in span.images)
    return tuple(dict.fromkeys(images))


def materialize_images(message: dict[str, object], *, protocol: str,
                       images: tuple[ToolImageRef, ...] | None = None) -> dict[str, object]:
    images = message_images(message) if images is None else images
    result = {key: value for key, value in message.items() if key != TOOL_IMAGES_KEY}
    if not images:
        return result
    text = require_json_string(result.get("content", ""), "tool image message")
    content: list[dict[str, object]] = [{"type": "input_text" if protocol == "openai-responses" else "text", "text": text}]
    for image in images:
        raw = ManagedOutputStore(image.state_home).read_binary_artifact(session_id=image.session_id, run_id=image.run_id,
            handle=image.handle, sha256=image.sha256, size=image.size)
        with Image.open(io.BytesIO(raw)) as pixels:
            if pixels.format != "PNG" or pixels.size != (image.width, image.height):
                raise ValueError("captured image dimensions differ from its receipt")
        uri = "data:" + image.media_type + ";base64," + base64.b64encode(raw).decode("ascii")
        if protocol == "openai-responses":
            content.append({"type": "input_image", "image_url": uri, "detail": "auto"})
        else:
            content.append({"type": "image_url", "image_url": {"url": uri, "detail": "auto"}})
    result["content"] = content
    return result


def materialize_messages(messages: list[dict[str, object]], *, protocol: str,
                         pending_calls: Callable[[list[dict[str, object]]], tuple[str, ...]]) -> list[dict[str, object]]:
    """Keep native pairs intact and put their pixels after the completed batch."""
    result: list[dict[str, object]] = []
    seen: set[ToolImageRef] = set()
    queued: list[ToolImageRef] = []
    prefix: list[dict[str, object]] = []
    for message in messages:
        prefix.append(message)
        images = tuple(image for image in message_images(message) if image not in seen)
        seen.update(images)
        if message.get("role") == "tool" or message.get("type") == "function_call_output":
            queued.extend(images)
            result.append({key: value for key, value in message.items() if key != TOOL_IMAGES_KEY})
            if queued and not pending_calls(prefix):
                result.append(materialize_images({"role": "user", "content": "Images from the preceding tool results."},
                    protocol=protocol, images=tuple(queued)))
                queued.clear()
        else:
            result.append(materialize_images(message, protocol=protocol, images=images))
    if queued:
        raise ValueError("captured images have an incomplete native result batch")
    return result
