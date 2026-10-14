# Frontend strict boundary

The migration is complete when the following conditions hold:

1. `npm run typecheck` passes with `strict: true` and no `@ts-nocheck` or explicit `any` in `frontend/src`.
2. Browser globals expose named contracts only. There are no `Codey*` globals, `Function` compatibility types, or `deps: unknown` declarations in `types/window.d.ts`.
3. Every external JSON boundary has a decoder that accepts `unknown` and returns a named DTO. UI modules consume the DTO, never the raw JSON object.
4. Domain entities (`Session`, `Project`, `Message`, research runs, graph nodes) have no catch-all index signatures. Dynamic values are isolated in named fields whose type is `unknown` and narrowed at the use site.
5. Compatibility fallbacks are absent from the cold boot path. A malformed catalog, event, status response, or restore payload is rejected and reported through the existing error path.
6. Tests cover each decoder's accepted and rejected shapes, each boot-critical module's initialization order, and at least one real browser run after publishing the bundle.

The remaining `unknown` values are therefore intentional boundary values, not incomplete model definitions. The final inventory should be zero for catch-all interfaces and compatibility types; it may contain `unknown` only at decoder inputs, error payloads, DOM text helpers, and explicitly dynamic message payloads.

Current work is being reduced against this contract in this order:

- provider catalog and provider status DTOs;
- SSE events and run-state DTOs;
- restore, changes, local-context, and model-settings API DTOs;
- module dependency interfaces and browser global declarations;
- session/project persistence decoding;
- removal of the legacy runtime bridge after the entrypoint imports are direct.
