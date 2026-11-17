#!/usr/bin/env python3
"""One-command local development simulation. Never connects to a real service."""
from __future__ import annotations

import argparse
import json
import platform
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or later is required. The project target is Python 3.11.")

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from purevoting_rules import __version__
    from purevoting_rules.engine import evaluate_all
    from purevoting_rules.findings import build_finding, validate_submission
    from purevoting_rules.fixtures import development_cases
    from purevoting_rules.paths import reference_root
    from purevoting_rules.publication import LocalPublicationSimulator
    from purevoting_rules.reporting import write_reports
    from purevoting_rules.scheduler import run_decision
    from purevoting_rules.signing import EIP191Signer
    from purevoting_rules.storage import FindingStore
    from purevoting_rules.vector_check import verify_vectors
except ModuleNotFoundError as exc:
    raise SystemExit(f"Missing dependency or module: {exc.name}. Install rules/requirements-lock.txt first.") from None

UPSTREAM = "95793b32e20e9253c4f94a9bdc51dcb5c31c80f4"


def scheduler_examples() -> list[dict]:
    cases = [
        ("Checkpoint not accepted", dict(checkpoint_accepted=False,gateway_available=True,all_anchored=False,elapsed_seconds=100)),
        ("Gateway unavailable before evidence read", dict(checkpoint_accepted=True,gateway_available=False,all_anchored=False,elapsed_seconds=200)),
        ("Unanchored at 179 seconds", dict(checkpoint_accepted=True,gateway_available=True,all_anchored=False,elapsed_seconds=179)),
        ("All events anchored", dict(checkpoint_accepted=True,gateway_available=True,all_anchored=True,elapsed_seconds=1)),
        ("Unanchored at 180 second deadline", dict(checkpoint_accepted=True,gateway_available=True,all_anchored=False,elapsed_seconds=180)),
    ]
    return [{"name":name,"inputs":inputs,"decision":run_decision(**inputs)} for name,inputs in cases]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "simulation_output", help="Parent output directory; each run gets a new subdirectory")
    parser.add_argument("--seeds", type=int, nargs="+", default=[1,2,3])
    args = parser.parse_args(argv)
    if any(s < 0 for s in args.seeds) or len(set(args.seeds)) != len(args.seeds):
        parser.error("Seeds must be distinct nonnegative integers")
    start = datetime.now(timezone.utc)
    run_id = start.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    output = args.output.resolve() / run_id
    output.mkdir(parents=True, exist_ok=False)
    vectors = verify_vectors(reference_root() / "tests/vectors/hash-vectors.json")
    if not all(v["passed"] for v in vectors):
        (output / "failed_vectors.json").write_text(json.dumps(vectors,indent=2),encoding="utf-8")
        raise SystemExit("Hash vector conformance failed. Simulation stopped.")
    print(f"Independent hash vector checks: {len(vectors)}/{len(vectors)} matched")
    print("Mode: synthetic development fixtures; local Core/Gateway mocks; AI disabled")
    print("Coverage: R1-R5 implemented; these older fixtures omit independent chain evidence")
    signer = EIP191Signer()
    result = {
        "version":__version__,"upstream_commit":UPSTREAM,"contracts_version":"1.2.2",
        "run_id":run_id,"created_at":start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance":"synthetic_development", "formal_scenarios_run":False,
        "independent_ground_truth":False,"live_services_used":False,"ai_model_used":False,
        "runtime":{"python":platform.python_version(),"platform":platform.platform()},
        "seeds":args.seeds,"hash_vectors":[{"name":v["name"],"passed":v["passed"]} for v in vectors],
        "scheduler_examples":scheduler_examples(),"cases":[],"findings_stored":0,
    }
    mismatches = []
    for seed in args.seeds:
        for case in development_cases(seed):
            case_id = case["case_id"]
            if not case_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in case_id):
                raise ValueError("Unsafe development case identifier")
            case_dir = output / f"seed_{seed}" / case_id
            case_dir.mkdir(parents=True)
            snapshot = case["snapshot"]
            outcomes = evaluate_all(snapshot)
            store = FindingStore(case_dir / "findings.sqlite3")
            bodies = []
            normalized = []
            try:
                for outcome in outcomes:
                    body = build_finding(outcome,snapshot)
                    validate_submission(body)
                    fid = store.enqueue(body)
                    bodies.append(store.get(fid)["body"])
                    item = asdict(outcome)
                    item["matches_expected"] = outcome.result == case["expected"][outcome.rule_id]
                    normalized.append(item)
                    if not item["matches_expected"]:
                        mismatches.append({"seed":seed,"case_id":case_id,"rule_id":outcome.rule_id,
                                           "expected":case["expected"][outcome.rule_id],"actual":outcome.result})
                publication_reports = []
                with LocalPublicationSimulator(store,signer,transient_body_failures=1) as publication:
                    for _ in range(3):
                        publication_reports.append(publication.publish_all())
                        if not store.pending():
                            break
                pending_count = len(store.pending())
                if pending_count:
                    raise RuntimeError(f"{case_id}: {pending_count} local publication records remain pending")
                result["findings_stored"] += len(bodies)
                (case_dir / "findings.json").write_text(json.dumps(bodies,indent=2) + "\n",encoding="utf-8")
                (case_dir / "publication.json").write_text(json.dumps(publication_reports,indent=2) + "\n",encoding="utf-8")
                result["cases"].append({"seed":seed,"case_id":case_id,"description":case["description"],
                                        "expected":case["expected"],"outcomes":normalized,
                                        "finding_count":len(bodies),"publication_reports":publication_reports})
                states = " ".join(f"{o.rule_id}={o.result}" for o in outcomes)
                status = "MATCH" if all(o["matches_expected"] for o in normalized) else "MISMATCH"
                print(f"seed={seed} {case_id}: {states} [{status}]")
            finally:
                store.close()
    result["mismatches"] = mismatches
    write_reports(output,result)
    print(f"Report: {output / 'report.html'}")
    print(f"Actual findings stored in local mocks: {result['findings_stored']}")
    print("Official integration scenarios: NOT RUN. This fixture run does not certify deployment readiness.")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
