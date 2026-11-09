"""End-to-end check of finding records against a running Gateway (plays the Rule Engine).

  python3 -I ledger/tests/test_findings_e2e.py --env .env.gw [--gateway http://127.0.0.1:8100]

Needs the same setup as test_gateway_e2e.py (network up, registry set up, Gateway running with the same env file) and
RULE_ENGINE_SIGNER_KEY and RULE_KEY in the env file. It creates its own election up to checkpoint 0, then records
findings signed with the Rule Engine key and checks refusals (wrong key, wrong role, future checkpoint, conflict),
idempotency, the FINDING_RECORDED event (source gateway, own seq), its anchor and the contract status. About 40 s.
"""
import argparse
import hashlib
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import ethsign  # noqa: E402
import pv_hash  # noqa: E402
import test_gateway_e2e as t  # noqa: E402  (helpers: Gw, event, until_final, check, rid, iso)


def idh(text):
    return hashlib.sha256(text.encode()).digest()


def u(n):
    return int(n).to_bytes(32, "big")


RULES = {"R1": 1, "R2": 2, "R3": 3, "R4": 4, "R5": 5}
RESULTS = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}


def finding_body(priv, election, finding_id, rule, result, checkpoint, fhash):
    """hashing.md: sha256(tag ‖ idh(finding) ‖ idh(election) ‖ u(rule) ‖ u(result) ‖ u(checkpoint) ‖ h32(hash)), eip191."""
    tag = b"PV2-FINDING-v1".ljust(32, b"\0")
    digest = hashlib.sha256(tag + idh(finding_id) + idh(election) + u(RULES[rule]) + u(RESULTS[result])
                            + u(checkpoint) + bytes.fromhex(fhash)).digest()
    sig = ethsign.personal_sign_digest(priv, digest)
    return {"election_id": election, "finding_id": finding_id, "rule_id": rule, "result": result,
            "finding_checkpoint": checkpoint, "finding_hash": fhash,
            "signature": {"scheme": "eip191-secp256k1", "signer": ethsign.address(priv).lower(), "value": "0x" + sig.hex()}}


def post_until(gw, body, key, budget=60):
    """POST /v1/finding-records, repeating the same request while the answer is 503 (not confirmed yet)."""
    end = time.time() + budget
    while True:
        st, out = gw.req("POST", "/v1/finding-records", body, key)
        if st != 503 or time.time() > end:
            return st, out
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--gateway", default="http://127.0.0.1:8100")
    a = ap.parse_args()
    env = {}
    for ln in open(a.env, encoding="utf-8").read().splitlines():
        m = re.match(r"^([A-Z0-9_]+)=(\S*)", ln)
        if m:
            env[m.group(1)] = m.group(2)
    gw = t.Gw(a.gateway, env["CORE_KEY"])
    rule_key = env["RULE_KEY"]
    priv = int(env["RULE_ENGINE_SIGNER_KEY"], 16)
    check = t.check

    # ---- an election with checkpoint 0 committed (as Core would do), never opened
    st, orgs = gw.get("/v1/organizations")
    names = sorted(o["org_id"] for o in orgs["items"])
    el, bucket, now = t.rid("elc_"), 5, int(time.time())
    ws = (now + 3600) // bucket * bucket
    created = {"config_hash": pv_hash.H("config-" + el), "window_start": t.iso(ws), "window_end": t.iso(ws + 60),
               "time_bucket_seconds": bucket, "checkpoint_interval_seconds": 10, "candidate_count": 2,
               "organizations": [{"org_id": o, "signer": "0x" + "00" * 20} for o in names], "approval_threshold": 2,
               "public_key_hash": pv_hash.H("pk"), "proposal_ttl_hours": 24, "operator_org": "org_commission"}
    roster = pv_hash.set_root([pv_hash.H("voter-%d" % i) for i in range(3)])
    troot = pv_hash.set_root([pv_hash.H("token-%d" % i) for i in range(3)])
    tables = [{"table": "t%d" % i, "row_count": 0, "snapshot_hash": pv_hash.H("t%d" % i)} for i in range(6)]
    steps = [("ELECTION_CREATED", created), ("ROSTER_FROZEN", {"roster_hash": roster, "voter_count": 3}),
             ("TOKEN_BATCH_ISSUED", {"batch_no": 1, "token_count": 3, "token_set_root": troot}),
             ("CHECKPOINT_COMMITTED", {"checkpoint": 0, "reason": "STATE_CHANGE", "tables": tables, "counts": {},
                                       "voter_events": {}})]
    for seq, (typ, payload) in enumerate(steps, 1):
        r = t.until_final(gw, t.event(el, typ, seq, 0, payload, int(time.time())))
        check(f"setup: {typ} accepted", r["status"] in ("ACCEPTED", "DUPLICATE"), r)
    print("election", el)

    fhash = pv_hash.H("finding-body-1")
    fid = t.rid("fnd_")
    good = finding_body(priv, el, fid, "R1", "FAIL", 0, fhash)

    # ---- refusals
    st, out = gw.req("POST", "/v1/finding-records", good, env["CORE_KEY"])
    check("the Core key may not record findings (403)", st == 403, (st, out))
    st, out = gw.req("POST", "/v1/finding-records", dict(good, result="MAYBE"), rule_key)
    check("malformed body is 400", st == 400, (st, out))
    st, out = gw.req("POST", "/v1/finding-records", finding_body(12345, el, fid, "R1", "FAIL", 0, fhash), rule_key)
    check("a signature from another key is 422 SIGNATURE_INVALID", st == 422 and out.get("code") == "SIGNATURE_INVALID", (st, out))
    st, out = gw.req("POST", "/v1/finding-records", finding_body(priv, el, t.rid("fnd_"), "R1", "FAIL", 7, fhash), rule_key)
    check("a checkpoint not committed yet is 422 CHECKPOINT_NOT_FOUND", st == 422 and out.get("code") == "CHECKPOINT_NOT_FOUND", (st, out))

    # ---- recording and idempotency
    t0 = time.time()
    st, out = post_until(gw, good, rule_key)
    check("a signed FAIL finding is recorded (201, or 200 when the first answer was 503)", st in (200, 201) and re.fullmatch(r"evt_[0-9A-HJKMNP-TV-Z]{26}", out.get("event_id", "")), (st, out))
    print(f"      (took {time.time() - t0:.1f} s)")
    first_event = out.get("event_id")
    st, out = gw.req("POST", "/v1/finding-records", good, rule_key)
    check("the identical request again is 200 with the same event_id", st == 200 and out.get("event_id") == first_event, (st, out))
    other = finding_body(priv, el, fid, "R1", "FAIL", 0, pv_hash.H("finding-body-2"))
    st, out = gw.req("POST", "/v1/finding-records", other, rule_key)
    check("the same finding_id with another hash is 409", st == 409, (st, out))
    fid2 = t.rid("fnd_")
    st, out = post_until(gw, finding_body(priv, el, fid2, "R2", "PASS", 0, pv_hash.H("finding-body-3")), rule_key)
    check("a PASS finding is recorded too", st in (200, 201), (st, out))

    # ---- the ledger event and its anchor
    st, evs = gw.get(f"/v1/events?election_id={el}&type=FINDING_RECORDED")
    items = evs.get("items", [])
    check("two FINDING_RECORDED events are listed", st == 200 and len(items) == 2, (st, evs))
    ev = next((x for x in items if x["event_id"] == first_event), {})
    p = ev.get("payload", {})
    check("event: source gateway, own seq 1, checkpoint = last committed + 1, exact time",
          ev.get("source") == "gateway" and ev.get("seq") == 1 and ev.get("checkpoint") == 1
          and ev.get("time_precision") == "exact" and ev.get("actor") == {"kind": "system"}, ev)
    check("event payload carries the signed record and submitted_via gateway",
          p.get("finding_id") == fid and p.get("finding_hash") == fhash and p.get("rule_id") == "R1"
          and p.get("result") == "FAIL" and p.get("finding_checkpoint") == 0 and p.get("submitted_via") == "gateway"
          and p.get("signature", {}).get("value") == good["signature"]["value"], p)
    check("payload_hash is Hc(payload)", ev.get("payload_hash") == pv_hash.Hc(p))
    end = time.time() + 40
    while time.time() < end:
        st, e1 = gw.get("/v1/events/" + first_event)
        if st == 200 and e1["anchor"]["status"] == "ANCHORED":
            break
        time.sleep(1)
    check("the finding event is anchored (immediate)", st == 200 and e1["anchor"]["status"] == "ANCHORED"
          and e1["anchor"]["mode"] == "immediate", e1)
    st, vr = gw.req("POST", "/v1/verify", {"event_ids": [first_event]})
    check("verify: payload, leaf, proof and on-chain root OK", st == 200 and all(x["verified"] for x in vr["results"]), vr)

    # ---- listing grouped per finding, contract status
    st, fl = gw.get(f"/v1/finding-records?election_id={el}")
    by_id = {x["finding_id"]: x for x in fl.get("items", [])}
    check("finding list: two findings with the contract's own status",
          st == 200 and by_id.get(fid, {}).get("contract_status") == "PENDING"
          and by_id.get(fid2, {}).get("contract_status") == "NOT_REQUIRED", fl)
    check("finding list: recorded event and empty reviews", by_id.get(fid, {}).get("recorded", {}).get("event_id") == first_event
          and by_id.get(fid, {}).get("reviews") == [], by_id.get(fid))

    # ---- Core's seq space is untouched
    st, evs = gw.get(f"/v1/events?election_id={el}")
    check("last_core_seq still counts only Core events (4)", evs.get("last_core_seq") == 4, evs.get("last_core_seq"))
    st, out = gw.req("POST", f"/v1/finding-records/{fid}/reviews", {}, rule_key)
    check("reviews stay 501 until their week", st == 501, (st, out))
    end = time.time() + 40
    while True:
        st, h = gw.get("/v1/health")
        if h.get("pending_events") == 0 or time.time() > end:
            break
        time.sleep(1)
    check("health ok and nothing pending", h["status"] == "ok" and h["pending_events"] == 0, h)

    print(("\n%d check(s) failed" % len(t.FAILED)) if t.FAILED else "\nall checks passed")
    sys.exit(1 if t.FAILED else 0)


if __name__ == "__main__":
    main()
