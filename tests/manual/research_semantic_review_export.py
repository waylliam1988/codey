"""Export auditable scores plus checksums and a lossless local evidence archive."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from tests.manual import research_semantic_review_ab as ab
from tests.manual import research_semantic_review_extension as extension
from tests.manual import research_semantic_review_isolation as isolation

REPORT = Path("docs/reports/research-semantic-review-ab-20261106.json")


def metrics(runs):
    rows = [u for r in runs for u in r["rows"]]
    factual = [u for u in rows if u["gold"] != "not_claim"]
    good = [u for u in factual if u["gold"] == "entailed"]
    bad = [u for u in factual if u["gold"] in ab.BAD]
    uncited = [u for u in bad if not u["evidence_refs"]]

    def ratio(n, d):
        return round(n / d, 6) if d else None

    return {"runs": len(runs), "units": len(rows), "factual_units": len(factual),
        "good_units": len(good), "bad_units": len(bad),
        "four_class_accuracy": ratio(sum(u["predicted"] == u["gold"] for u in factual), len(factual)),
        "bad_detected": sum(u["predicted"] in ab.BAD for u in bad),
        "bad_recall": ratio(sum(u["predicted"] in ab.BAD for u in bad), len(bad)),
        "good_flagged": sum(u["predicted"] in ab.BAD for u in good),
        "good_false_positive": ratio(sum(u["predicted"] in ab.BAD for u in good), len(good)),
        "given_unit_claim_recognition_recall": ratio(
            sum(u["predicted"] in ab.LABELS - {"not_claim"} for u in factual), len(factual)),
        "uncited_bad_recall": ratio(sum(u["predicted"] in ab.BAD for u in uncited), len(uncited)),
        "validity": ratio(sum(r["error"] is None for r in runs), len(runs)),
        "input_tokens": sum(r["response"].get("usage", {}).get("prompt_tokens", 0) for r in runs),
        "output_tokens": sum(r["response"].get("usage", {}).get("completion_tokens", 0) for r in runs),
        "seconds": round(sum(r["seconds"] for r in runs), 3)}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    roots = {"primary": ab.ROOT, "extension": extension.ROOT, "isolation": isolation.ROOT}
    specs, runs, files = {}, [], []
    for name, root in roots.items():
        spec_path = root / "preregistration.json"
        specs[name] = json.loads(spec_path.read_text(encoding="utf-8"))
        files.append(spec_path)
        for path in sorted((root / "runs").glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            files.append(path)
            runs.append({"experiment": name, "path": str(path), "sha256": sha(path),
                **{k: raw[k] for k in ("case", "split", "seed", "arm", "input_digest", "seconds", "error", "rows")},
                "response": {"usage": raw["response"].get("usage", {})},
                "response_sha256": ab.digest(raw["response"])})
    if len(runs) != 112:
        raise RuntimeError(f"Expected all 64+24+24 requests, found {len(runs)}")
    groups = []
    for name, split, arm in [("primary", s, a) for s in ("dev", "holdout") for a in ab.PROMPTS] + [
        ("extension", "extension", a) for a in ab.PROMPTS] + [("isolation", "extension", "focused")]:
        selected = [r for r in runs if (r["experiment"], r["split"], r["arm"]) == (name, split, arm)]
        groups.append({"experiment": name, "split": split, "arm": arm, **metrics(selected)})
    archive = Path(".e2e-artifacts/research-semantic-review-20261106-evidence.zip")
    # Include the interrupted two-call pilot as evidence, never in primary scores.
    pilot = Path("tests/manual/results/research-semantic-review-20261106")
    files.extend(pilot.rglob("*.json"))
    archive.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, str(path))
    manifest = [{"path": str(p), "sha256": sha(p), "bytes": p.stat().st_size} for p in files]
    ab.write(REPORT, {"date": "2026-11-06", "decision": "do_not_integrate_semantic_gate",
        "current_production_semantics": "cited_unreviewed, not entailed; structural success is not semantic approval",
        "scoring": {"bad": sorted(ab.BAD), "invalid_output": "all units invalid, never a detection; retained in denominator",
                    "gold": "human prelabelled fixtures, never supplied to tested model",
                    "recognition": "classification of supplied units only, NOT important-claim extraction recall",
                    "seeds": "repeated observations, not independent tasks"},
        "specifications": specs, "groups": groups, "runs": runs,
        "evidence_archive": {"path": str(archive), "sha256": sha(archive), "bytes": archive.stat().st_size},
        "manifest": manifest})
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
