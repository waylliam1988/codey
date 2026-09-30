"""Offline deterministic legacy/current parity gate; see docs/kernel_parity.zh-CN.md.

python tools/kernel_parity.py --legacy-root reference-projects/codey-pre-unified-958bcb4 --report parity.json
Baseline export is deliberately separate from checking: --export-baseline PATH.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests/support/kernel_parity_probe.py"
FIXTURE = ROOT / "tests/fixtures/kernel_parity/baseline.json"
DELTAS = ROOT / "tests/fixtures/kernel_parity/intentional_deltas.json"
BOUNDARIES = ROOT / "tests/fixtures/kernel_parity/boundaries.json"
LEGACY_COMMIT = "958bcb485bf05d0ae8232763681d1df5ecee1d34"


def probe(source, cases=None, *, legacy=False):
    with tempfile.TemporaryDirectory(prefix="codey-parity-home-") as td:
        env = {**os.environ, "HOME": td, "USERPROFILE": td, "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONIOENCODING": "utf-8", "NATIVE_TOOLS": "0"}
        if os.name == "nt":
            env["HOMEDRIVE"] = Path(td).drive
            env["HOMEPATH"] = str(Path(td))[len(Path(td).drive):]
        command = [sys.executable, "-I", str(PROBE), "--source", str(Path(source).resolve())]
        if legacy:
            command.append("--legacy")
        if cases is None:
            command.append("--inventory")
        result = subprocess.run(command, input=json.dumps(cases) if cases is not None else None,
                                cwd=td, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180,
                                check=False)
        if result.returncode:
            raise RuntimeError(f"probe failed ({source}):\n{result.stderr}\n{result.stdout[-1500:]}")
        return json.loads(result.stdout)


def load_baseline():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def surface_diff(before, after):
    """An enumerated review index, never a claim that declarations are behavior."""
    old, new = before["ast_surface"], after["ast_surface"]
    def names(surface):
        return {f"{path}:{row['kind']}:{row['name']}" for path, rows in surface.items() for row in rows}
    old_names, new_names = names(old), names(new)
    return {"legacy_modules": len(old), "current_modules": len(new),
            "removed_modules": sorted(set(old) - set(new)), "added_modules": sorted(set(new) - set(old)),
            "removed_declarations": sorted(old_names - new_names),
            "added_declarations": sorted(new_names - old_names)}


def compare(baseline, current, deltas):
    failures = []
    intentional = []
    expected = baseline["observations"]
    for case in baseline["cases"]:
        key = case["id"]
        before, after = expected[key], current.get(key)
        if key in deltas:
            delta = deltas[key]
            if not delta.get("reason") or delta["before"] != before or delta["after"] != after:
                failures.append({"id": key, "kind": "stale_delta", "before": before, "after": after})
            elif before == after:
                failures.append({"id": key, "kind": "obsolete_delta"})
            else:
                intentional.append({"id": key, "status": delta.get("status", "INTENTIONAL_CHANGE"),
                                    "reason": delta["reason"]})
        elif before != after:
            failures.append({"id": key, "kind": "unclassified_difference", "before": before, "after": after})
    for key in sorted(set(deltas) - set(expected)):
        failures.append({"id": key, "kind": "unknown_delta"})
    for key in sorted(set(current) ^ set(expected)):
        failures.append({"id": key, "kind": "missing_or_extra_case"})
    return {"legacy_commit": baseline["commit"], "case_count": len(expected),
            "parity_count": len(expected) - len(intentional) - len(failures),
            "intentional": intentional, "failures": failures}


def export_baseline(source, destination):
    sys.path.insert(0, str(ROOT))
    from tests.support.kernel_parity_cases import cases_for_inventory

    inv = probe(source, legacy=True)
    # Prove that the independent extraction really is the pinned Git revision.
    tree = subprocess.run(["git", "ls-tree", "-r", LEGACY_COMMIT, "codey"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout
    import hashlib
    paths = {line.split("\t", 1)[1] for line in tree.splitlines()
             if line.split("\t", 1)[1].endswith(".py")}
    if set(inv["source_hashes"]) != paths:
        raise RuntimeError("legacy source file set does not match the pinned Git tree")
    for line in tree.splitlines():
        path = line.split("\t", 1)[1]
        if not path.endswith(".py"):
            continue
        blob = subprocess.run(["git", "show", f"{LEGACY_COMMIT}:{path}"], cwd=ROOT,
                              capture_output=True, check=True).stdout.decode("utf-8").replace("\r\n", "\n")
        if inv["source_hashes"].get(path) != hashlib.sha256(blob.encode()).hexdigest():
            raise RuntimeError(f"legacy source does not match {LEGACY_COMMIT}: {path}")
    cases = cases_for_inventory(inv)
    payload = {"schema": 1, "commit": LEGACY_COMMIT, "inventory": inv, "cases": cases,
               "observations": probe(source, cases, legacy=True)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--export-baseline", type=Path)
    args = parser.parse_args()
    if args.export_baseline:
        if args.legacy_root is None:
            parser.error("--export-baseline requires --legacy-root")
        export_baseline(args.legacy_root, args.export_baseline)
        return 0
    baseline = load_baseline()
    if args.legacy_root:
        inv = probe(args.legacy_root, legacy=True)
        if inv != baseline["inventory"]:
            raise RuntimeError("legacy inventory/source fingerprint drift")
        observed = probe(args.legacy_root, baseline["cases"], legacy=True)
        if observed != baseline["observations"]:
            raise RuntimeError("legacy replay does not match frozen baseline")
    current = probe(ROOT, baseline["cases"])
    deltas = json.loads(DELTAS.read_text(encoding="utf-8")) if DELTAS.exists() else {}
    report = compare(baseline, current, deltas)
    report["surface"] = surface_diff(baseline["inventory"], probe(ROOT))
    report["boundaries"] = json.loads(BOUNDARIES.read_text(encoding="utf-8"))
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": report["case_count"], "parity": report["parity_count"],
                      "intentional": len(report["intentional"]), "failures": len(report["failures"])}))
    for row in report["failures"]:
        print(row["id"], row["kind"])
    return bool(report["failures"])


if __name__ == "__main__":
    raise SystemExit(main())
