"""Independently audit frozen requests and expose raw/validated tradeoffs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.manual import research_semantic_review_c_abc as abc
from tests.manual import research_semantic_review_c_prompts as prompts

OUTPUT = Path("docs/reports/research-semantic-review-c-audit-20261106.json")


def request_matches(run, case):
    payload = run["request"]
    assert payload["messages"][0] == {"role": "system", "content": prompts.PROMPTS[run["arm"]]}
    assert json.loads(payload["messages"][1]["content"]) == abc.model_input(case)
    for key, value in {"temperature": .3, "top_p": .9, "seed": run["seed"], "max_tokens": 1800}.items():
        assert payload[key] == value, key
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["stream"] is False
    assert run["backend_before"]["idle"] == 1
    assert run["backend_before"]["queue"] == 0
    assert run["backend_after"]["last_seed"] == run["seed"]


def tradeoffs(runs):
    rows = [u for r in runs for u in r["rows"] if u["gold"] != "not_claim"]
    return {
        "factual_units": len(rows),
        "raw_false_acceptances": sum(u["gold"] in abc.old.BAD and u["model_verdict"] == "entailed" for u in rows),
        "validated_false_acceptances": sum(u["gold"] in abc.old.BAD and u["predicted"] == "entailed" for u in rows),
        "guard_blocked_false_acceptances": sum(u["gold"] in abc.old.BAD and u["model_verdict"] == "entailed"
                                               and u["predicted"] == "invalid" for u in rows),
        "correct_verdict_but_invalid_anchor": sum(u["gold"] == u["model_verdict"] and u["predicted"] == "invalid" for u in rows),
        "unresolved_factual_units": sum(u["predicted"] == "invalid" for u in rows),
        "format_length_stops": sum(r["response"]["choices"][0]["finish_reason"] == "length" for r in runs),
    }


def main():
    spec = abc.freeze(abc.ROOT)
    cases = {c["id"]: c for c in spec["fixtures"]}
    paths = sorted((abc.ROOT / "runs").glob("*.json"))
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    expected = {(c["id"], seed, arm) for c in cases.values() for seed in abc.SEEDS[c["split"]] for arm in prompts.PROMPTS}
    actual = {(r["case"], r["seed"], r["arm"]) for r in runs}
    assert len(runs) == len(expected) == len(actual) and actual == expected
    model = json.loads((abc.ROOT / "backend.json").read_text(encoding="utf-8"))["model"]["result"]
    for r in runs:
        assert r["request"]["model"] == r["response"]["model"] == model
        request_matches(r, cases[r["case"]])
        interpreted = abc.interpret(cases[r["case"]], r["response"]["choices"][0]["message"]["content"])
        assert all(r[key] == value for key, value in interpreted.items())
    report = json.loads(abc.REPORT.read_text(encoding="utf-8"))
    for item in report["manifest"] + [report["archive"]]:
        assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
    selected = [r for r in runs if r["split"] != "dev"]
    summary = [{"arm": arm, "raw": abc.scored([r for r in selected if r["arm"] == arm], raw=True),
                "validated": abc.scored([r for r in selected if r["arm"] == arm]),
                "tradeoffs": tradeoffs([r for r in selected if r["arm"] == arm])} for arm in prompts.PROMPTS]
    targets = []
    for category in ("necessity", "injection", "binding", "inference"):
        for arm in prompts.PROMPTS:
            rr = [r for r in selected if r["category"] == category and r["arm"] == arm]
            targets.append({"category": category, "arm": arm, "raw": abc.scored(rr, raw=True), "validated": abc.scored(rr)})
    abc.old.write(OUTPUT, {"requests_verified": len(runs), "fresh_requests": len(selected), "groups": summary,
        "targets": targets, "paired_raw": abc.paired(selected, raw=True), "paired_validated": abc.paired(selected),
        "production_unchanged": abc.production_identity() == spec["identity"]["production"],
        "checks": ["complete expected matrix", "same model inputs and contract", "same sampling and seeds",
                   "no gold in requests", "idle before inference", "response replay matches recorded score",
                   "all raw/archive hashes", "all tracked production files unchanged"]})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
