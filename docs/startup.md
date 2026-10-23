# Desktop startup

The desktop creates one window with a character pond before attaching
to the shared service. The existing startup lock, immutable release snapshots,
service instance lease, upgrade journal and rollback coordinator still own startup
and cutover. The page does not start a task runtime or maintain service facts.
Service preparation runs in the native host's background callback, independently
of document loading and JavaScript bridge initialization. Once both the service
and the shown host are ready, that same window navigates to its single-use launch URL.
The packaged startup asset uses the same animation source as the empty-chat pond.
It covers frontend restoration and fades out only after application boot completes.
Normal startup has no progress copy; phase details belong in the timing log.

Errors retain the startup page and expose Retry. Concurrent retries cannot spawn
another preparation in the same window. Closing a window suppresses later page
updates and navigation; it does not shut down an existing shared service. An
already admitted independent upgrade remains governed by its journal and recovery.

The standalone startup script observes main-module and dependency load errors
without importing the main application. A single native 30-second readiness
deadline also covers a failed startup script or a document that never reports
readiness. The same deadline mechanism bounds initial host display and then
application readiness; phase tokens prevent an expired host timer from settling
a later navigation in the same attempt. Error, ready and close cancel the active
deadline. Loaded events are diagnostic only. Feedback is embedded in the startup
document instead of synchronous native `evaluate_js` calls. Late callbacks cannot
settle a newer attempt, and native presentation does not hold the preparation gate.
There is no additional service/task runtime and no progress callback channel;
stage measurements are recorded directly in the startup log.

## Inputs and validation

`codey/runtime.json` is the sole runtime contract for checkouts, source archives,
wheels, snapshots and validation copies. It records version, dependency constraints
and the exact package-file inventory. Runtime execution never needs a repository
`pyproject.toml`; that file is consumed only during publication. Both the development
launcher and the wheel build use the same publisher/inventory policy. Reused wheel
build directories discard obsolete files before publication. Missing descriptors,
missing files and unpublished extra files are rejected. There is no legacy-snapshot
reader or data migration path; testing and this format transition use fresh state.

After manually publishing frontend assets in a checkout, run
`python -m tools.publish_runtime` before snapshot tests. Normal development launch
does both publication steps automatically. Installed wheels execute neither build
step and need no Node installation. The source archive includes its build backend
and the frontend CSS required to rebuild it.

The frontend publisher excludes only explicit `.test` / `.spec` files with
JavaScript or TypeScript extensions under `frontend/src`. Production files,
public assets, package/lock files, TypeScript/Vite configuration, HTML and the
publisher's own rules remain fingerprinted.

Successful default compilation and CLI-help checks may be reused only when the
runtime content digest, Python executable and version, Python configuration,
installed dependency versions and metadata, and validation commands/rules match.
Malformed, missing, failed or mismatched cache records cause validation to run.
Custom verification commands always run for a new preparation request; exact
request replay still returns the original outcome without repeating commands.
The isolated validation copy and immutable candidate are checked for mutations.
Activation rejects validation from a changed environment.

An unchanged, healthy running release attaches directly, without preparation or
validation. A recovery snapshot with another release ID can also attach directly
when its on-disk runtime digest matches the current content. Missing or changed
recovery content goes through normal preparation. Installed packages already ship
the frontend and do not need Node or development-source frontend checks. Cold
service startup and changed-release activation remain distinct from a warm attach;
none of these paths creates another task runtime.

Database evidence is never stored in the static cache. Each new preparation
backs up the current SQLite databases and runs the service preflight against
those copies, including on static-cache hits. Automatic startup attempts use new
preparation and activation identities, so an explicit Retry does not get stuck
reading a previous failed operation. Exact activation request replay still reads
its original journal. The coordinator retains its fresh live-schema checks and
rollback behavior.

## Timing

`~/.codey/startup-timings.jsonl` contains bounded diagnostic observations grouped
by startup ID and process ID. Durations use a monotonic clock. Log failure cannot
fail startup. Rotation keeps the current log and one previous log, each about 1 MB.
These logs contain stage names and timings, never launch URLs or credentials.

Stages distinguish startup-lock wait, frontend verification, snapshot creation,
service health, validation commands, database backup/preflight, activation,
window shown, document loaded and interface ready. The final interface-ready
signal is emitted after the normal frontend boot completes. It is distinct from
the window becoming visible or the browser finishing document loading.
