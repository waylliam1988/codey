# Model management

Settings controls the models Codey may use. Websites start enabled with the
five registered websites selected; API sources start disabled with no selected
models. Turning a source off retains its subset. All sources off is valid.
Zero selected means off, including in the stored preference snapshot. The empty
source's master toggle is disabled; checking a model or Select all explicitly
enables it. Clear and removing the last selected model turn it off immediately.
In-use models cannot be cleared. Changes remain staged until Save changes.

Save changes is disabled until enablement or selected model-ID sets differ from
the loaded baseline; reverting those values disables it again. Names, ordering,
search and expanded groups do not count as preference edits. Catalog refresh
updates only that source's model rows while keeping staged selections, search,
disclosures and focus. Missing selected IDs remain in the subset, new models
remain unchecked, and a removed focused option returns focus to its source row.

`model-preferences.json` contains a revisioned preference snapshot and separate
non-secret catalog facts. A catalog observation never changes consent or its
revision. Saves use the file lease and an atomic replacement; stale revisions
return 409. Removed registrations are excluded when reading the snapshot.

`/api/model_settings` reads local state only. `/api/model_catalog` is explicit
metadata discovery, including for a disabled source. Automatic API discovery
skips disabled sources. Disabled Local connection settings read saved values
without probing. Neither metadata discovery nor connecting settings performs
inference. Desktop startup no longer warms or opens model websites.

HTTP admission and browser-worker reservation both enforce the selected scope.
Reservation and Settings saves share the application lock. Active writer,
approval-paused model, review and advisor leases protect exact identities while
leaving unused models editable. Automatic failover, repair, reviewers and
advisors use the same scope. HTTP API sends require an explicit model, and the
resolved selection is checked again before admission.

The zero-build frontend has two small modules:

- `models.js` owns the authoritative source snapshot and selection predicates.
- `model_settings.js` renders every source with the same disclosure, master
  toggle and model checkboxes; changes are staged until Save.

Connection fields stay inside Local's `Connection` disclosure. `Save connection`
is an explicit connection operation, separate from model preference Save.
Refresh models after editing the connection to select its current models.
Save connection separately tracks its own fields: unchanged connected forms
cannot save, edits enable Save, and reverting disables it. An unconnected form
with an address can still Connect. A blank key keeps the stored key. Collapsing
Connection does not reload over staged fields; saving either scope leaves the
other scope unsaved. Late responses cannot modify a newly opened dialog.
Model names come from connector metadata, user display names or actual IDs.
Display names never become request IDs. A selected model's display name persists
with the chat so a removed integration remains readable and unavailable.

## Removing an optional integration

Remove its `API_CONNECTIONS` registration and backend package. The shared
frontend contains no Zen/OpenCode-specific branch or label: source rows and
picker entries come from backend data. No replacement frontend adapter or
fallback is required. Old chats retain their identity and draft; Send requires
an explicit enabled choice. The removal probe makes the Zen package unimportable
and exercises Local, bootstrap, history and release-gate metadata. A browser
test removes an arbitrary optional source, independently of the Zen name.

## Offline inspection

Run `python -m tools.offline_models_demo --port 8767` and open
`http://127.0.0.1:8767/`. This serves the shipped assets with in-memory source,
connection and reply fixtures. It imports no Codey/provider runtime, reads no
credentials and makes no outbound requests. The page's CSP permits connections
only to its own origin. Refresh adds an unchecked model; Send produces a local
simulated reply. Restart the simulator to reset its choices.

## Verification

Behavior tests were run red before implementation, including durable subsets,
revision conflicts, exact booleans, no disabled discovery, admission, paused
approval identity, reviewer/advisor scope, old model names, stale UI replies and
optional source removal. Regression coverage includes existing manual retry,
settings recovery, keyboard/focus, chat state and backend task flows.

UI behavior fixtures serve exact packaged asset bytes through the production
asset resolver, with deterministic connection-failure injection. HTTP asset
delivery and package inclusion retain separate integration coverage. Scripted
Research sources use scoped DNS answers and fresh caches while executing the
real URL policy, so offline verification does not depend on public DNS.

Latest Windows/Python 3.12 verification: **7912 passed, 7 skipped, 1503 subtests**;
required machine contracts **679 passed**. See [the test report](../TEST_REPORT.md)
for TDD, the interrupted first continuity run, historical replay and platform limits.

Primary tests: `tests/test_model_preferences.py`,
`tests/test_ui_model_management.py`, `tests/test_offline_models_demo.py`,
`tests/test_ui_settings_save_scopes_and_refresh.py`,
`tests/test_optional_zen_removal_preserves_local_api.py`. The visual baseline is
`DESIGN.md`.
