# Ghost 0.6.1 admission live smoke report

## per-family behavior (no aggregate score)

- lang_on_d2 [admission_smoke_20261021]: lang 20/20 (100%, chars_avg=641)

## frozen matrix coverage

- status: **INCOMPLETE**
- required suites: modelA
- missing: [{"suite": "modelA", "family": "language", "role": "on", "cell": "lang_on_d2", "runs": 0, "required_runs": 20}, {"suite": "modelA", "family": "language", "role": "off", "cell": "lang_off_d2", "runs": 0, "required_runs": 20}, {"suite": "modelA", "family": "command", "role": "on", "cell": "cmd_on_d2", "runs": 0, "required_runs": 20}, {"suite": "modelA", "family": "command", "role": "off", "cell": "cmd_off_d2", "runs": 0, "required_runs": 20}]

## promotion verdicts

| family | on | off | rule | verdict |
|---|---|---|---|---|
| language | 0% (0/20) | 0% (0/20) | promote only when language delivery beats withheld control | **INCOMPLETE** |
| command | 0% (0/20) | 0% (0/20) | promote only when task-matched command outcome beats control | **INCOMPLETE** |
| format_length_answer_first | - | - | - | **WEAK-HINT-ONLY (no compliance evidence)** |
| self_description | - | - | - | **EXCLUDED by default** |
| hebbian | - | - | - | **ordering feature + explainability only; no final say** |
