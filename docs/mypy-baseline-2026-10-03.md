# Full-tree mypy baseline (2026-10-03)

This is the frozen starting point for the staged typing cleanup. No
production module, test assertion, or CI gate was changed for this baseline.

## Environment

- Python: 3.12.8
- mypy: 1.18.2 (the version pinned by `requirements-ci.txt`)
- Command: `python -m mypy codey`
- Source files checked: 363
- Raw output: [`tools/mypy-baseline-2026-10-03.txt`](../tools/mypy-baseline-2026-10-03.txt)
- Reproducible classifier: [`tools/analyze_mypy_baseline.py`](../tools/analyze_mypy_baseline.py)

## Counts

`506` errors were reported in `138` files. The error-code distribution is:

| Code | Count |
| --- | ---: |
| `arg-type` | 161 |
| `attr-defined` | 108 |
| `call-overload` | 64 |
| `union-attr` | 64 |
| `assignment` | 29 |
| `index` | 21 |
| `return-value` | 12 |
| `misc` | 12 |
| `var-annotated` | 11 |
| `operator` | 7 |
| `no-redef` | 6 |
| `list-item` | 3 |
| `return` | 2 |
| `dict-item` | 2 |
| `exit-return` | 1 |
| `import-untyped` | 1 |
| `type-var` | 1 |
| `valid-type` | 1 |

The classifier reports `249` unique normalized `(error code, message)` forms.
The largest confirmed repeated root patterns are:

| Root pattern | Count |
| --- | ---: |
| numeric coercion: `int(object)` | 57 |
| nullable member access without a guard | 35 |
| object-typed value used as a structured object | 70 |
| numeric coercion: `float(object)` | 14 |
| object-typed value indexed or assigned | 11 |
| untyped lambda boundary | 8 |

The remaining rows are retained individually in the raw output and in the
classifier's JSON output. Pattern counts are triage aids, not permission to
apply a bulk edit: each fix still requires a behavior regression test and a
root-cause decision in the later stages.

## Hot files

The files with the most diagnostics are `codey/runs/receipt.py` (31),
`codey/operations/task_loop.py` (21), `codey/app/server.py` (17),
`codey/operations/project_writer_phase.py` (15), and
`codey/research/evidence_ledger.py` / `codey/research/proof_quality.py` (14
each). These are triage locations only; no conclusion that every diagnostic is
a production bug is made at this stage.

## Version comparison

An earlier local run with mypy 1.13.0 produced 510 diagnostics. That result is
not part of the frozen baseline because CI installs 1.18.2. The only frozen
number for this migration is the 1.18.2 result above.

## Current status (2026-10-03)

After the staged boundary cleanup, the same command now reports **0 errors
in 363 source files** with mypy 1.18.2. The raw file above remains unchanged
as the frozen starting point; this section records the ratcheted result.
