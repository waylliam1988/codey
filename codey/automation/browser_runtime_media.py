"""BrowserMedia: browser actions using explicit runtime state and callbacks."""

from __future__ import annotations

import base64
import hashlib
import json
from uuid import uuid4

from playwright.async_api import Page

from codey.automation.browser_contracts import BrowserAction, BrowserToolOutput
from codey.automation.browser_document import same_document
from codey.automation.browser_frames import FrameContext, backend_node
from codey.automation.browser_images import MAX_IMAGE_PIXELS, annotate_capture, image_number, pixel_digest
from codey.automation.browser_refs import ElementRecord
from codey.automation.browser_runtime_state import (
    FrameAccess,
    Invocation,
    MarkProgress,
    ObservationState,
    PageState,
    ResolveElement,
    _evaluate,
    _send,
)
from codey.automation.browser_snapshot import (
    collect_snapshot,
    element_metadata,
    file_control_details,
    semantic_fingerprint,
)
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string


class BrowserMedia:

    def __init__(self, observation: ObservationState, *, frame: FrameAccess,
                 resolve: ResolveElement, mark: MarkProgress) -> None:
        self._observation = observation
        self._frame = frame
        self._resolve = resolve
        self._mark = mark
        self._visual: dict[str, JsonObject] = {}
        self._file_controls: dict[str, JsonObject] = {}
        self._file_controls_truncated = False
        self._file_document: JsonObject | None = None

    def reset_refs(self) -> None:
        self._visual.clear()
        self._file_controls.clear()
        self._file_document = None

    async def _file_snapshot(self, context: FrameContext) -> BrowserToolOutput:
        self._file_controls.clear()
        self._file_document = context.document
        handles = await context.frame.query_selector_all("input[type=file]")
        rows: list[JsonValue] = []
        for handle in handles:
            if len(rows) >= 80:
                break
            if not await handle.is_visible():
                continue
            metadata, owner = await element_metadata(handle)
            node = await backend_node(context, handle)
            ref = self._observation.registry.observe_node(ElementRecord(handle, context.document, node,
                "file_input", " ".join(str(metadata.get("name") or "").split())[:240],
                None, owner, None, self._observation.revision, semantic_fingerprint(metadata)))
            self._file_controls[ref] = {"id": ref, **await file_control_details(handle)}
            rows.append(self._file_controls[ref])
        truncated = len(handles) > 80
        self._file_controls_truncated = truncated
        value: JsonObject = {"page_id": context.document["target_id"], "page_url": context.frame.url,
            "generation": self._observation.revision, "scope": "file_inputs", "complete": not truncated,
            "text_truncated": False, "elements_truncated": truncated, "elements": [], "contexts": [],
            "file_controls": rows, "text": json.dumps(rows, ensure_ascii=False)}
        return BrowserToolOutput(value["text"] if isinstance(value["text"], str) else "", True, value)

    @staticmethod
    async def _viewport(page: Page) -> JsonObject:
        raw: object = await _evaluate(page, """() => ({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,
            scale:visualViewport?.scale||1,offset_x:visualViewport?.offsetLeft||0,offset_y:visualViewport?.offsetTop||0})""")
        return decode_json_object(raw)

    @staticmethod
    async def _capture_png(page: Page, clip: JsonObject) -> bytes:
        cdp = await page.context.new_cdp_session(page)
        try:
            capture_args: JsonObject = {"format": "png", "fromSurface": True,
                "captureBeyondViewport": True, "clip": {**clip, "scale": 1}}
            raw: object = await _send(cdp, "Page.captureScreenshot", capture_args)
            data = decode_json_object(raw)
            return base64.b64decode(require_json_string(data["data"], "captured pixels"), validate=True)
        finally:
            await cdp.detach()

    async def _screenshot(self, invocation: Invocation, state: PageState, context: FrameContext) -> tuple[JsonValue, bytes]:
        args = invocation.command.arguments
        await _evaluate(state.page, "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(null))))")
        viewport = await self._viewport(state.page)
        full_page = args.get("full_page") is True
        clip: JsonObject = {"x": viewport["x"], "y": viewport["y"], "width": viewport["width"], "height": viewport["height"]}
        if context.frame is not state.page.main_frame:
            owner = await context.frame.frame_element()
            box = await owner.bounding_box()
            await owner.dispose()
            if box is None:
                raise ValueError("frame_not_visible")
            clip = decode_json_object(box)
            clip["x"] = image_number(clip["x"]) + image_number(viewport["x"])
            clip["y"] = image_number(clip["y"]) + image_number(viewport["y"])
            full_page = False
        elif full_page:
            raw_size: object = await _evaluate(context.frame, "() => ({width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight})")
            size = decode_json_object(raw_size)
            clip.update(x=0, y=0, width=size["width"], height=size["height"])
        if "clip" in args:
            clip = decode_json_object(args["clip"])
            if set(clip) != {"x", "y", "width", "height"}:
                raise ValueError("invalid screenshot clip")
            full_page = False
        width, height = image_number(clip["width"]), image_number(clip["height"])
        if min(width, height) <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("screenshot exceeds pixel budget; choose a smaller region")
        self._observation.revision += 1
        observed = await collect_snapshot(context, self._observation.registry, revision=self._observation.revision)
        png = await self._capture_png(state.page, clip)
        if await self._viewport(state.page) != viewport:
            raise ValueError("screenshot_viewport_changed")
        async with self._frame(state) as after:
            if not same_document(context.document, after.document):
                raise ValueError("screenshot_document_changed")
        rendered, mapping, pixel_width, pixel_height = annotate_capture(png, observed, clip,
            annotate=args.get("annotate", True) is True, scroll_x=image_number(viewport["x"]), scroll_y=image_number(viewport["y"]))
        from codey.automation.browser_ipc import MAX_BLOB_BYTES

        if len(rendered) > MAX_BLOB_BYTES:
            raise ValueError("screenshot_exceeds_transfer_budget")
        identity = "visual-" + uuid4().hex
        visual: JsonObject = {"observation_ref": identity, "document": context.document, "viewport": viewport,
            "clip": clip, "full_page": full_page, "raw_sha256": pixel_digest(png),
            "sha256": hashlib.sha256(rendered).hexdigest(), "width": pixel_width, "height": pixel_height,
            "media_type": "image/png", "scale_x": pixel_width / width, "scale_y": pixel_height / height, "mapping": mapping}
        self._visual[identity] = visual
        while len(self._visual) > 4:
            del self._visual[next(iter(self._visual))]
        self._observation.observations[self._observation.revision] = observed
        while len(self._observation.observations) > 4:
            del self._observation.observations[next(iter(self._observation.observations))]
        value = {**observed, "image": visual}
        return BrowserToolOutput("Captured the current browser image with observed control mappings.", True, value).payload(), rendered

    async def _visual_click(self, invocation: Invocation, state: PageState, context: FrameContext) -> BrowserToolOutput:
        args = invocation.command.arguments
        identity = require_json_string(args["observation_ref"], "observation_ref")
        visual = self._visual.get(identity)
        if visual is None:
            raise ValueError("visual_observation_unavailable")
        if visual["document"] != context.document or visual["viewport"] != await self._viewport(state.page):
            raise ValueError("visual_observation_stale")
        x, y = image_number(args["x"]), image_number(args["y"])
        if not 0 <= x < image_number(visual["width"]) or not 0 <= y < image_number(visual["height"]):
            raise ValueError("visual_point_outside_image")
        clip = decode_json_object(visual["clip"])
        current = await self._capture_png(state.page, clip)
        if pixel_digest(current) != visual["raw_sha256"]:
            raise ValueError("visual_pixels_changed")
        viewport = decode_json_object(visual["viewport"])
        x /= image_number(visual["scale_x"])
        y /= image_number(visual["scale_y"])
        offset_x = image_number(clip["x"]) - image_number(viewport["x"])
        offset_y = image_number(clip["y"]) - image_number(viewport["y"])
        if not 0 <= x + offset_x < image_number(viewport["width"]) or not 0 <= y + offset_y < image_number(viewport["height"]):
            raise ValueError("visual_point_outside_viewport; scroll and capture a fresh image")
        await self._mark(invocation, "visual_input_dispatch", dispatch="unknown")
        await state.page.mouse.click(x + offset_x, y + offset_y)
        await self._mark(invocation, "visual_input_complete", dispatch="confirmed")
        self._observation.revision += 1
        return BrowserToolOutput("Delivered the click in the verified image space.", True,
            {"action": "visual_click", "observation_ref": identity, "delivery": "delivered", "effect_verified": False})

    async def _visual_or_file(self, invocation: Invocation, state: PageState, context: FrameContext) -> tuple[JsonValue, bytes]:
        action, args = invocation.command.action, invocation.command.arguments
        if action == BrowserAction.SCREENSHOT:
            return await self._screenshot(invocation, state, context)
        if action == BrowserAction.VISUAL_CLICK:
            return (await self._visual_click(invocation, state, context)).payload(), b""
        if action == BrowserAction.UPLOAD:
            return (await self._upload(invocation, context)).payload(), b""
        if action != BrowserAction.DOWNLOAD:
            raise ValueError("unsupported browser action")
        ref = require_json_string(args.get("ref", ""), "ref")
        record = await self._resolve(context, ref)
        await self._mark(invocation, "download_dispatch", dispatch="unknown")
        async with state.page.expect_download(timeout=15000) as pending:
            await record.handle.click(timeout=5000, no_wait_after=True)
            await self._mark(invocation, "download_click_complete", dispatch="confirmed")
        download = await pending.value
        path = await download.path()
        if path is None:
            raise ValueError("download_file_unavailable")
        from codey.automation.browser_ipc import MAX_BLOB_BYTES

        with path.open("rb") as stream:
            binary = stream.read(MAX_BLOB_BYTES + 1)
        if len(binary) > MAX_BLOB_BYTES:
            raise ValueError("download_exceeds_transfer_budget")
        await self._mark(invocation, "download_complete", dispatch="confirmed")
        value: JsonObject = {"action": "download", "ref": ref, "delivery": "delivered", "suggested_filename": download.suggested_filename,
                 "bytes": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}
        return BrowserToolOutput("Downloaded the selected file.", True, value).payload(), binary

    async def _file_control_ref(self, context: FrameContext, target: JsonObject) -> str:
        selector = "id" if "id" in target else "name"
        matches = [(ref, row) for ref, row in self._file_controls.items() if row[selector] == target[selector]]
        if len(matches) != 1 or (selector == "name" and self._file_controls_truncated):
            candidates = [row for _, row in matches] or list(self._file_controls.values())
            raise ValueError("Choose one file input using a current observed ID. Candidates: "
                             + json.dumps(candidates, ensure_ascii=False))
        ref, observed = matches[0]
        record = await self._resolve(context, ref)
        current = await file_control_details(record.handle)
        if current != {key: value for key, value in observed.items() if key != "id"}:
            raise RuntimeError("file_observation_expired")
        return ref

    async def _upload(self, invocation: Invocation, context: FrameContext) -> BrowserToolOutput:
        args = invocation.command.arguments
        ref = require_json_string(args.get("ref", ""), "ref")
        expected = args.get("expected_document")
        if expected is not None and expected != context.document:
            raise ValueError("file_observation_expired" if "target" in args else "upload_document_changed")
        if not ref:
            if self._file_document != context.document:
                raise ValueError("file_observation_expired")
            try:
                ref = await self._file_control_ref(context, decode_json_object(args["target"]))
            except ValueError as exc:
                return BrowserToolOutput("ERROR: " + str(exc), False,
                    {"action": "upload", "ref": "", "delivery": "not_delivered", "reason": "file_target_unavailable"})
        record = await self._resolve(context, ref)
        await self._mark(invocation, "file_selection_dispatch", dispatch="unknown")
        name, mime = require_json_string(args["name"], "name"), require_json_string(args["mime_type"], "mime_type")
        await record.handle.set_input_files({"name": name, "mimeType": mime, "buffer": invocation.binary}, timeout=5000)
        await self._mark(invocation, "file_selection_complete", dispatch="confirmed")
        raw: object = await _evaluate(record.handle, "el => Array.from(el.files||[],f=>({name:f.name,bytes:f.size,type:f.type}))")
        files = decode_json_object({"files": raw})["files"]
        verified = files == [{"name": name, "bytes": len(invocation.binary), "type": mime}]
        self._observation.revision += 1
        # File selection readback proves the input state, not server acceptance.
        value: JsonObject = {"action": "upload", "ref": ref, "delivery": "verified" if verified else "unverified",
            "effect_verified": False, "name": name, "media_type": mime, "bytes": len(invocation.binary),
            "sha256": hashlib.sha256(invocation.binary).hexdigest()}
        return BrowserToolOutput("Selected and verified the imported attachment." if verified else "File readback differed.", verified, value)
