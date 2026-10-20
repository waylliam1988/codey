# Ghost 0.6.0 frozen evaluation report

## per-family behavior (no aggregate score)

- lang_on_d2 [modelA]: lang 19/20 (95%, chars_avg=655)
- lang_off_d2 [modelA]: lang 0/20 (0%, chars_avg=1086)
- cmd_on_d2 [modelA]: cmd 0/20 (0%, chars_avg=112)
- cmd_off_d2 [modelA]: cmd 0/20 (0%, chars_avg=97)
- cap4_d2 [modelA]: cmd 1/10 (10%, chars_avg=149); lang 10/10 (100%, chars_avg=621)
- cap8_d2 [modelA]: cmd 1/10 (10%, chars_avg=115); lang 10/10 (100%, chars_avg=604)
- cap12_d2 [modelA]: cmd 1/10 (10%, chars_avg=140); lang 9/10 (90%, chars_avg=663)
- cap4_recency [modelA]: cmd 0/10 (0%, chars_avg=105); lang 0/10 (0%, chars_avg=1134)
- cap8_recency [modelA]: cmd 0/10 (0%, chars_avg=50); lang 9/10 (90%, chars_avg=709)
- cap12_recency [modelA]: cmd 0/10 (0%, chars_avg=40); lang 2/10 (20%, chars_avg=1072)
- cap12_hebbian [modelA]: cmd 2/10 (20%, chars_avg=159); lang 6/10 (60%, chars_avg=835)
- override_d2 [modelA]: override 12/12 (100%, chars_avg=1132)

## coverage vs bytes (offline)

- d2@cap4: bytes=469 selected=4 behavior_families=0.50 (Prefer: reply language = Chinese.; Prefer: code notes = minimal prose, co)
- recency@cap4: bytes=438 selected=4 behavior_families=0.00 ()
- hebbian@cap4: bytes=438 selected=4 behavior_families=0.00 ()
- d2@cap8: bytes=626 selected=8 behavior_families=1.00 (Prefer: reply language = Chinese.; Prefer: code notes = minimal prose, co; Task tendency: test command = pytest -; Prefer: format = bullets.)
- recency@cap8: bytes=608 selected=8 behavior_families=0.50 (Prefer: code notes = minimal prose, co; Prefer: reply language = Chinese.)
- hebbian@cap8: bytes=580 selected=8 behavior_families=0.25 (Prefer: format = bullets.)
- d2@cap12: bytes=807 selected=12 behavior_families=1.00 (Prefer: reply language = Chinese.; Prefer: code notes = minimal prose, co; Task tendency: test command = pytest -; Prefer: format = bullets.)
- recency@cap12: bytes=765 selected=12 behavior_families=1.00 (Prefer: code notes = minimal prose, co; Prefer: reply language = Chinese.; Prefer: format = bullets.; Task tendency: test command = pytest -)
- hebbian@cap12: bytes=765 selected=12 behavior_families=1.00 (Prefer: format = bullets.; Prefer: code notes = minimal prose, co; Prefer: reply language = Chinese.; Task tendency: test command = pytest -)

## frozen matrix coverage

- status: **COMPLETE**
- required suites: modelA

## promotion verdicts

| family | on | off | rule | verdict |
|---|---|---|---|---|
| language | 95% (20/20) | 0% (20/20) | promote only when language delivery beats withheld control | **PROMOTE** |
| command | 0% (20/20) | 0% (20/20) | promote only when task-matched command outcome beats control | **SHADOW** |
| format_length_answer_first | - | - | - | **WEAK-HINT-ONLY (no compliance evidence)** |
| self_description | - | - | - | **EXCLUDED by default** |
| hebbian | - | - | - | **ordering feature + explainability only; no final say** |
