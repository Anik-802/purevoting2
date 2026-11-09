"""T01 - normal vote (docs/test-plan.md): redeem -> ballot -> receipt -> ANCHORED; R1-R5 PASS at the next checkpoint;
ELECTION_OPENED never REJECTED. Drives the running stack over Core's HTTP API only, as the admin web and the voter
web do, then judges the findings against the ground-truth label in this folder (expected.json, written by Harits
before the first run; without it the run is recorded but not judged). How to run: README.md in this folder.

Judged checkpoint C: the checkpoint that commits the last ballot (Core's open checkpoint right after it; if Core
committed one in between, C is one later, never earlier). Each rule's result at C follows contracts/README.md
"Review and supersession": a FAIL at C stands; otherwise a PASS at C decides; an INCONCLUSIVE at C counts only until a
later PASS of the rule (at C or higher) supersedes it, except that one with reason OTHER whose summary lists checks
"not implemented" (rules-ai.md section 8) decides at once and makes the verdict "T01 (partial ...)". A FAIL at any other checkpoint is
also a difference from an all-PASS label (FAIL is never superseded).

Exit code 0 = as labelled, 1 = differs from the label, 2 = not judged (no label, a partly implemented rule, or not run
to the end). Tokens, credentials and receipt hashes are never printed or recorded.
"""
import argparse
import base64
import hashlib
import json
import os
import random
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import nacl.public

HERE = Path(__file__).resolve().parent
RULES = ("R1", "R2", "R3", "R4", "R5")
ORGS = ["org_commission", "org_neutral", "org_observer"]   # ascending by org_id (createElection orgIds order)
# system-design 10.2 (evaluation, demo); quick = development runs of a few minutes (D-10 still holds)
PRESETS = {
    "evaluation": {"voters": 100, "bucket": 900, "buckets": 8, "votes": 3},
    "demo": {"voters": 100, "bucket": 60, "buckets": 10, "votes": 3},
    "quick": {"voters": 20, "bucket": 30, "buckets": 2, "votes": 3},
}
# rules-ai.md section 8: a partly implemented rule lists its unimplemented checks in `summary`
PARTIAL = re.compile(r"not (yet )?implemented|unimplemented", re.IGNORECASE)


def partial(f: dict) -> bool:
    """INCONCLUSIVE only for unimplemented checks: reason OTHER (the Rule Engine re-runs the checkpoint for the other
    reasons, and adds the "not implemented" list to every summary of a partly implemented rule)."""
    return (f.get("result") == "INCONCLUSIVE" and f.get("inconclusive_reason") == "OTHER"
            and bool(PARTIAL.search(f.get("summary") or "")))


class Stop(Exception):
    """The run cannot go on (verdict NOT COMPLETED)."""


class Failed(Stop):
    """The run shows a T01 failure before the findings (verdict FAIL)."""


def H(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def iso(t: float) -> str:
    return datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def expect(r: httpx.Response, *codes: int) -> httpx.Response:
    if r.status_code not in codes:   # Core's problem bodies carry a code and a reason, never the request's secrets
        raise Stop(f"{r.request.method} {r.request.url.path.split('/v1/')[-1][:40]}: HTTP {r.status_code} "
                   f"{r.text[:200]}")
    return r


def poll(fn, timeout: float, why: str):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(2)
    raise Stop(f"{why}: not seen within {int(timeout)} s")


def commit() -> str | None:
    """HEAD, with "-dirty" when the working tree has uncommitted changes (the run is then not reproducible)."""
    def git(*args):
        return subprocess.run(["git", "-C", str(HERE), *args], capture_output=True, text=True, timeout=5,
                              check=True).stdout.strip()
    try:
        return git("rev-parse", "HEAD") + ("-dirty" if git("status", "--porcelain") else "")
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("T01_COMMIT")


def at_checkpoint(items: list[dict], rule: str, k: int) -> tuple[str, dict] | None:
    """(result, deciding finding) of `rule` at checkpoint k, or None while undecided (supersession rule above)."""
    mine = [f for f in items if f["rule_id"] == rule]
    here = [f for f in mine if f["checkpoint"] == k]
    for want in ("FAIL", "PASS"):
        hit = [f for f in here if f["result"] == want]
        if hit:
            return want, max(hit, key=lambda f: f["finding_id"])
    inconclusive = [f for f in here if f["result"] == "INCONCLUSIVE"]
    if inconclusive:
        later_pass = [f for f in mine if f["result"] == "PASS" and f["checkpoint"] > k]
        if later_pass:
            return "PASS", min(later_pass, key=lambda f: (f["checkpoint"], f["finding_id"]))
        # a partly implemented rule (rules-ai.md section 8) gives no later PASS: decided, reported as partial
        unimplemented = [f for f in inconclusive if partial(f)]
        if unimplemented:
            return "INCONCLUSIVE", max(unimplemented, key=lambda f: f["finding_id"])
    return None


def judge(label: dict, decided: dict, items: list[dict], k: int) -> list[str]:
    """Differences from the label (empty = as labelled)."""
    out = []
    for rule, want in label["rules"].items():
        got = decided.get(rule)
        if got is None:
            out.append(f"{rule}: no decided finding at checkpoint {k} (label {want})")
            continue
        result, f = got
        if partial(f):
            continue   # partly implemented: in rec["partial"], verdict "T01 (partial ...)", not compared
        if result != want:
            out.append(f"{rule}: {result} at checkpoint {k} (label {want})")
        nonzero = sorted(n for n, v in (f.get("observed") or {}).items() if isinstance(v, (int, float)) and v)
        expected = sorted(label.get("observed_nonzero", {}).get(rule, []))
        if nonzero != expected:
            out.append(f"{rule}: non-zero observed {nonzero} (label {expected})")
    for f in items:
        if f["result"] == "FAIL" and f["checkpoint"] != k and label["rules"].get(f["rule_id"]) != "FAIL":
            out.append(f"{f['rule_id']}: FAIL at checkpoint {f['checkpoint']} (never superseded)")
    return out


def run(a, rec: dict) -> None:
    c = httpx.Client(base_url=a.core, timeout=15)
    B, window, voters, votes = a.bucket, a.bucket * a.buckets, a.voters, a.votes
    rng = random.Random(a.seed)

    expect(c.get("/readyz"), 200)
    user, password = os.environ.get("T01_USERNAME"), os.environ.get("T01_PASSWORD")
    if not (user and password):
        raise Stop("set T01_USERNAME and T01_PASSWORD (the staff-setup admin of org_commission)")
    tok = expect(c.post("/v1/auth/login", json={"username": user, "password": password}), 200).json()
    auth = {"Authorization": f"Bearer {tok['access_token']}"}

    def staff_election():
        return expect(c.get(f"/v1/elections/{eid}", headers=auth), 200).json()

    start = (int(time.time()) + a.lead) // B * B + B
    end = start + window
    e = expect(c.post("/v1/elections", headers=auth, json={
        "title": f"T01 seed {a.seed}", "window_start": iso(start), "window_end": iso(end),
        "time_bucket_seconds": B, "checkpoint_interval_seconds": B,
        "candidates": [{"name": "A"}, {"name": "B"}], "org_ids": ORGS, "approval_threshold": 2}), 201).json()
    eid = rec["election_id"] = e["election_id"]
    log(f"election {eid}: window {iso(start)} - {iso(end)}")
    expect(c.put(f"/v1/elections/{eid}/roster", headers=auth,
                 json={"voters": [{"external_ref": f"T01-{i:03d}", "name": f"voter {i}"} for i in range(voters)]}), 200)

    t = time.time()
    poll(lambda: staff_election()["creation_check"] == "PASSED", a.timeout, "creation check PASSED")
    rec["timings"]["creation_check_s"] = round(time.time() - t, 1)
    log(f"creation check PASSED after {rec['timings']['creation_check_s']} s")
    expect(c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth), 202)
    r = expect(c.post(f"/v1/elections/{eid}/tokens:issue", json={"delivery": "CSV"},
                      headers={**auth, "Idempotency-Key": "t01-" + secrets.token_hex(6)}), 200)
    tokens = [ln.rsplit("#t=", 1)[1] for ln in r.text.strip().splitlines()[1:]]
    if len(tokens) != voters:
        raise Stop(f"{len(tokens)} links for {voters} voters")
    if time.time() >= start:
        raise Stop(f"setup ran past window_start; raise --lead (now {a.lead} s)")

    log("waiting for the window to open")

    def opened():
        if staff_election().get("transition", {}).get("ledger_status") == "REJECTED":
            raise Failed("the opening transition (ELECTION_OPENED) was REJECTED")
        return (x := c.get(f"/v1/public/elections/{eid}").json())["status"] == "OPEN" and x
    pub = poll(opened, start - time.time() + a.timeout, "status OPEN")
    rec["timings"]["open_after_window_start_s"] = round(time.time() - start, 1)
    log(f"OPEN {rec['timings']['open_after_window_start_s']} s after window_start")

    pk = nacl.public.PublicKey(base64.b64decode(pub["public_key"]))
    receipts = []
    for i in rng.sample(range(voters), votes):          # voter order and choice vary by seed (test-plan protocol)
        cand = rng.choice(pub["candidates"])["candidate_id"]
        credential = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
        expect(c.post("/v1/credentials:redeem",
                      json={"election_id": eid, "token": tokens[i], "nullifier_hash": H(credential)}), 200)
        sealed = nacl.public.SealedBox(pk).encrypt(json.dumps({"candidate_id": cand}).encode())
        b = expect(c.post("/v1/ballots", json={"election_id": eid, "credential": credential,
                                               "sealed_ballot": base64.b64encode(sealed).decode()}), 201).json()
        receipts.append(b["receipt_hash"])
    k = rec["judged_checkpoint"] = staff_election()["open_checkpoint"]   # commits the last ballot (or one later)
    rec["checks"]["ballots_201"] = len(receipts)
    log(f"{len(receipts)} ballots: 201 + receipt; judged checkpoint {k}")

    t = time.time()
    for h in receipts:
        st = poll(lambda: (s := c.get(f"/v1/public/receipts/{h}").json()["status"]) != "RECEIVED" and s,
                  a.timeout, "receipt status ANCHORED")
        if st != "ANCHORED":
            raise Failed(f"receipt status {st}")
    rec["checks"]["receipts_anchored"] = len(receipts)
    rec["timings"]["anchored_s"] = round(time.time() - t, 1)
    log(f"every receipt ANCHORED ({rec['timings']['anchored_s']} s)")

    poll(lambda: staff_election()["counters"]["as_of_checkpoint"] >= k, B + a.timeout, f"checkpoint {k} committed")
    log(f"checkpoint {k} committed; waiting for R1-R5 findings at it")
    t = time.time()
    state = {}

    def decided_all():
        state["items"] = expect(c.get(f"/v1/elections/{eid}/findings", headers=auth), 200).json()["items"]
        state["decided"] = {r: d for r in RULES if (d := at_checkpoint(state["items"], r, k))}
        return len(state["decided"]) == len(RULES)
    try:
        poll(decided_all, a.findings_timeout, f"a decided finding of every rule R1-R5 at checkpoint {k}")
    except Stop as exc:
        state.setdefault("decided", {})
        rec["problems"].append(str(exc))
    rec["timings"]["findings_s"] = round(time.time() - t, 1)
    rec["findings"] = [{n: f.get(n) for n in ("finding_id", "rule_id", "checkpoint", "result", "observed", "summary",
                                              "inconclusive_reason")} for f in state.get("items", [])]
    rec["decided"] = {r: {"result": res, "finding_id": f["finding_id"]} for r, (res, f) in state["decided"].items()}
    rec["partial"] = sorted(r for r, (_, f) in state["decided"].items() if partial(f) or
                            (f.get("result") != "INCONCLUSIVE" and PARTIAL.search(f.get("summary") or "")))
    for r in RULES:
        res, f = state["decided"].get(r, ("UNDECIDED", {}))
        log(f"{r} {res} at checkpoint {k}: {(f.get('summary') or '')[:120]}")

    alerts = expect(c.get(f"/v1/elections/{eid}/audit-alerts", headers=auth), 200).json()["items"]
    rec["alerts"] = [{n: x.get(n) for n in ("checkpoint", "kind", "source", "detail")} for x in alerts]
    rec["checks"]["transition_rejected"] = sum(x["kind"] == "TRANSITION_REJECTED" for x in alerts)
    if rec["checks"]["transition_rejected"]:
        rec["problems"].append("a transition was REJECTED (TRANSITION_REJECTED alert)")
    for x in alerts:
        log(f"alert {x['kind']} ({x['source']}, checkpoint {x['checkpoint']}): {(x.get('detail') or '')[:120]}")


def main(argv=None) -> int:
    try:   # a summary or alert detail with characters the console cannot show must not stop the run record
        sys.stdout.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="T01 normal vote against the running stack")
    p.add_argument("--core", default=os.environ.get("CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--config", choices=sorted(PRESETS), default="demo",
                   help="preset; changing a value below records the configuration as custom")
    p.add_argument("--bucket", type=int, help="time bucket = checkpoint interval (s)")
    p.add_argument("--buckets", type=int, help="voting window in buckets")
    p.add_argument("--voters", type=int, help="D-10: voters x bucket >= 10 x window")
    p.add_argument("--votes", type=int)
    p.add_argument("--lead", type=int, default=180, help="seconds before window_start for creation check and setup")
    p.add_argument("--timeout", type=int, default=300, help="per ledger step (s)")
    p.add_argument("--findings-timeout", type=int, default=900)
    p.add_argument("--out", type=Path, default=Path("t01-runs"))
    a = p.parse_args(argv)
    config = a.config
    for name, value in PRESETS[a.config].items():
        if getattr(a, name) is None:
            setattr(a, name, value)
        elif getattr(a, name) != value:
            config = "custom"
    if a.voters < 10 * a.buckets or not 0 < a.votes <= a.voters:
        p.error("need voters x bucket >= 10 x window (D-10) and 0 < votes <= voters")

    started = time.time()
    rec = {"scenario": "T01", "commit": commit(), "seed": a.seed, "config": config,
           "parameters": {n: getattr(a, n) for n in ("bucket", "buckets", "voters", "votes", "lead")},
           "started_at": iso(started), "timings": {}, "checks": {}, "problems": []}
    label_path = HERE / "expected.json"
    label = json.loads(label_path.read_text(encoding="utf-8")) if label_path.exists() else None
    code = 2
    try:
        run(a, rec)
        if label is not None:
            items = [f for f in rec["findings"] if f["rule_id"] in RULES]
            decided = {r: (d["result"], next(f for f in items if f["finding_id"] == d["finding_id"]))
                       for r, d in rec["decided"].items()}
            rec["problems"] += judge(label, decided, items, rec["judged_checkpoint"])
        if label is None:
            rec["verdict"] = "NOT JUDGED (no expected.json)"
        elif rec["problems"]:
            rec["verdict"], code = "FAIL", 1
        elif rec["partial"]:
            rec["verdict"] = f"T01 (partial {'/'.join(rec['partial'])})"   # not a T01 pass (rules-ai.md section 8)
        else:
            rec["verdict"], code = "PASS", 0
    except Failed as exc:
        rec["verdict"], code = "FAIL", 1
        rec["problems"].append(str(exc))
    except Exception as exc:   # noqa: BLE001 — whatever stopped the run, keep its record
        rec["verdict"] = "NOT COMPLETED"
        rec["problems"].append(str(exc) if isinstance(exc, (Stop, httpx.HTTPError)) else f"{type(exc).__name__}: {exc}")
    rec["finished_at"] = iso(time.time())

    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / f"T01-seed{a.seed}-{datetime.fromtimestamp(started).strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(("" if rec["verdict"].startswith("T01") else "T01 ") + rec["verdict"] + "".join(f"\n  - {x}" for x in rec["problems"]))
    log(f"run record: {path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
