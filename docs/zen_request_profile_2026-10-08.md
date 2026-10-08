# Temporary Zen request profile (2026-10-08)

This is a removable connection adaptation under the authorized OpenCode
partnership. It does not change Codey's task policy, execution or completion gate.

## What the evidence establishes

Muse returned `403 FreeTierError` for plain text and reduced read-only requests.
Controlled live probes kept the headers, session, request ID and prompt identical:

| Wire declarations | Observed response |
| --- | --- |
| Read-only declarations | 403 |
| The same declarations plus shell | 200 |
| Read-only again | 403 |
| A minimal real read + shell pair | 200 |
| Shell alone | 403 |

The installed OpenCode desktop and reference source use the same partner identity
format. The Console inference guard itself is not in the supplied source; these
observations establish a request-shape restriction, not its complete private
implementation or a promise about future service behavior. Waiting and retrying
403 does not correct this deterministic request mismatch.

Probe artifacts are ignored under `.e2e-artifacts/zen-diagnosis-current/`.
They use isolated fixtures, not the user's project source. Reports do not publish
credentials or request headers.

## Ownership and execution boundary

- `providers/zen/declarations.py` owns wire aliases and the temporary minimum
  read/shell profile. Missing declarations say they are unavailable. It reuses
  existing parameter schemas and never mutates the caller's frozen tool list.
- `providers/zen/connection.py` owns the text-only exchange. An ordinary text
  response takes one generation. Any tool call gets an exact-ID **not executed**
  result; at most two closure rounds share the original deadline. Cancelled,
  truncated or exhausted exchanges are not accepted as successful text.
- Native supplemental calls map back to canonical names. The task kernel still
  validates them against its original authorized snapshot before execution.
  A network declaration is not a task grant, shell approval or completion fact.
- The wrapper serializes the entire logical exchange and invalidates late replies
  on cancellation. Shared API code contains no Zen-specific branches.
- Review model identity includes the actual temporary declarations, descriptions,
  parameter schemas and text prefix. Access observations use the request-profile
  contract scope, so refusal observations from the old shape are not reused.
- Explicit HTTP refusals and unknown generations are never automatically replayed.
  Existing pre-submission TLS connection retry remains a separate transport rule.

This deliberately changes Zen's network declaration projection. It does not
change the authorized execution snapshot for Zen, Local or browser providers.

## Review routing

The first formal review smoke selected an open DeepSeek website before Muse.
Its result was complete, but the gate correctly failed because there was no
request to the chosen API model. Standalone API review now pins the frozen
selected model before considering websites and declines unavailable selection.
Project automatic review retains its independent web/API reviewer policy.
Both consume one `run_review_attempt` entry; no additional review loop was added.

## Deterministic contracts and live result

The machine gate includes the transport authorization/ID/closure tests,
standalone Local/Zen entry tests, HTTP refusal projection, incomplete Responses
items, decoder opt-in, offline CI fixture admission and registry isolation.
The third-task demo now cleans up its registration after every test run.

Final Muse smoke, `muse-spark-1.3-contributor-free`, native/Responses:

| Case | Result | HTTP requests | HTTP retries | Seconds |
| --- | --- | ---: | ---: | ---: |
| chat | Pass | 1 | 0 | 5.919 |
| read | Pass; read_file → done; no file changes | 3 | 0 | 7.878 |
| review | Pass; complete result restored from artifact/ledger | 1 | 0 | 6.692 |

Raw evidence: `.e2e-artifacts/zen-diagnosis-current/final-muse-gate/`.
This is a three-case compatibility smoke, not the full release matrix, model
quality comparison, UI E2E run or whole-project mathematical no-bug proof.

## Removing the partnership

Remove `codey/providers/zen/` and the Zen entry in `providers/catalog.py`.
Remove the associated Zen-specific test/gate selections and documentation.
Local gates fingerprint Zen files only when Zen is explicitly selected.
The shared API runtime, Chat Completions/Responses codecs, Local decoder and
task kernel remain; old Zen runs retain their stored history. There is no
automatic alternate-connection or alternate-model fallback.

`tests/test_optional_zen_removal_preserves_local_api.py` installs an import barrier
in a fresh process and removes the registration/label. Both Local protocols then
complete a real HTTP read/done/receipt task; desktop catalogs, CLI help, old stored
history and Local gate metadata still work. An old Zen connection is rejected,
and no Zen import is attempted. Keep this absence probe when removing the package;
it accepts an already absent registration. This deterministic test does not prove
compatibility with every future model or replace the live release matrix.
