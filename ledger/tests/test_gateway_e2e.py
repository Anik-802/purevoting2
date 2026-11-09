"""End-to-end check of the Ledger Gateway against a running network (T01-like flow, no Core needed).

  python3 -I ledger/tests/test_gateway_e2e.py --env .env [--gateway http://127.0.0.1:8100]

Needs: the network up, the contract deployed and the registry set up (setup_registry.py), the Gateway running with the
same .env. It plays Core: submits the six T01 system events and voter events with duplicate and out-of-window cases,
waits for the batch anchor and checks proofs, rejections, anchors, organizations and the on-chain election state.
It takes about 90 seconds. Every check prints PASS or FAIL; the exit code is 1 if any failed.
"""
import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import pv_hash  # noqa: E402

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
FAILED = []


def rid(prefix):
    return prefix + "".join(secrets.choice(CROCKFORD) for _ in range(26))


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (("  " + str(extra)) if (extra and not cond) else ""))
    if not cond:
        FAILED.append(name)


class Gw:
    def __init__(self, base, key):
        self.base, self.key = base.rstrip("/"), key

    def req(self, method, path, body=None, key=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data, {"Content-Type": "application/json",
                                                            "X-Service-Key": self.key if key is None else key}, method=method)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path, key=None):
        return self.req("GET", path, key=key)

    def submit(self, events, key=None):
        return self.req("POST", "/v1/events", {"events": events}, key)


def event(election, etype, seq, checkpoint, payload, occurred, voter=False, event_id=None):
    ev = {"event_id": event_id or rid("evr_" if voter else "evt_"), "schema_version": "1", "election_id": election,
          "type": etype, "source": "core", "checkpoint": checkpoint, "occurred_at": iso(occurred),
          "time_precision": "bucketed" if voter else "exact",
          "actor": {"kind": "voter_anonymous" if voter else "system"}, "payload": payload,
          "payload_hash": pv_hash.Hc(payload)}
    if voter:
        ev["time_bucket_seconds"] = 5
    else:
        ev["seq"] = seq
    return ev


def until_final(gw, ev, budget=60):
    """Re-send an event (same event_id) until its outcome is not RETRY_LATER; returns the last result."""
    end = time.time() + budget
    while True:
        st, body = gw.submit([ev])
        assert st == 200, (st, body)
        res = body["results"][0]
        if res["status"] != "RETRY_LATER" or time.time() > end:
            return res
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
    gw = Gw(a.gateway, env["CORE_KEY"])

    # ---- read side and access control
    st, body = gw.get("/v1/health", key="")
    check("health without key", st == 200 and body["chain_reachable"] is True, body)
    check("401 without key", gw.get("/v1/chain", key="")[0] == 401)
    check("401 with a wrong key", gw.get("/v1/chain", key="nope")[0] == 401)
    if env.get("AGENT_KEY") and env["AGENT_KEY"] != "<set>":
        check("403 when the agent key writes events", gw.submit([{}], key=env["AGENT_KEY"])[0] == 403)
    st, chain = gw.get("/v1/chain")
    check("chain info", st == 200 and chain["chain_id"] == env["CHAIN_ID"]
          and chain["confirmation_depth"] == int(env.get("CONFIRMATION_DEPTH", "2"))
          and chain["contract_address"].lower() == env["CONTRACT_ADDRESS"].lower(), chain)
    st, orgs = gw.get("/v1/organizations")
    names = sorted(o["org_id"] for o in orgs.get("items", []))
    check("three organizations registered", names == ["org_commission", "org_neutral", "org_observer"], orgs)
    check("operator is org_commission", [o["org_id"] for o in orgs["items"] if o["is_operator"]] == ["org_commission"])
    check("registration has a real tx hash", all(o["tx_hash"] != "0x" + "00" * 32 for o in orgs["items"]))
    st, nodes = gw.get("/v1/nodes")
    check("node list", st == 200 and nodes["items"] and nodes["items"][0]["reachable"], nodes)

    # ---- election timeline: bucket 5 s, window 45 s, opens about 20 s from now
    el = rid("elc_")
    bucket = 5
    now = int(time.time())
    ws = (now + 20) // bucket * bucket + bucket
    we = ws + 45
    print(f"election {el}, window {iso(ws)} .. {iso(we)}")
    created = {"config_hash": pv_hash.H("config-" + el), "window_start": iso(ws), "window_end": iso(we),
               "time_bucket_seconds": bucket, "checkpoint_interval_seconds": 10, "candidate_count": 2,
               "organizations": [{"org_id": o, "signer": "0x" + "00" * 20} for o in names], "approval_threshold": 2,
               "public_key_hash": pv_hash.H("pk"), "proposal_ttl_hours": 24, "operator_org": "org_commission"}
    e1 = event(el, "ELECTION_CREATED", 1, 0, created, now)
    t0 = time.time()
    r = until_final(gw, e1)
    check("ELECTION_CREATED accepted after confirmation", r["status"] in ("ACCEPTED", "DUPLICATE") and r.get("tx_hash") and r.get("block_number"), r)
    print(f"      (took {time.time() - t0:.1f} s)")
    check("same event again is DUPLICATE with the tx", gw.submit([e1])[1]["results"][0]["status"] == "DUPLICATE")
    changed = dict(e1, payload=dict(created, candidate_count=3))
    changed["payload_hash"] = pv_hash.Hc(changed["payload"])
    check("same event_id, other payload is CONFLICT", gw.submit([changed])[1]["results"][0]["status"] == "CONFLICT")

    st, s = gw.get(f"/v1/elections/{el}/state")
    check("state read from chain: DRAFT with the creation fields", st == 200 and s["status"] == "DRAFT"
          and s["config_hash"] == created["config_hash"] and s["window_start"] == iso(ws) and s["approval_threshold"] == 2
          and s["operator_org"] == "org_commission" and [o["org_id"] for o in s["organizations"]] == names, s)

    roster = pv_hash.set_root([pv_hash.H("voter-%d" % i) for i in range(3)])
    r = until_final(gw, event(el, "ROSTER_FROZEN", 2, 0, {"roster_hash": roster, "voter_count": 3}, int(time.time())))
    check("ROSTER_FROZEN accepted", r["status"] in ("ACCEPTED", "DUPLICATE"), r)
    tokens = [pv_hash.H("token-%d" % i) for i in range(3)]
    troot = pv_hash.set_root(tokens)
    r = until_final(gw, event(el, "TOKEN_BATCH_ISSUED", 3, 0, {"batch_no": 1, "token_count": 3, "token_set_root": troot}, int(time.time())))
    check("TOKEN_BATCH_ISSUED accepted", r["status"] in ("ACCEPTED", "DUPLICATE"), r)
    tables = [{"table": "t%d" % i, "row_count": 0, "snapshot_hash": pv_hash.H("t%d" % i)} for i in range(6)]
    cp0 = {"checkpoint": 0, "reason": "STATE_CHANGE", "tables": tables, "counts": {}, "voter_events": {}}
    r = until_final(gw, event(el, "CHECKPOINT_COMMITTED", 4, 0, cp0, int(time.time())))
    check("CHECKPOINT_COMMITTED 0 accepted", r["status"] in ("ACCEPTED", "DUPLICATE"), r)
    st, s = gw.get(f"/v1/elections/{el}/state")
    check("state: ROSTER_FROZEN, roster and token root on chain, checkpoint 0", s["status"] == "ROSTER_FROZEN"
          and s["roster_hash"] == roster and s["token_set_root"] == troot and s.get("last_checkpoint") == 0, s)

    # ---- opening before window_start is RETRY_LATER (TIME_NOT_REACHED), then accepted
    eo = event(el, "ELECTION_OPENED", 5, 1, {"window_start": iso(ws), "window_end": iso(we)}, ws)
    st, body = gw.submit([eo])
    res = body["results"][0]
    if time.time() < ws - 3:
        check("ELECTION_OPENED before window_start is RETRY_LATER TIME_NOT_REACHED",
              res["status"] == "RETRY_LATER" and res.get("reject_code") == "TIME_NOT_REACHED", res)
    r = until_final(gw, eo, budget=90)
    check("ELECTION_OPENED accepted once the block time reached window_start", r["status"] in ("ACCEPTED", "DUPLICATE"), r)

    # ---- voter events (batched)
    b0 = ws + bucket
    ok_events = []
    for i in range(3):
        ok_events.append(event(el, "TOKEN_USED", 0, 1, {"token_hash": tokens[i]}, b0, voter=True))
        ok_events.append(event(el, "BALLOT_RECEIVED", 0, 1, {"nullifier_hash": pv_hash.H("cred-%d" % i),
                                                           "ballot_hash": pv_hash.H("ballot-%d" % i),
                                                           "receipt_hash": pv_hash.H("receipt-%d" % i)}, b0, voter=True))
    st, body = gw.submit(ok_events)
    check("six voter events ACCEPTED as batched", [x["status"] for x in body["results"]] == ["ACCEPTED"] * 6
          and all(x["anchor_mode"] == "batched" and "tx_hash" not in x for x in body["results"]), body)
    dup = event(el, "TOKEN_USED", 0, 1, {"token_hash": tokens[0]}, b0, voter=True)
    late = event(el, "TOKEN_USED", 0, 1, {"token_hash": pv_hash.H("late")}, we, voter=True)
    early_v = event(el, "TOKEN_USED", 0, 1, {"token_hash": pv_hash.H("early")}, ws - bucket, voter=True)
    st, body = gw.submit([dup, late, early_v, ok_events[0], dict(ok_events[0], payload_hash=pv_hash.H("x"))])
    res = body["results"]
    check("duplicate token_hash REJECTED DUPLICATE_CONTENT", res[0]["status"] == "REJECTED" and res[0]["reject_code"] == "DUPLICATE_CONTENT", res[0])
    check("event after the window REJECTED ELECTION_NOT_OPEN", res[1]["status"] == "REJECTED" and res[1]["reject_code"] == "ELECTION_NOT_OPEN", res[1])
    check("event before the window REJECTED ELECTION_NOT_OPEN", res[2]["status"] == "REJECTED" and res[2]["reject_code"] == "ELECTION_NOT_OPEN", res[2])
    check("resend of an accepted voter event is DUPLICATE", res[3]["status"] == "DUPLICATE", res[3])
    check("same voter event_id with another payload is CONFLICT", res[4]["status"] == "CONFLICT", res[4])
    st, rej = gw.get(f"/v1/rejections?election_id={el}")
    check("three rejection records for the refused voter events", st == 200 and len(rej["items"]) == 3, rej)
    check("a rejected voter event records its bucketed time, never the arrival time",
          {x["reject_code"]: x["recorded_at"] for x in rej["items"]}.get("DUPLICATE_CONTENT") == iso(b0), rej)

    # ---- close, final checkpoint
    while time.time() < we + 1:
        time.sleep(1)
    ec = event(el, "ELECTION_CLOSED", 6, 1, {"reason": "SCHEDULED", "closed_at": iso(we)}, we)
    r = until_final(gw, ec, budget=60)
    check("ELECTION_CLOSED (scheduled) accepted", r["status"] in ("ACCEPTED", "DUPLICATE"), r)
    cp1 = {"checkpoint": 1, "reason": "CLOSE_FINAL", "tables": tables, "counts": {}, "voter_events": {}}
    r = until_final(gw, event(el, "CHECKPOINT_COMMITTED", 7, 1, cp1, int(time.time())))
    check("CHECKPOINT_COMMITTED CLOSE_FINAL accepted", r["status"] in ("ACCEPTED", "DUPLICATE"), r)
    bad = event(el, "ELECTION_OPENED", 8, 2, {"window_start": iso(ws), "window_end": iso(we)}, int(time.time()))
    r = until_final(gw, bad)
    check("a state change the contract refuses is REJECTED INVALID_STATE_TRANSITION with a rejection id",
          r["status"] == "REJECTED" and r["reject_code"] == "INVALID_STATE_TRANSITION" and r.get("rejection_id"), r)
    gap = event(el, "CHECKPOINT_COMMITTED", 12, 2, dict(cp1, checkpoint=2, reason="PERIODIC"), int(time.time()))
    check("a seq gap is RETRY_LATER SEQUENCE_GAP", gw.submit([gap])[1]["results"][0].get("reject_code") == "SEQUENCE_GAP")
    nope = event(el, "TOKEN_REVOKED", 9, 2, {"x": 1}, int(time.time()))
    r = until_final(gw, nope)
    check("an event type outside M1 is REJECTED with a clear detail",
          r["status"] == "REJECTED" and "not implemented in M1" in r.get("detail", ""), r)

    # ---- anchors: wait until every event is anchored
    print("waiting for the voter batch and the immediate anchors ...")
    end = time.time() + 90
    while time.time() < end:
        st, evs = gw.get(f"/v1/events?election_id={el}&anchor_status=PENDING")
        if not evs["items"]:
            break
        time.sleep(2)
    check("no event is left PENDING", not evs["items"], [e["type"] for e in evs["items"]])
    st, anchors = gw.get(f"/v1/anchors?election_id={el}")
    modes = sorted(x["mode"] for x in anchors["items"])
    check("anchors: 7 immediate (one per accepted system event) and at least 1 batched",
          modes.count("immediate") == 7 and modes.count("batched") >= 1, modes)
    batched = [x for x in anchors["items"] if x["mode"] == "batched"]
    check("batched anchors hold the six voter events",
          sum(x["event_count"] for x in batched) == 6 and all("bucket_range" in x for x in batched), batched)
    st, lv = gw.get(f"/v1/anchors/{batched[0]['anchor_id']}/leaves")
    leaves = [x["leaf_hash"] for x in lv["leaves"]]
    check("recomputed merkle_root equals the anchor's", pv_hash.merkle_root(leaves) == batched[0]["merkle_root"])
    empty = pv_hash.set_root([])
    check("a rejections_root other than H('') was anchored", any(x["rejections_root"] != empty for x in anchors["items"]))
    covered = set()
    for x in anchors["items"]:
        covered |= set(gw.get(f"/v1/anchors/{x['anchor_id']}/leaves")[1]["rejection_ids"])
    st, rej = gw.get(f"/v1/rejections?election_id={el}")
    ids = {x["rejection_id"] for x in rej["items"]}
    check("five rejection records (3 voter, 2 system); the 3 recorded before the last anchor are covered",
          len(ids) == 5 and len(covered) == 3 and covered <= ids, (len(ids), len(covered)))

    vid = ok_events[1]["event_id"]
    st, pr = gw.get(f"/v1/events/{vid}/proof")
    h = pr["leaf_hash"]
    for step in pr["path"]:
        h = pv_hash.H(step["hash"] + h) if step["position"] == "left" else pv_hash.H(h + step["hash"])
    check("Merkle proof of a voter event verifies", st == 200 and h == pr["merkle_root"], pr)
    st, vr = gw.req("POST", "/v1/verify", {"event_ids": [vid, e1["event_id"]]})
    check("verify: payload, leaf, proof and on-chain root all OK", st == 200 and all(x["verified"] for x in vr["results"]), vr)
    st, listing = gw.get(f"/v1/events?election_id={el}&type=ELECTION_CREATED")
    check("event list filter by type", [x["type"] for x in listing["items"]] == ["ELECTION_CREATED"]
          and listing["items"][0]["anchor"]["status"] == "ANCHORED" and "received_at" in listing["items"][0], listing)
    st, listing = gw.get(f"/v1/events?election_id={el}")
    voters = [x for x in listing["items"] if x["type"] in ("TOKEN_USED", "BALLOT_RECEIVED")]
    check("voter events carry no received_at and no seq", len(voters) == 6 and all("received_at" not in x and "seq" not in x for x in voters))
    check("last_core_seq counts rejected events too (9 seqs used)", listing["last_core_seq"] == 9, listing["last_core_seq"])
    st, s = gw.get(f"/v1/elections/{el}/state")
    check("final chain state is CLOSED with checkpoint 1", s["status"] == "CLOSED" and s["last_checkpoint"] == 1, s)
    st, h = gw.get("/v1/health", key="")
    check("health ok and nothing pending", h["status"] == "ok" and h["pending_events"] == 0, h)

    print(("\n%d check(s) failed" % len(FAILED)) if FAILED else "\nall checks passed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
