"""Annotate captured pixels without modifying the website's DOM."""
from __future__ import annotations

import hashlib
import io
import math

from PIL import Image, ImageDraw, ImageFont

from codey.types.json_value import JsonObject, JsonValue, decode_json_object

MAX_IMAGE_PIXELS = 4_000_000


def pixel_digest(png: bytes) -> str:
    with Image.open(io.BytesIO(png)) as image:
        if image.format != "PNG" or image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("invalid captured PNG")
        pixels = image.convert("RGBA")
        return hashlib.sha256(str(pixels.size).encode("ascii") + pixels.tobytes()).hexdigest()


def image_number(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("invalid image coordinate")
    return float(value)


def annotate_capture(png: bytes, observation: JsonObject, clip: JsonObject, *, annotate: bool,
                     scroll_x: float = 0, scroll_y: float = 0) -> tuple[bytes, list[JsonValue], int, int]:
    with Image.open(io.BytesIO(png)) as source:
        if source.format != "PNG" or source.width * source.height > MAX_IMAGE_PIXELS:
            raise ValueError("screenshot exceeds pixel budget; choose a smaller observed region")
        pixels = source.convert("RGB")
    width, height = pixels.size
    scale_x, scale_y = width / image_number(clip["width"]), height / image_number(clip["height"])
    draw = ImageDraw.Draw(pixels)
    font = ImageFont.load_default(size=12)
    mappings: list[JsonValue] = []
    elements = observation.get("elements")
    for value in elements if isinstance(elements, list) else []:
        row = decode_json_object(value)
        if not isinstance(row.get("box"), dict):
            continue
        box = decode_json_object(row["box"])
        left = (image_number(box["x"]) + scroll_x - image_number(clip["x"])) * scale_x
        top = (image_number(box["y"]) + scroll_y - image_number(clip["y"])) * scale_y
        right, bottom = left + image_number(box["width"]) * scale_x, top + image_number(box["height"]) * scale_y
        if right <= 0 or bottom <= 0 or left >= width or top >= height:
            continue
        number = len(mappings) + 1
        mapped: JsonObject = {"number": number, "ref": row["ref"], "role": row["role"], "name": row["name"],
            "box": {"x": left, "y": top, "width": right - left, "height": bottom - top}}
        mappings.append(mapped)
        if annotate:
            x, y = max(0, min(width - 24, left)), max(0, min(height - 17, top))
            draw.rectangle((x, y, x + 23, y + 16), fill="#ffe100", outline="#151515")
            draw.text((x + 3, y + 1), str(number), font=font, fill="#151515")
    if not annotate:
        return png, mappings, width, height
    output = io.BytesIO()
    pixels.save(output, format="PNG")
    return output.getvalue(), mappings, width, height
