#!/usr/bin/env python3
"""Regression harness for /v1/validate.

Three iterations of this service were validated by one person reading output by
hand. That does not scale and it does not survive the next clever idea, so the
cases live in golden.json and run on every deploy.

  python3 harness.py            run and compare against the stored baseline
  python3 harness.py --baseline run and store the result as the new baseline

Metrics: recall@10 over the papers a case says must be found, verdict validity,
scope decisions, and coverage floors. A case fails loudly; a metric that merely
drifts shows up in the diff against the baseline.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
GOLDEN = HERE / "golden.json"
BASELINE = HERE / "harness_baseline.json"
API = "http://localhost:8010/v1/validate"
KEY = list(json.load(open("/opt/dtox-research-api/keys.json")).keys())[0]


def validate(case):
    claim = ({"claim": case["claim"], "phrasings": case["phrasings"]}
             if case.get("phrasings") else case["claim"])
    body = {"idea": case["idea"], "claims": [claim], "evidence_per_claim": 10}
    if case.get("layer"):
        body["layer"] = case["layer"]
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-API-Key": KEY})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


def adjudicate(setup):
    """File a verdict before the case runs, so the registry has something to say."""
    body = {"claim": setup["claim"], "judgments": setup["judgments"]}
    if setup.get("layer"):
        body["layer"] = setup["layer"]
    req = urllib.request.Request("http://localhost:8010/v1/adjudicate",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-API-Key": KEY})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def run_case(case):
    """Returns (result dict, list of failure strings)."""
    started = time.time()
    if case.get("setup_judgment"):
        try:
            adjudicate(case["setup_judgment"])
        except (urllib.error.URLError, TimeoutError) as e:
            return ({"id": case["id"], "error": str(e), "seconds": 0},
                    [f"{case['id']}: setup failed: {e}"])
    try:
        data = validate(case)
    except (urllib.error.URLError, TimeoutError) as e:
        return ({"id": case["id"], "error": str(e), "seconds": 0},
                [f"{case['id']}: request failed: {e}"])

    fails = []
    in_scope = data.get("in_scope", True)
    result = {"id": case["id"], "in_scope": in_scope,
              "seconds": round(time.time() - started, 1)}

    if "expect_in_scope" in case and not case["expect_in_scope"]:
        # Two mechanisms satisfy the same intent: the entry gate can refuse the
        # idea outright, or the claim report can carry out_of_scope_signal. The
        # case exists to make sure the tool never presents an off-subject idea
        # as an audited one, and either one does that. Checking only the gate made
        # the suite fail the moment the corpus grew past its threshold.
        signal = None
        if in_scope:
            claim0 = (data.get("claims") or [{}])[0]
            signal = claim0.get("out_of_scope_signal")
            result["verdict"] = claim0.get("verdict")
            result["out_of_scope_signal"] = bool(signal)
        if not in_scope or signal:
            return result, fails
        fails.append(f"{case['id']}: audited as in scope with no out_of_scope_signal")
        return result, fails
    if "expect_in_scope" in case and in_scope != case["expect_in_scope"]:
        fails.append(f"{case['id']}: in_scope={in_scope}, expected {case['expect_in_scope']}")

    if not in_scope:
        fails.append(f"{case['id']}: refused as out of scope, but the case expects an audit")
        return result, fails

    claim = data["claims"][0]
    evidence = claim.get("evidence", [])
    weaker = claim.get("weaker_matches", [])
    found_ids = [e["id"] for e in evidence] + [w["id"] for w in weaker]
    direct_ids = {e["id"] for e in evidence if e.get("band") == "direct"}

    result.update({
        "verdict": claim["verdict"],
        "coverage": claim["corpus_coverage"]["papers_about_this_claim"],
        "depth": claim["corpus_coverage"]["depth"],
        "evidence": len(evidence),
        "top": [e["id"] for e in evidence[:3]],
    })

    must = case.get("must_find", [])
    hit = [pid for pid in must if pid in found_ids[:10]]
    if must:
        result["recall_at_10"] = round(len(hit) / len(must), 2)
        for pid in must:
            if pid not in found_ids[:10]:
                fails.append(f"{case['id']}: {pid} not in top 10")

    for pid in case.get("must_not_be_direct", []):
        if pid in direct_ids:
            fails.append(f"{case['id']}: {pid} ranked as direct evidence")

    if case.get("expect_verdict") and claim["verdict"] not in case["expect_verdict"]:
        fails.append(f"{case['id']}: verdict {claim['verdict']} not in {case['expect_verdict']}")
    if claim["verdict"] in case.get("must_not_verdict", []):
        fails.append(f"{case['id']}: verdict {claim['verdict']} is forbidden here")
    if case.get("min_coverage") and (result["coverage"] or 0) < case["min_coverage"]:
        fails.append(f"{case['id']}: coverage {result['coverage']} below {case['min_coverage']}")

    # the invariant the whole registry exists for: a verdict filed under one
    # wording has to answer the same question asked in another
    expect_registry = case.get("expect_in_registry", [])
    if expect_registry:
        on_record = {r["id"] for r in claim.get("registry", [])}
        result["registry_ids"] = sorted(on_record)
        for pid in expect_registry:
            if pid not in on_record:
                fails.append(f"{case['id']}: {pid} missing from the registry answer "
                             f"(a verdict filed under another wording was lost)")
        in_evidence = {e["id"] for e in evidence if e.get("from_registry")}
        for pid in expect_registry:
            if pid in on_record and pid not in in_evidence:
                fails.append(f"{case['id']}: {pid} is on the record but absent from "
                             f"the evidence, so the reader cannot see what the "
                             f"verdict rests on")

    return result, fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="store this run as the baseline")
    ap.add_argument("--only", help="run one case by id")
    args = ap.parse_args()

    golden = json.load(open(GOLDEN))
    cases = [c for c in golden["cases"] if not args.only or c["id"] == args.only]

    results, failures = [], []
    for i, case in enumerate(cases):
        if i:
            time.sleep(8.0)  # validate is capped at 8/min now; the suite must not trip its own limiter
        result, fails = run_case(case)
        results.append(result)
        failures.extend(fails)
        mark = "FAIL" if fails else "ok  "
        print(f"{mark} {case['id']:32} {result.get('verdict', 'refused'):28} "
              f"cov={str(result.get('coverage', '-')):>4} "
              f"recall={result.get('recall_at_10', '-')} {result['seconds']}s")

    audited = [c["id"] for c in cases if c.get("verified_by") == "audited"]
    recalls = [r["recall_at_10"] for r in results if "recall_at_10" in r]
    summary = {
        "cases": len(cases),
        "audited": len(audited),
        "failures": len(failures),
        "mean_recall_at_10": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "results": results,
    }
    print(f"\n{len(cases)} cases, {len(failures)} failures, "
          f"mean recall@10 {summary['mean_recall_at_10']}")
    for f in failures:
        print("   ", f)

    if args.baseline:
        BASELINE.write_text(json.dumps(summary, indent=2))
        print(f"baseline written to {BASELINE}")
    elif BASELINE.exists():
        base = json.load(open(BASELINE))
        by_id = {r["id"]: r for r in base["results"]}
        drift = []
        for r in results:
            b = by_id.get(r["id"])
            if not b:
                continue
            if b.get("verdict") != r.get("verdict"):
                drift.append(f"{r['id']}: verdict {b.get('verdict')} -> {r.get('verdict')}")
            if b.get("recall_at_10") != r.get("recall_at_10"):
                drift.append(f"{r['id']}: recall {b.get('recall_at_10')} -> {r.get('recall_at_10')}")
        if drift:
            print("\ndrift against baseline:")
            for d in drift:
                print("   ", d)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
