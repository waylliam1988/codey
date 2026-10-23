# Desktop startup

The desktop creates one window with a quiet English startup page before attaching
to the shared service. The existing startup lock, immutable release snapshots,
service instance lease, upgrade journal and rollback coordinator still own startup
and cutover. The page does not start a task runtime or maintain service facts.
Once the service is ready, that same window navigates to its single-use launch URL.

Errors retain the startup page and expose Retry. Concurrent retries cannot spawn
another preparation in the same window. Closing a window suppresses later page
updates and navigation; it does not shut down an existing shared service. An
already admitted independent upgrade remains governed by its journal and recovery.

## Inputs and validation

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
