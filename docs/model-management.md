# Model management

Settings controls the models Codey may use. Websites start enabled with the
five registered websites selected; API sources start disabled with no selected
models. Turning a source off retains its subset. All sources off is valid.
Zero selected means off, including in the stored preference snapshot. The empty
source's master toggle is disabled; checking a model or Select all explicitly
enables it. Clear and removing the last selected model turn it off immediately.
In-use models cannot be cleared. Changes remain staged until Save changes.

All sources use one action toolbar: Select all and Clear stay together on the
left with a 14px gap; only Refresh models is pushed to the right. The shared
renderer marks the refresh action explicitly instead of positioning whichever
button happens to be last. This also works for an API source without discovery.
Refresh appears only when the source declares `discoverable`. Websites lists
registered website connectors, not models discovered inside those websites,
so it currently needs no Refresh models action. Settings paints its local
snapshot immediately, then refreshes enabled built-in catalogs after loading
preferences. Disabled sources and optional integrations require explicit
discovery. Expanding a group does not repeat discovery or save preferences.

Save changes is disabled until enablement or selected model-ID sets differ from
the loaded baseline; reverting those values disables it again. Names, ordering,
search and expanded groups do not count as preference edits. Catalog refresh
updates that source everywhere while keeping staged selections, search,
disclosures and focus. Missing selected IDs remain in the subset and appear in
an expandable unavailable-selection summary; counts separate usable selections
from unavailable ones. New models remain unchecked, and a removed focused
option returns focus to its source row. Old chats retain their model name and
draft, with Send disabled until the user chooses an available enabled model.

`model-preferences.json` contains a revisioned preference snapshot and separate
non-secret catalog facts. A catalog observation never changes consent or its
revision. Saves use the file lease and an atomic replacement; stale revisions
return 409. Removed registrations are excluded when reading the snapshot.

`/api/model_settings` reads local state only. `/api/model_catalog` is explicit
metadata discovery, including for a disabled source. Manual discovery defaults
to `force: true`; automatic requests use `force: false` and skip disabled
sources. A forced refresh bypasses the local freshness interval while retaining
conditional HTTP validation. Failed refresh preserves cached rows and reports
stale/error state in Settings. Disabled Local connection settings read saved values
without probing. Neither metadata discovery nor connecting settings performs
inference. Desktop startup no longer warms or opens model websites.

HTTP admission and browser-worker reservation both enforce the selected scope.
Reservation and Settings saves share the application lock. Active writer,
approval-paused model, review and advisor leases protect exact identities while
leaving unused models editable. Automatic failover, repair, reviewers and
advisors use the same scope. HTTP API sends require an explicit model, and the
resolved selection is checked again before admission.

The TypeScript frontend has two source owners, built into the packaged UI:

- `frontend/src/models.ts` owns the authoritative source snapshot and selection predicates.
- `frontend/src/model_settings.ts` renders every source with the same disclosure, master
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

## Optional account sign-in

Cline uses the same source editor. On Windows, expand Cline and choose Sign in;
the system browser handles device authorization. The source shows its state,
one-time code and expiry while authorization is pending. Repeated clicks reuse
the current attempt; Cancel invalidates it. Closing Settings stops only UI
polling; the backend attempt can finish, and reopening restores its status.
A successful login refreshes the
directory without changing staged selections; explicitly choose models and Save
changes. Sign out is refused while a task or approval continuation uses Cline.

Authentication revisions are separate from catalog timestamps and preference
revisions. Polling cannot overwrite unsaved model choices. Operator-authenticated
`GET /api/connection_auth?source=cline` reads status;
`POST /api/connection_action` accepts `source`, `action` and an optional
`attempt_id`. Login controls are optional connector functions, with no Cline UI
branch. Tokens, device codes and credential-store paths are never returned.

Rejected Cline credentials retain a private `reauth_required` flag in the secure
record. A failed, expired or cancelled replacement login cannot restore them to
Connected, including after backend restart. Retry sign-in remains available;
only a successful replacement authentication restores admission. A late rejection
is matched to its account and token version before invalidating credentials.

The initial secure storage implementation requires Windows Credential Manager;
there is no plaintext fallback on another OS. Free eligibility comes only from
Cline's `free` array (five-minute cache). Capabilities come from exact IDs or
reviewed aliases in the public model directory (one-hour cache). Unknown limits
exclude a model from usable choices. Expired eligibility must refresh before a
request; retired models and changed capability contracts reject stale selections.

The composer retains `[model] [thinking]`. Connectors may opt into a three-state
toggle: Default sends no override, On/Off send explicit booleans. Missing opt-in
preserves existing toggle behavior. Unsupported stored efforts are rejected at
admission rather than silently becoming Default. Cline sends gateway `reasoning`
fields and does not add an agent identity/system prompt. If a catalog removes a
selected effort or changes its reasoning control, the shared UI clears invalid
fields/caches and persists a reselection requirement for Cline, Zen and Local.
It shows “Thinking options changed” and blocks sending until the user chooses
the model or a valid thinking option again. Hidden controls cannot send old
efforts, and a later capability refresh cannot revive the discarded cache.

## Request budgets and usage

Model admission also freezes its context budget and counting method. API static
capabilities carry no model capacity. Zen requires explicit catalog context and
output limits; Local presets are configuration, clamped to the running window
for confirmed KoboldCpp Jinja connections. The latter counts the entire final
request and blocks generation on counting failure. Other supported connections
explicitly estimate preflight context. Service-reported usage remains separate;
missing usage is unknown. See [token accounting](token-accounting.zh-CN.md).

## Removing an optional integration

Zen discovers model API addresses, protocol, limits and explicit zero-cost prices
directly from `https://models.opencode.ai/api.json`. Models with
`status: deprecated` are excluded when parsing both cached and fresh directories;
the settings snapshot uses this local projection to discard obsolete display
cache rows before any network response. ETag and a five-minute lazy refresh
allow approved API paths to change without shipping Codey. Desktop startup
refreshes enabled API sources asynchronously; the open UI also checks hourly.
Manual discovery publishes its result to the status coordinator so an older
in-flight status probe cannot restore retired models. Frontend catalog timestamps
likewise prevent older replies replacing newer facts. Failed refresh preserves
valid cached facts with stale/error information and backs off automatic retries
for five minutes; manual retry remains immediate. There is no second
endpoint-availability index and no dependency on `/models`, OpenCode.exe or its
credentials. A run freezes its endpoint revision; a changed endpoint rejects its
old selection rather than silently rerouting it. New trust domains and unsupported
protocols still require review. See [implementation and removal boundaries](external-control-implementation.zh-CN.md).

Remove its `API_CONNECTIONS` registration and backend package. The shared
frontend contains no Zen/OpenCode-specific branch or label: source rows and
picker entries come from backend data. No replacement frontend adapter or
fallback is required. Old chats retain their identity and draft; Send requires
an explicit enabled choice. The removal probe makes the Zen package unimportable
and exercises Local, bootstrap, history and release-gate metadata. A browser
test removes an arbitrary optional source, independently of the Zen name.
The same probe now executes both Local protocols with tool exchanges, usage and
unbound Chat edits/Shell requests while Zen imports are blocked. Raw Zen usage parsing lives only in its package;
normalized historical records and Run details do not import Zen.

### Cline cleanup before removal

Deleting Python code does not erase operating-system credentials. Stop the
desktop/backend first, then run from the checkout with its environment:

```powershell
python -m tools.cline_clear_data
```

Success removes the `Codey/Cline/default` Credential Manager record, Cline's
catalog and safety block cache, selected models and source display cache. It
retains chat/run history. A failure reports `pending_cleanup` and must be
resolved before claiming complete removal. Normal Sign out removes credentials
and the safety block while retaining the public directory and model preferences.
An unexpected positive user `usage.cost` blocks later requests across restart;
only explicit sign-out/cleanup resets this safety state.

Then remove the single Cline registration in `codey/providers/catalog.py` and
`codey/providers/cline/`. Clean dedicated `tests/test_cline_*.py`,
`tests/test_optional_cline_removal_preserves_other_connections.py`,
`tests/support/cline.py`, `cline_refresh_probe.py`,
`optional_cline_removal_probe.py`, `tools/cline_live_gate.py`,
`tools/cline_clear_data.py` and associated documentation. Generic controls,
codec/stream hooks and frontend files serve other optional connectors and stay.
The credential lock sidecar contains no credential or catalog data; remove it
only after all processes have stopped. Retained history can still contain old
model identities and protocol reasoning metadata; deleting history is a separate
user data operation.

Before removal, run `tests/test_cline_cleanup.py` and
`tests/test_optional_cline_removal_preserves_other_connections.py`. They use
isolated stores, make Cline unimportable in fresh processes, execute both Local
protocols, Zen and auxiliary generation, workspace reads/approvals, history,
CLI startup and an actual headless child. No live account is logged out by tests.
See [implementation evidence and limitations](reports/cline-integration-validation-20261105.zh-CN.md).

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

Current results, full-run failures and focused fixes are maintained in
[the test report](../TEST_REPORT.md), including TDD and platform limits.

Primary tests: `tests/test_model_preferences.py`,
`tests/test_zen_catalog_lifecycle.py`, `tests/test_ui_catalog_lifecycle.py`,
`tests/test_ui_model_management.py`, `tests/test_offline_models_demo.py`,
`tests/test_ui_settings_save_scopes_and_refresh.py`,
`tests/test_optional_zen_removal_preserves_local_api.py`. The visual baseline is
`DESIGN.md`.

## Composer and connector lifecycle

The model menu stays 240px wide regardless of the selected model/effort, shrinking
to fit the viewport. Selected checks retain padding; long labels use ellipsis.
Command approval belongs only to the command card's Allow / Deny buttons, without
a duplicate composer notice. Model-disabled/removed feedback remains with Settings.

Zen's Bun fetch and custom request-ID headers live in its optional package;
Local keeps Python HTTP. Packaging and CI discover existing JS/MJS resources.
Remove its registration/package together with dedicated tests, gates and manifest
entries; old history remains readable, and removed connections cannot send.
The current live matrix includes eight successful SSE conversations, two real
code repairs and approval continuations, plus upstream region/429 failures.
Non-streamed JSON probes were rejected while controlled SSE probes succeeded;
later SSE requests were also rejected. An error named FreeUsageLimitError alone
does not establish actual quota exhaustion. See the
[current evidence](reports/zen-live-validation-20261105.zh-CN.md); historical Ling
results in the maintenance audit do not describe the current catalog.
