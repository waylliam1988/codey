import difflib
import json

import round2
from experiment_paths import DATA_ROOT

report = {}
for arm in ("A2", "B2"):
    with round2.arm_patch_context(arm):
        payload = round2.dump_directive(arm)
    report[arm] = {"arm": arm, "directive": payload}
report["C2"] = {"arm": "C2", "directive": {"text": "", "selected_count": 0}}
(DATA_ROOT / "directives2.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
for arm in ("A2", "B2"):
    text = report[arm]["directive"].get("text", "")
    (DATA_ROOT / f"directive_{arm}.txt").write_text(text, encoding="utf-8")
    print(arm, "selected", report[arm]["directive"].get("selected_count"),
          "bytes", len(text.encode("utf-8")))
a = report["A2"]["directive"]["text"].splitlines()
b = report["B2"]["directive"]["text"].splitlines()
print("identical:", report["A2"]["directive"]["text"] == report["B2"]["directive"]["text"])
print("\n".join(difflib.unified_diff(b, a, "B2", "A2", lineterm="", n=0)))
