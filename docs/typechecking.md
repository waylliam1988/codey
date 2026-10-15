# Type checking and frontend boundaries

The current gates are defined in [CI](../.github/workflows/ci.yml),
[pyproject.toml](../pyproject.toml), and
[frontend/package.json](../frontend/package.json).
This guide replaces the historical mypy/Pyrefly rollout baselines and frontend
migration checklist. Historical diagnostic counts are not current results.

## Python

CI installs the direct tool versions pinned in
[requirements-ci.txt](../requirements-ci.txt). All three checkers cover `codey`
on Windows Python 3.11, 3.12 and 3.13, and Linux Python 3.12. Each command passes
the target Python version explicitly; changing the interpreter alone does not
override a checker setting or the project's minimum supported version.

```powershell
python -m mypy codey --python-version 3.11
pyrefly check codey --python-version 3.11
ty check codey --python-version 3.11
```

Use `3.12` or `3.13` to check another target. These target-version checks do not
replace running tests on the corresponding interpreter and operating system.
Tests and experiment scripts are outside this full-production-tree typing gate.

Mypy uses the strict settings in `pyproject.toml`. Pyrefly has an explicit
`legacy` preset and error severities in that file. Warnings, existing suppression
directives and active errors are separate facts: a successful error gate is not
a claim that no warnings or dynamic types exist. Fix diagnostics at their owning
boundary; do not add baseline exemptions or disable a target to obtain success.

[CI configuration tests](../tests/test_ci_full_tree_typing_gate.py) require all
three commands, explicit target versions, pinned installations and blocking
execution. The historical raw mypy files under `tools/` and
`pyrefly-baseline-2026-10-09.json` are rollout records, not CI error allowances.

## Frontend

The production source is TypeScript under `frontend/src`. From `frontend/`:

```text
npm ci
npm run check
npm run build
```

`check` runs strict TypeScript checking, formatting and Vitest. Publish the
validated production build from the repository root:

```text
python -m tools.publish_frontend
python -m tools.publish_frontend --check
```

The following boundaries are maintained by
[frontend hygiene tests](../frontend/src/type_hygiene.test.ts) and the adjacent
decoder and browser tests:

- Domain records and module dependencies use named interfaces. Dynamic message
  payloads use `unknown` and require narrowing before typed use.
- Provider catalog/status and SSE payloads enter through decoders. Malformed
  data follows the existing error path rather than a silent compatibility boot.
- The frontend does not expose `window.Codey*` globals. Production modules use
  the internal runtime registry; tests explicitly opt into its browser bridge.
- The registry in [runtime.ts](../frontend/src/runtime.ts) remains dynamic and
  contains explicit TypeScript `any` declarations. The current tree is therefore not
  described as globally free of explicit `any`. Keep that fact separate from
  the named DTO and dependency boundaries enforced by tests.
- Type checks do not verify layout, text selection, focus or scrolling. Changes
  to those behaviors need relevant real-browser regressions after publishing.

Visual rules belong in [DESIGN.md](../DESIGN.md); current validation results
belong in [TEST_REPORT.md](../TEST_REPORT.md).
