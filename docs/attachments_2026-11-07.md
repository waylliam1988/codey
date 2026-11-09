# Composer attachments

## Scope and appearance

This change implements only the approved attachment interaction. Retain the
existing sidebar, Settings, model picker, tool stream, Changes view, and reading
behavior. Do not add current-task/model/branch context rows above the composer.
Do not display `Attachment pending` or another normal-state attachment banner.

Files can be dropped onto the composer; pasting an image uploads the clipboard
file instead of inserting a filename as text. There is no `Attach files` button
or file picker. With no attachments or errors, the attachment area is hidden
and reserves no height or padding. It expands for attachments or errors and
collapses again after the final attachment is removed and errors are cleared.
Chips show the filename, a 36px image thumbnail when applicable,
and an accessible Remove action. Uploads use a small spinner; failures retain the
file and offer Retry. Successful attachment UI contains no status sentence.

Only errors occupy the attachment error row. An image whose delivery model has
no confirmed image-input support blocks Send and displays exactly:

> This model does not support image input. Remove the image or choose a supported model.

An attachment is draft content even without a caption. Keep exactly one primary
action: Send when the draft has text or files; Stop for an empty active composer.
Uploading, failed, unknown-capability, or unsupported attachments disable Send.
Removing the final file from an empty active composer restores Stop.

## Input and storage contract

- Up to four attachments and 4 MiB total per submission; 2 MiB per file.
- UTF-8 text/source files and PNG, JPEG, GIF, or WebP images. SVG and other binary
  formats are rejected. Images are bounded to 4 MP and 4096 pixels per side.
- `POST /api/attachments`: `{session_id, name, data}` where data is base64.
  Returns an opaque attachment descriptor. The server derives media type and
  verifies the byte and filename bounds; it does not trust client MIME labels.
- `POST /api/attachments/read`: `{session_id, id}` returns the public descriptor
  and image preview, when applicable. A chat must exist before import/read.
- `POST /api/attachments/capabilities`: `{session_id, provider, model_selection}`
  returns `{images}`. A running task's frozen selection owns Steer capability;
  changing the next-task picker cannot change the current task's transport.
- Run and queued-input requests accept `attachments`, an ordered descriptor list.
  Only the IDs identify content. Names, types, hashes, and contents are resolved
  from server storage, never from supplied descriptor fields.

Uploads are immutable JSON objects under
`<state-home>/input_attachments/<session-key>/`, with content-derived IDs and
integrity hashes. The per-chat upload store is bounded to 32 MiB. Import never
copies files into the project, interprets filenames as paths, or executes them.
Removing a draft chip does not delete archived bytes or another window's input.

Successfully uploaded draft references persist locally per chat; raw file bytes
and image data URLs do not enter localStorage. A browser File that has not
finished uploading remains in the current page for retry, rather than being
misrepresented as durable. Switching chats does not redirect late upload replies.

Admission snapshots full validated content into canonical message metadata.
History and outbox APIs return public descriptors instead of base64/text bodies.
Forks can read attachments on their visible canonical ancestry without gaining
access to unrelated chats' upload stores. Input text remains the user's caption;
an attachment-only submission uses `Review the attached files.` as its default
instruction. Attached documents are data and cannot grant execution capabilities.

## Ownership and delivery

Browser submissions containing attachments always use the existing durable
input outbox, including idle chats. This stores one request identity and its
references before clearing the draft. During an active task the input is Steer;
otherwise it is a follow-up dispatched by the durable task scheduler.

Queued, unconfirmed, or rejected inputs retain their file chips. A lost HTTP
reply never triggers automatic resubmission. Check status resolves the same
identity. A definitive attachment validation rejection exposes its error and
Return to draft restores both caption and files, preserving any newer draft.
Submitted and canonical user messages retain their attachment descriptors.

Provider preparation reads only admitted canonical attachment data. Text files
are delivered as explicitly untrusted, JSON-encoded file content. API image
inputs use actual Chat Completions `image_url` parts or Responses `input_image`
parts. Local delivery-entry markers remain in protocol provenance and never
appear as wire message fields. Accepted history prevents reinjecting the same
attachments on later tool/model requests; new consumed Steers can add new files.
Cold context reconstruction preserves attachment content. Text-only historical
handoffs identify omitted images instead of pretending the model can see them.

Image eligibility requires an explicit model capability and an implemented
adapter. Cline's verified catalog supplies `architecture.input_modalities` and
freezes image support in the admitted selection. Current website adapters and
unverified Local/Zen selections remain blocked for new images; image support is
never inferred from a model name or protocol name. UTF-8 files work through both
API and browser-backed text transports, including adapter workers.

Estimated API budgets count bounded images with a conservative 16,384-token
allowance rather than treating base64 as language tokens. This is an estimate,
separate from reported usage or a declared tokenizer. Text summaries mark image
presence without sending base64 to a text-only summary prompt. Original canonical
image content remains retained.

## Verification

The focused suites are `tests/test_input_attachments.py`,
`tests/test_ui_attachments.py`, `frontend/src/attachments.test.ts`, and the
existing outbox/composer, durable admission, Cline capability, API codec, and
context-budget regressions. Browser checks exercise the shipped frontend and
real storage/queue routes at desktop and narrow widths. They cover error-only
copy, single Send/Stop action, drop/paste, collapsed empty layout, reload/session scope, retry,
rejected-input recovery, and lost-reply Steer consumption.

Model transport tests exercise both actual protocol serializers with controlled
responses; they do not claim a live external model/image invocation. Native
desktop review uses an isolated state directory and controlled run facts. No
full pytest run is required or performed for this change.

The compact-layout update is recorded in
[the 2026-11-09 work report](reports/composer-attachments-compact-20261109.zh-CN.md).
