"""Common-contract frozen A/B/C diagnostic experiment; no production changes.

python -m tests.manual.research_semantic_review_c_abc --phase prepare
python -m tests.manual.research_semantic_review_c_abc --phase dev
python -m tests.manual.research_semantic_review_c_abc --phase fresh
python -m tests.manual.research_semantic_review_c_abc --phase stress
python -m tests.manual.research_semantic_review_c_abc --phase export
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from tests.manual import research_semantic_review_ab as old
from tests.manual.research_semantic_review_c_cases import fresh_cases
from tests.manual.research_semantic_review_c_prompts import PROMPTS
from tests.manual.research_semantic_review_cases import cases as old_cases
from tests.manual.research_semantic_review_export import metrics

ROOT = Path("tests/manual/results/research-semantic-review-c-abc-20261106")
REPORT = Path("docs/reports/research-semantic-review-c-abc-20261106.json")
SEEDS = {"dev": [930], "fresh": [931, 932], "stress": [933, 934]}


def all_cases():
    selected = [copy.deepcopy(c) for c in old_cases() if c["id"] in {"unless", "arithmetic"}]
    previous = Path(__file__).resolve().parents[2] / "docs/reports/research-semantic-review-ab-20261106.json"
    prior = json.loads(previous.read_text(encoding="utf-8"))["specifications"]["extension"]["fixtures"]
    selected += [copy.deepcopy(c) for c in prior if c["id"] in {"binding", "injection"}]
    for c in selected:
        c["split"] = "dev"
        c["category"] = {"unless": "necessity", "arithmetic": "inference"}.get(c["id"], c["id"])
    return selected + fresh_cases()


def model_input(case):
    sources = {e["label"]: e for e in case["evidence"]}
    if len(sources) != len(case["evidence"]):
        raise ValueError("duplicate evidence identities")
    units = old.units(case["markdown"])
    for unit in units:
        unit["bound_evidence"] = [sources[label] for label in dict.fromkeys(unit["evidence_refs"])]
    return {"question": case["question"], "report_digest": old.digest(case["markdown"]), "units": units}


def validate_anchors(row, unit):
    anchors = row.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError("anchors must be an array")
    sources = {s["label"]: s for s in unit["bound_evidence"]}
    grounded = []
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError("invalid anchor object")
        label, quote, purpose = (anchor.get(k) for k in ("evidence_ref", "quote", "purpose"))
        if not isinstance(label, str) or label not in sources:
            raise ValueError("anchor source is outside this unit's citation binding")
        if not isinstance(quote, str) or not quote or quote not in sources[label]["excerpt"]:
            raise ValueError("anchor quote is not exact text in its bound source")
        if purpose not in {"supports", "contradicts", "context"}:
            raise ValueError("invalid anchor purpose")
        start = sources[label]["excerpt"].index(quote) + sources[label]["locator"]["start"]
        grounded.append({**anchor, "start": start, "end": start + len(quote)})
    required = {"entailed": "supports", "partially": "supports", "contradicted": "contradicts"}.get(row["verdict"])
    if required and not any(a["purpose"] == required for a in grounded):
        raise ValueError("verdict lacks the required anchor purpose")
    return grounded


def interpret(case, content):
    data = model_input(case)
    parsed, framing_error = {}, None
    try:
        parsed = old.parse_reply(content, data["units"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        framing_error = str(exc)
    verified, anchor_errors, anchors = {}, {}, {}
    for unit in data["units"]:
        row = parsed.get(unit["id"])
        if row is None:
            continue
        try:
            anchors[unit["id"]] = validate_anchors(row, unit)
        except (ValueError, TypeError) as exc:
            anchor_errors[unit["id"]] = str(exc)
        else:
            verified[unit["id"]] = row
    graded = old.grade(case, verified)
    for row in graded:
        row["model_verdict"] = parsed.get(row["id"], {}).get("verdict", "invalid")
        row["reason"] = parsed.get(row["id"], {}).get("reason", "")
        row["anchors"] = anchors.get(row["id"], [])
        row["anchor_error"] = anchor_errors.get(row["id"])
    return {"rows": graded, "error": framing_error or ("anchor_validation_failed" if anchor_errors else None),
            "framing_error": framing_error, "anchor_errors": anchor_errors}


def production_identity():
    paths = subprocess.check_output(["git", "ls-files", "codey"], text=True).splitlines()
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}


def source_identity():
    names = ["research_semantic_review_c_abc.py", "research_semantic_review_c_cases.py", "research_semantic_review_export.py",
             "research_semantic_review_c_prompts.py"]
    return {**old.signature(), **{name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                 for name in names}}


def freeze(root):
    identity = {"sources": source_identity(), "prompts": old.digest(PROMPTS),
                "fixtures": old.digest(all_cases()), "production": production_identity()}
    path = root / "preregistration.json"
    if path.exists():
        spec = json.loads(path.read_text(encoding="utf-8"))
        if identity != spec["identity"]:
            raise RuntimeError("frozen inputs or production changed; do not mix runs")
        return spec
    spec = {"identity": identity, "prompts": PROMPTS, "fixtures": all_cases(), "seeds": SEEDS,
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "sampling": {"temperature": .3, "top_p": .9, "max_tokens": 1800, "enable_thinking": False},
            "design": "same anchors contract and bound-evidence context across arms; no production integration",
            "decision": "paired repairs/regressions against B, targeted errors and costs; no arbitrary percentage gate"}
    old.write(path, spec)
    return spec


def run_one(root, case, seed, arm, model):
    target = root / "runs" / f"{case['id']}-{seed}-{arm}.json"
    if target.exists():
        return
    before = old.idle_backend()
    data = model_input(case)
    payload = {"model": model, "messages": [{"role": "system", "content": PROMPTS[arm]},
               {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
               "temperature": .3, "top_p": .9, "seed": seed, "max_tokens": 1800,
               "stream": False, "chat_template_kwargs": {"enable_thinking": False}}
    request = Request(old.BASE + "/v1/chat/completions", json.dumps(payload).encode(), {"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urlopen(request, timeout=180) as response:
            raw = json.load(response)
    except (OSError, ValueError) as exc:
        old.write(target, {"transport_error": repr(exc), "request": payload})
        raise RuntimeError("transport failure retained; no inference retry") from exc
    interpreted = interpret(case, raw["choices"][0]["message"]["content"])
    row = {"case": case["id"], "split": case["split"], "category": case["category"], "seed": seed, "arm": arm,
           "request": payload, "response": raw, "input_digest": old.digest(data),
           "seconds": round(time.monotonic() - started, 3), "backend_before": before,
           "backend_after": old.get("/api/extra/perf"), **interpreted}
    old.write(target, row)
    print(case["id"], seed, arm, "error=" + str(row["error"]), flush=True)


def scored(runs, *, raw=False):
    values = copy.deepcopy(runs)
    if raw:
        for r in values:
            r["error"] = r["framing_error"]
            for u in r["rows"]:
                u["predicted"] = u["model_verdict"]
    return {**metrics(values), "framing_failures": sum(r["framing_error"] is not None for r in runs),
            "anchor_invalid_units": sum(len(r["anchor_errors"]) for r in runs)}


def paired(runs, arm="C", *, raw=False):
    by_key = {(r["case"], r["seed"], u["id"], r["arm"]): u for r in runs for u in r["rows"]}
    changes = []
    key = "model_verdict" if raw else "predicted"
    for (case, seed, uid, a), before in by_key.items():
        if a != "B" or (case, seed, uid, arm) not in by_key:
            continue
        after = by_key[case, seed, uid, arm]
        if before[key] != after[key]:
            changes.append({"case": case, "seed": seed, "unit": uid, "text": before["text"],
                "gold": before["gold"], "B": before[key], arm: after[key],
                "repaired": before[key] != before["gold"] and after[key] == after["gold"],
                "regressed": before[key] == before["gold"] and after[key] != after["gold"]})
    return {"repaired": sum(c["repaired"] for c in changes), "regressed": sum(c["regressed"] for c in changes),
            "changes": changes}


def export(root):
    spec = freeze(root)
    paths = sorted((root / "runs").glob("*.json"))
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    expected = sum(len(SEEDS[c["split"]]) * len(PROMPTS) for c in all_cases())
    if len(runs) != expected:
        raise RuntimeError(f"expected {expected} complete requests, found {len(runs)}")
    groups = []
    for phase in SEEDS:
        for arm in PROMPTS:
            selected = [r for r in runs if r["split"] == phase and r["arm"] == arm]
            groups.append({"phase": phase, "arm": arm, "verified": scored(selected), "raw": scored(selected, raw=True)})
    targets = []
    for category in ("necessity", "injection", "binding", "inference", "scope", "material_scope", "compound", "recognition"):
        for arm in PROMPTS:
            selected = [r for r in runs if r["split"] != "dev" and r["arm"] == arm and r["category"] == category]
            targets.append({"category": category, "arm": arm, "verified": scored(selected), "raw": scored(selected, raw=True)})
    archive = Path(".e2e-artifacts/research-semantic-review-c-abc-20261106-evidence.zip")
    files = [root / "preregistration.json", root / "backend.json", *paths]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, str(path))
    compact = [{**{k: r[k] for k in ("case", "split", "category", "seed", "arm", "input_digest", "seconds", "error",
                                    "framing_error", "anchor_errors", "rows")},
                "usage": r["response"].get("usage", {})} for r in runs]
    fresh = [r for r in runs if r["split"] != "dev"]
    result = {"specification": spec, "groups": groups, "targets": targets,
        "paired_new_verified": paired(fresh), "paired_new_raw": paired(fresh, raw=True),
        "runs": compact, "production_unchanged": production_identity() == spec["identity"]["production"],
        "archive": {"path": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
        "manifest": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
    old.write(REPORT, result)
    for g in groups:
        print(g["phase"], g["arm"], json.dumps(g["verified"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("prepare", "dev", "fresh", "stress", "export"), required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    spec = freeze(args.root)
    if args.phase == "prepare":
        old.write(args.root / "backend.json", {"model": old.get("/api/v1/model"), "version": old.get("/api/extra/version"),
                                               "perf": old.idle_backend()})
        print("Frozen", old.digest(spec))
        return
    if args.phase == "export":
        export(args.root)
        return
    model = old.get("/v1/models")["data"][0]["id"]
    for index, case in enumerate(c for c in all_cases() if c["split"] == args.phase):
        for seed in SEEDS[args.phase]:
            shift = (index + seed) % len(PROMPTS)
            order = list(PROMPTS)
            for arm in order[shift:] + order[:shift]:
                run_one(args.root, case, seed, arm, model)
    freeze(args.root)


if __name__ == "__main__":
    main()
