"""Week 2 path against a real PostgreSQL and the fake Gateway: election → creation check → roster → freeze → links →
open → redeem → ballot → periodic checkpoint → views → close / CLOSE_FINAL → finding storage.

Needs TEST_DATABASE_URL (pv2_core on a database initialised with core/db/init); skipped otherwise. CI runs it in the
`db` job.
"""
import base64
import json
import os
import secrets
import time
from urllib.parse import urlparse

import httpx
import nacl.public
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

if URL:
    from app import creation_check, db, hashing, ledger_compare, outbox
    from app.auth import hash_password
    from app.chain import NodeReader, set_reader
    from app.config import settings
    from app.gateway import GatewayClient, set_gateway
    from app.ids import ulid
    from app.main import app, scheduler_tick
    from app.timeutil import clock, iso, parse_iso
    from devtools import fake_gateway

RULE_KEY = "test-rule-key"
B = 60   # demo bucket and checkpoint interval


@pytest.fixture()
def env():
    settings.database_url = URL
    settings.run_workers = False
    settings.jwt_secret = "test-jwt-secret"
    settings.rule_key = RULE_KEY
    settings.agent_key = "test-agent-key"
    settings.creation_check_mode = "stub"
    db._engine = None
    fake_gateway.state = fake_gateway.State()
    set_gateway(GatewayClient(http=TestClient(fake_gateway.app)))
    outbox._backoff.clear()
    t0 = (int(time.time()) // 3600 + 1) * 3600          # an hour boundary in the future
    clock.frozen = t0
    username = "admin_" + secrets.token_hex(4)
    with db.get_engine().begin() as conn:
        conn.execute(text("INSERT INTO credential.staff_users (user_id, username, password_hash, org_id, role) "
                          "VALUES (:i, :u, :p, 'org_commission', 'admin')"),
                     {"i": ulid("usr"), "u": username, "p": hash_password("pw-123456")})
    client = TestClient(app)
    tok = client.post("/v1/auth/login", json={"username": username, "password": "pw-123456"}).json()["access_token"]
    yield {"client": client, "auth": {"Authorization": f"Bearer {tok}"}, "t0": t0}
    clock.frozen = None
    set_gateway(None)
    settings.database_url = ""
    db._engine = None


def deliver_until_idle(*eids):
    # only this test's elections: the database may be shared with a running stack
    for _ in range(20):
        outbox._backoff.clear()
        outbox.deliver_all(list(eids))
        for eid in eids:
            creation_check.check(eid)       # the scheduler's part between deliveries


def outbox_rows(eid):
    with db.get_engine().connect() as conn:
        return [dict(r) for r in conn.execute(text("SELECT event_id, kind, seq, status, event FROM credential.outbox "
                                                   "WHERE election_id = :e"), {"e": eid}).mappings()]


def test_week2_path(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    start, end = t0 + 10 * B, t0 + 20 * B      # 10-minute window, 60 s buckets → 100 voters needed (D-10)

    # ---- create: DRAFT, creation check pending, nothing but ELECTION_CREATED can be delivered ----
    r = c.post("/v1/elections", headers=auth, json={
        "title": "Week 2 test", "window_start": iso(start), "window_end": iso(end),
        "time_bucket_seconds": B, "checkpoint_interval_seconds": B,
        "candidates": [{"name": "Kim"}, {"name": "Lee"}],
        "org_ids": ["org_commission", "org_neutral", "org_observer"], "approval_threshold": 2})
    assert r.status_code == 201, r.text
    e = r.json()
    eid = e["election_id"]
    assert e["status"] == "DRAFT" and e["creation_check"] == "PENDING" and e["operator_org"] == "org_commission"

    voters = [{"external_ref": f"s{i:03d}", "name": f"Voter {i}"} for i in range(100)]
    assert c.put(f"/v1/elections/{eid}/roster", headers=auth, json={"voters": voters}).status_code == 200
    assert c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth).status_code == 409   # check not passed yet

    clock.frozen = t0 + B        # ELECTION_CREATED's bucket has ended
    deliver_until_idle(eid)
    e = c.get(f"/v1/elections/{eid}", headers=auth).json()
    assert e["creation_check"] == "PASSED" and e["transition"]["ledger_status"] == "ACCEPTED"

    # ---- freeze → checkpoint 0 committed (STATE_CHANGE); links ----
    r = c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth)
    assert r.status_code == 202 and r.json()["status"] == "ROSTER_FROZEN" and r.json()["open_checkpoint"] == 1
    assert c.put(f"/v1/elections/{eid}/roster", headers=auth, json={"voters": voters}).status_code == 409

    r = c.post(f"/v1/elections/{eid}/tokens:issue", headers={**auth, "Idempotency-Key": "issue-0001"},
               json={"delivery": "CSV"})
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert lines[0] == "external_ref,name,link" and len(lines) == 101
    again = c.post(f"/v1/elections/{eid}/tokens:issue", headers={**auth, "Idempotency-Key": "issue-0001"},
                   json={"delivery": "CSV"})
    assert again.text == r.text                                   # lost-response retry: same CSV
    tokens = [ln.rsplit("#t=", 1)[1] for ln in lines[1:]]
    assert c.post(f"/v1/elections/{eid}/tokens:issue", headers=auth, json={"delivery": "CSV"}).text == ""

    # ---- open at window_start (permissive: OPEN only after ACCEPTED) ----
    clock.frozen = start
    scheduler_tick([eid])
    assert c.get(f"/v1/public/elections/{eid}").json()["status"] == "ROSTER_FROZEN"
    deliver_until_idle(eid)
    pub = c.get(f"/v1/public/elections/{eid}").json()
    assert pub["status"] == "OPEN"

    # ---- vote ----
    clock.frozen = start + 5
    cand = pub["candidates"][0]["candidate_id"]
    sealed = nacl.public.SealedBox(nacl.public.PublicKey(base64.b64decode(pub["public_key"]))).encrypt(
        json.dumps({"candidate_id": cand}).encode())
    credential = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    nullifier = hashing.H(credential)
    r = c.post("/v1/credentials:redeem", json={"election_id": eid, "token": tokens[0], "nullifier_hash": nullifier})
    assert r.status_code == 200, r.text
    r = c.post("/v1/credentials:redeem", json={"election_id": eid, "token": tokens[0], "nullifier_hash": nullifier})
    assert r.status_code == 409 and r.json()["code"] == "TOKEN_ALREADY_USED"
    bad = c.post("/v1/credentials:redeem", json={"election_id": eid, "token": "A" * 22, "nullifier_hash": "0" * 64})
    assert bad.status_code == 404 and bad.json()["code"] == "TOKEN_INVALID"

    ballot = {"election_id": eid, "credential": credential, "sealed_ballot": base64.b64encode(sealed).decode()}
    r = c.post("/v1/ballots", json=ballot)
    assert r.status_code == 201, r.text
    receipt = r.json()["receipt_hash"]
    assert receipt == hashing.receipt_hash(eid, nullifier, hashing.H(sealed))     # voter can compute it locally
    assert c.post("/v1/ballots", json=ballot).status_code == 200                  # identical retry
    other = dict(ballot, sealed_ballot=base64.b64encode(b"x" * 80).decode())
    assert c.post("/v1/ballots", json=other).json()["code"] == "CREDENTIAL_ALREADY_USED"
    stranger = dict(ballot, credential="B" * 22)
    assert c.post("/v1/ballots", json=stranger).json()["code"] == "CREDENTIAL_UNKNOWN"

    # ---- periodic checkpoint at start + B; delivery; snapshot hash from the view equals the committed one ----
    clock.frozen = start + B
    # a voter request exactly at the boundary T, before the scheduler ran: the request itself commits the PERIODIC
    # checkpoint first, then gets checkpoint k+1 and the bucket that starts at T (boundary serialization)
    cred2 = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
    assert c.post("/v1/credentials:redeem", json={"election_id": eid, "token": tokens[2],
                                                  "nullifier_hash": hashing.H(cred2)}).status_code == 200
    used2 = [r["event"] for r in outbox_rows(eid) if r["event"]["type"] == "TOKEN_USED"
             and r["event"]["occurred_at"] == iso(start + B)]
    assert len(used2) == 1 and used2[0]["checkpoint"] == 3
    scheduler_tick([eid])
    deliver_until_idle(eid)
    rk = {"X-Service-Key": RULE_KEY}
    cps = c.get("/internal/v1/checkpoints", params={"election_id": eid}, headers=rk).json()
    reasons = [(x["checkpoint"], x["reason"], x["ledger_status"]) for x in cps["items"]]
    assert reasons == [(0, "STATE_CHANGE", "ACCEPTED"), (1, "STATE_CHANGE", "ACCEPTED"), (2, "PERIODIC", "ACCEPTED")]
    page = c.get("/internal/v1/views/ballot_receipts", params={"election_id": eid, "checkpoint": 2}, headers=rk).json()
    assert page["total_rows"] == 1 and page["rows"][0]["receipt_hash"] == receipt
    committed = next(r["event"] for r in outbox_rows(eid) if r["event"]["type"] == "CHECKPOINT_COMMITTED"
                     and r["event"]["checkpoint"] == 2)
    by_table = {t["table"]: t for t in committed["payload"]["tables"]}
    assert by_table["ballot_receipts"]["snapshot_hash"] == page["snapshot_hash"]
    assert committed["payload"]["voter_events"] == {"TOKEN_USED": 1, "BALLOT_RECEIVED": 1}
    # early freeze, roster PUT after freeze, used token, bad token, two bad ballots
    assert committed["payload"]["counts"]["rejected_attempts"] == 6
    cfg = c.get("/internal/v1/views/election_config", params={"election_id": eid}, headers=rk).json()
    assert cfg["snapshot_hash"] is None and cfg["rows"][0]["private_key"] is None    # not before TALLY
    assert c.get("/internal/v1/views/roster", params={"election_id": eid}).status_code == 403   # no key

    assert c.get(f"/v1/public/receipts/{receipt}").json()["status"] == "ANCHORED"

    # ---- delivery order: core seq gap-free; inside a unit voter events precede system events ----
    sent = fake_gateway.state.received
    seqs = [ev["seq"] for ev in sent if ev["election_id"] == eid and "seq" in ev]
    assert sorted(set(seqs)) == list(range(1, max(seqs) + 1))
    first_voter = next(i for i, ev in enumerate(sent) if ev["event_id"].startswith("evr_"))
    periodic = next(i for i, ev in enumerate(sent) if ev["type"] == "CHECKPOINT_COMMITTED"
                    and ev["payload"]["reason"] == "PERIODIC")
    assert first_voter < periodic

    # ---- close at window_end: CLOSED (strict), STATE_CHANGE + CLOSE_FINAL; late redeem refused ----
    clock.frozen = end
    r = c.post("/v1/credentials:redeem", json={"election_id": eid, "token": tokens[1], "nullifier_hash": "1" * 64})
    assert r.status_code == 409 and r.json()["code"] == "ELECTION_NOT_OPEN"
    deliver_until_idle(eid)
    items = c.get("/internal/v1/elections", headers=rk).json()["items"]
    mine = next(x for x in items if x["election_id"] == eid)
    assert mine["status"] == "CLOSED" and mine["close_final_checkpoint"] == mine["last_committed_checkpoint"]
    assert all(r["status"] == "ACCEPTED" for r in outbox_rows(eid))
    # every event Core sent is a valid LedgerEventSubmission (contracts/ledger-events.schema.json)
    from app import schemas
    for row in outbox_rows(eid):
        assert schemas.errors("ledger-events.schema.json", "LedgerEventSubmission", row["event"]) == [], row["event"]
    types = [r["event"]["type"] for r in sorted(outbox_rows(eid), key=lambda r: r["seq"] or 0) if r["seq"]]
    # freeze → STATE_CHANGE; open → STATE_CHANGE; 9 PERIODIC (start+60 … start+540); close → STATE_CHANGE, CLOSE_FINAL
    assert types == (["ELECTION_CREATED", "ROSTER_FROZEN", "CHECKPOINT_COMMITTED", "TOKEN_BATCH_ISSUED",
                      "ELECTION_OPENED", "CHECKPOINT_COMMITTED"] + ["CHECKPOINT_COMMITTED"] * 9
                     + ["ELECTION_CLOSED", "CHECKPOINT_COMMITTED", "CHECKPOINT_COMMITTED"])
    periodic = [r["event"]["occurred_at"] for r in outbox_rows(eid) if r["event"]["type"] == "CHECKPOINT_COMMITTED"
                and r["event"]["payload"]["reason"] == "PERIODIC"]
    assert sorted(periodic) == [iso(start + i * B) for i in range(1, 10)]     # logical boundary times (R5 cadence)

    # ---- finding storage: needs the FINDING_RECORDED first ----
    finding = {
        "finding_id": ulid("fnd"), "schema_version": "1", "rule_id": "R1", "rule_version": "0.1.0",
        "election_id": eid, "result": "PASS", "trigger": "SCHEDULED", "checked_at": iso(end + 30),
        "checkpoint": 2, "data_as_of": {"ledger_last_seq": 1, "ledger_last_block": 1,
                                        "checkpoint_event_id": committed["event_id"]},
        "observed": {"accepted_duplicate_tokens": 0, "accepted_duplicate_nullifiers": 0, "unknown_tokens": 0,
                     "unknown_nullifiers": 0, "used_then_revoked": 0, "refused_duplicates": 1},
        "expected": "no accepted duplicates", "summary": "No accepted duplicates.", "evidence_refs": [],
    }
    assert c.post("/internal/v1/findings", json=finding, headers=rk).status_code == 503     # not recorded yet
    fg = TestClient(fake_gateway.app)
    fg.post("/v1/finding-records", json={
        "election_id": eid, "finding_id": finding["finding_id"], "rule_id": "R1", "result": "PASS",
        "finding_checkpoint": 2, "finding_hash": hashing.Hc(finding),
        "signature": {"scheme": "eip191-secp256k1", "signer": "0x" + "c3" * 20, "value": "0x" + "00" * 65}})
    assert c.post("/internal/v1/findings", json=finding, headers=rk).status_code == 201
    assert c.post("/internal/v1/findings", json=finding, headers=rk).status_code == 200
    changed = dict(finding, summary="Changed.")
    assert c.post("/internal/v1/findings", json=changed, headers=rk).status_code == 409
    stored = c.get(f"/internal/v1/findings/{finding['finding_id']}", headers=rk).json()
    assert stored["review"]["status"] == "NOT_REQUIRED" and stored["recorded_event_id"].startswith("evt_")
    listed = c.get(f"/v1/elections/{eid}/findings", headers=auth).json()["items"]
    assert listed == [stored]
    assert c.get(f"/v1/elections/{eid}/findings?rule_id=R1&result=PASS", headers=auth).json()["items"] == [stored]
    assert c.get(f"/v1/elections/{eid}/findings?result=FAIL", headers=auth).json()["items"] == []
    assert c.get(f"/v1/elections/{eid}/findings").status_code == 401
    assert c.get(f"/v1/elections/{eid}/findings?rule_id=R9", headers=auth).status_code == 400
    assert c.get(f"/v1/elections/{eid}/findings?result=pass", headers=auth).status_code == 400
    assert c.get("/v1/elections/elc_unknown/findings", headers=auth).status_code == 404


def test_ledger_rejection_of_election_created_allows_delete(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    fake_gateway.state.reject_types.add("ELECTION_CREATED")
    r = c.post("/v1/elections", headers=auth, json={
        "title": "Rejected", "window_start": iso(t0 + 600), "window_end": iso(t0 + 1200),
        "time_bucket_seconds": B, "checkpoint_interval_seconds": B, "candidates": [{"name": "A"}],
        "org_ids": ["org_commission", "org_neutral", "org_observer"], "approval_threshold": 2})
    eid = r.json()["election_id"]
    assert c.post(f"/v1/elections/{eid}:delete", headers=auth).status_code == 409     # still pending
    clock.frozen = t0 + B
    deliver_until_idle(eid)
    e = c.get(f"/v1/elections/{eid}", headers=auth).json()
    assert e["transition"]["ledger_status"] == "REJECTED"
    alerts = c.get(f"/v1/elections/{eid}/audit-alerts", headers=auth).json()["items"]
    assert [a["kind"] for a in alerts] == ["TRANSITION_REJECTED"]
    assert c.post(f"/v1/elections/{eid}:delete", headers=auth).status_code == 204
    assert c.get(f"/v1/elections/{eid}", headers=auth).status_code == 404


def test_bucket_too_small_and_validation(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    base = {"title": "Small", "window_start": iso(t0 + 600), "window_end": iso(t0 + 1200),
            "time_bucket_seconds": B, "checkpoint_interval_seconds": B, "candidates": [{"name": "A"}],
            "org_ids": ["org_commission", "org_neutral", "org_observer"], "approval_threshold": 2}
    assert c.post("/v1/elections", headers=auth, json=dict(base, window_start=iso(t0 + 601))).status_code == 422
    assert c.post("/v1/elections", headers=auth, json=dict(base, approval_threshold=3)).status_code == 422
    assert c.post("/v1/elections", headers=auth,
                  json=dict(base, org_ids=["org_commission", "org_neutral", "org_unknown"])).json()["code"] \
        == "ORG_NOT_REGISTERED"
    eid = c.post("/v1/elections", headers=auth, json=base).json()["election_id"]
    clock.frozen = t0 + B
    deliver_until_idle(eid)
    c.put(f"/v1/elections/{eid}/roster", headers=auth,
          json={"voters": [{"external_ref": f"x{i}", "name": "n"} for i in range(20)]})
    r = c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "BUCKET_TOO_SMALL"


def _create(c, auth, t0, title="Idem", key=None):
    headers = dict(auth, **({"Idempotency-Key": key} if key else {}))
    return c.post("/v1/elections", headers=headers, json={
        "title": title, "window_start": iso(t0 + 600), "window_end": iso(t0 + 1200),
        "time_bucket_seconds": B, "checkpoint_interval_seconds": B, "candidates": [{"name": "A"}],
        "org_ids": ["org_commission", "org_neutral", "org_observer"], "approval_threshold": 2})


def test_idempotency_keys(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    key = "create-" + secrets.token_hex(4)
    first = _create(c, auth, t0, key=key)
    again = _create(c, auth, t0, key=key)
    assert first.status_code == 201 and again.status_code == 201
    assert again.json()["election_id"] == first.json()["election_id"]          # no second election
    assert _create(c, auth, t0, title="Other", key=key).status_code == 409      # same key, different body

    eid = first.json()["election_id"]
    clock.frozen = t0 + B
    deliver_until_idle(eid)
    c.put(f"/v1/elections/{eid}/roster", headers=auth,
          json={"voters": [{"external_ref": f"v{i}", "name": "n"} for i in range(100)]})
    assert c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth).status_code == 202
    ikey = {**auth, "Idempotency-Key": "issue-" + secrets.token_hex(4)}
    csv1 = c.post(f"/v1/elections/{eid}/tokens:issue", headers=ikey, json={"delivery": "CSV"}).text
    assert len(csv1.splitlines()) == 101
    assert c.post(f"/v1/elections/{eid}/tokens:issue", headers=ikey, json={"delivery": "CSV"}).text == csv1
    from app import elections as el
    el.issue_cache._items.clear()                       # CSV wiped (15 min passed, or Core restarted)
    late = c.post(f"/v1/elections/{eid}/tokens:issue", headers=ikey, json={"delivery": "CSV"})
    assert late.status_code == 409                      # never an empty 200 that hides the lost links


def test_gateway_4xx_raises_alert(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    eid = _create(c, auth, t0, title="4xx").json()["election_id"]
    fake_gateway.state.fail_status = 400
    clock.frozen = t0 + B
    deliver_until_idle(eid)
    kinds = [a["kind"] for a in c.get(f"/v1/elections/{eid}/audit-alerts", headers=auth).json()["items"]]
    assert kinds == ["LEDGER_UNAVAILABLE"]
    assert c.get(f"/v1/elections/{eid}", headers=auth).json()["creation_check"] == "PENDING"




# ---- creation check against the pinned node stand-in (fake_gateway /rpc), CREATION_CHECK_MODE=node ----

PINNED = ["http://a/rpc", "http://b/rpc"]


class _Nodes:
    """HTTP client for the reader: two pinned nodes in front of the fake node, each with a test-chosen behaviour."""
    def __init__(self):
        self.client = TestClient(fake_gateway.app)
        self.modes = {"a": "down", "b": "ok"}     # default: the first URL is down, the reader falls through

    def post(self, url, json=None, **kw):
        mode, method = self.modes[urlparse(url).hostname], json["method"]
        if mode == "down":
            raise httpx.ConnectError("refused")
        if mode == "lagging" and method == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(fake_gateway.state.block)})
        if mode == "call_error" and method == "eth_call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}})
        if mode == "garbage" and method == "eth_call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0xzz"})
        return self.client.post("http://testserver/rpc", json=json)


@pytest.fixture()
def node(env):
    settings.creation_check_mode = "node"
    # fixed values (the container's own .env may hold placeholders)
    settings.chain_id = fake_gateway.state.chain_id = "7777"
    settings.contract_address = fake_gateway.state.contract_address = "0x" + "cc" * 20
    settings.confirmation_depth = fake_gateway.state.confirmation_depth = 2
    settings.pinned_node_url = ",".join(PINNED)
    fake_gateway.state.rpc_urls = list(PINNED)
    creation_check._since.clear()
    creation_check._config_checked.clear()
    nodes = _Nodes()
    set_reader(NodeReader(urls=PINNED, contract=fake_gateway.state.contract_address, http=nodes))
    yield dict(env, nodes=nodes)
    set_reader(None)
    settings.creation_check_mode = "stub"
    settings.pinned_node_url = ""


def _created(c, auth, t0, title):
    eid = _create(c, auth, t0, title=title).json()["election_id"]
    clock.frozen = t0 + B
    deliver_until_idle(eid)
    return eid


def _check(c, auth, eid):
    scheduler_tick([eid])
    e = c.get(f"/v1/elections/{eid}", headers=auth).json()
    kinds = [a["kind"] for a in c.get(f"/v1/elections/{eid}/audit-alerts", headers=auth).json()["items"]]
    return e["creation_check"], kinds


def _detail(c, auth, eid):
    return c.get(f"/v1/elections/{eid}/audit-alerts", headers=auth).json()["items"][0]["detail"]


def test_node_mode_passes_once_confirmed(node):
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    fake_gateway.state.node_ahead = 1                        # block + 1 < block + D: not confirmed on the node yet
    eid = _created(c, auth, t0, "Node")
    assert _check(c, auth, eid) == ("PENDING", [])
    assert c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth).status_code == 409
    creation_check._since.clear()                            # Core restarted: the PENDING check is picked up again
    creation_check._config_checked.clear()
    fake_gateway.state.node_ahead = 2
    assert _check(c, auth, eid) == ("PASSED", [])


def test_node_mode_mismatch_names_the_fields(node):
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    fake_gateway.state.node_tamper = {"approval_threshold": 1, "window_end": t0 + 9999}
    eid = _created(c, auth, t0, "Tampered")
    assert _check(c, auth, eid) == ("MISMATCH", ["GOVERNANCE_MISMATCH"])
    assert _detail(c, auth, eid) == "differs: window_end, approval_threshold"
    assert c.post(f"/v1/elections/{eid}:delete", headers=auth).status_code == 204


def test_node_mode_org_order_is_checked(node):
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    orgs = sorted(["org_commission", "org_neutral", "org_observer"])
    fake_gateway.state.node_tamper = {"org_ids": [fake_gateway._idh(o) for o in reversed(orgs)]}   # same set, other order
    eid = _created(c, auth, t0, "Order")
    assert _check(c, auth, eid) == ("MISMATCH", ["GOVERNANCE_MISMATCH"])
    assert _detail(c, auth, eid) == "differs: org_ids"


def test_node_mode_not_found_after_grace(node):
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    fake_gateway.state.node_tamper = {"config_hash": "0" * 64}   # the node reads the election as missing
    eid = _created(c, auth, t0, "Missing")
    assert _check(c, auth, eid) == ("PENDING", [])
    clock.frozen += 59
    assert _check(c, auth, eid) == ("PENDING", [])
    clock.frozen += 1
    assert _check(c, auth, eid) == ("MISMATCH", ["GOVERNANCE_MISMATCH"])
    assert _detail(c, auth, eid) == "not found"


def test_node_mode_never_mixes_nodes(node):
    """Node a is confirmed but its eth_call fails; node b lags. Reading a's block number with b's state would turn the
    delay into 'not found'; one reading comes from one node, so the check just waits."""
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    node["nodes"].modes = {"a": "call_error", "b": "lagging"}
    eid = _created(c, auth, t0, "Mixed")
    for _ in range(3):
        clock.frozen += 61
        assert _check(c, auth, eid) == ("PENDING", [])
    node["nodes"].modes = {"a": "lagging", "b": "ok"}        # a answers but is stuck: b, which confirmed, decides
    assert _check(c, auth, eid) == ("PASSED", [])


def test_node_mode_malformed_answers(node):
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    node["nodes"].modes = {"a": "garbage", "b": "ok"}          # a malformed result moves on to the next node
    eid = _created(c, auth, t0, "Garbage")
    assert _check(c, auth, eid) == ("PASSED", [])
    node["nodes"].modes = {"a": "garbage", "b": "garbage"}
    eid = _created(c, auth, t0, "Garbage2")
    assert _check(c, auth, eid) == ("PENDING", [])
    clock.frozen += 60
    assert _check(c, auth, eid) == ("PENDING", ["LEDGER_UNAVAILABLE"])
    clock.frozen += 30
    assert _check(c, auth, eid) == ("PENDING", ["LEDGER_UNAVAILABLE"])   # raised once


def test_node_mode_unreachable_alerts_then_recovers(node):
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    fake_gateway.state.node_down = True
    eid = _created(c, auth, t0, "Down")
    assert _check(c, auth, eid) == ("PENDING", [])
    clock.frozen += 60
    assert _check(c, auth, eid) == ("PENDING", ["LEDGER_UNAVAILABLE"])
    fake_gateway.state.node_down = False
    assert _check(c, auth, eid)[0] == "PASSED"


def test_node_mode_starts_from_duplicate(node):
    """ELECTION_CREATED answered DUPLICATE (Core retried after a lost answer) also starts the check."""
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    fake_gateway.state.node_ahead = 0
    eid = _created(c, auth, t0, "Dup")
    with db.get_engine().begin() as conn:                     # as if the ACCEPTED answer had been lost
        conn.execute(text("UPDATE credential.outbox SET status = 'PENDING' WHERE election_id = :e"), {"e": eid})
        conn.execute(text("UPDATE credential.elections SET created_block = NULL WHERE election_id = :e"), {"e": eid})
    fake_gateway.state.node_ahead = 2
    deliver_until_idle(eid)
    assert [r["status"] for r in outbox_rows(eid)] == ["DUPLICATE"]
    assert _check(c, auth, eid) == ("PASSED", [])


def test_gateway_chain_config_differs(node):
    c, auth, t0 = node["client"], node["auth"], node["t0"]
    fake_gateway.state.confirmation_depth = 5                # the Gateway reports another D than the pinned one
    fake_gateway.state.rpc_urls = ["http://a/rpc"]           # and does not list the second pinned node
    fake_gateway.state.chain_id = hex(int(settings.chain_id))   # the same chain id written in hex: no difference
    eid = _created(c, auth, t0, "Config")
    assert _check(c, auth, eid) == ("PASSED", ["CHAIN_CONFIG_MISMATCH"])   # alert only; the pinned values are used
    assert _detail(c, auth, eid) == "Gateway /v1/chain differs: confirmation_depth, rpc_urls"


# ---- outbox comparison: ledger events vs the outbox (threat model I12) ----

def _fifo_stopped(eid, value=None):
    with db.get_engine().begin() as conn:
        if value is not None:      # the operators resumed the FIFO
            conn.execute(text("UPDATE credential.elections SET fifo_stopped = :v WHERE election_id = :e"),
                         {"v": value, "e": eid})
        return conn.execute(text("SELECT fifo_stopped FROM credential.elections WHERE election_id = :e"),
                            {"e": eid}).scalar_one()


def _alerts(c, auth, eid):
    items = c.get(f"/v1/elections/{eid}/audit-alerts", headers=auth).json()["items"]
    return {a["kind"]: a.get("detail") for a in items}


def _frozen(c, auth, t0, title):
    """ELECTION_CREATED, ROSTER_FROZEN and checkpoint 0 accepted by the fake Gateway."""
    now = clock.frozen                  # a second election in the same test starts where the clock is
    eid = _create(c, auth, t0, title=title).json()["election_id"]
    clock.frozen = now + B
    deliver_until_idle(eid)
    c.put(f"/v1/elections/{eid}/roster", headers=auth,
          json={"voters": [{"external_ref": f"v{i}", "name": "n"} for i in range(100)]})
    assert c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth).status_code == 202
    clock.frozen = now + 2 * B
    deliver_until_idle(eid)
    assert [r["status"] for r in outbox_rows(eid)] == ["ACCEPTED"] * 3
    for memo in (ledger_compare._equal, ledger_compare._reported, ledger_compare._absent, ledger_compare._unreadable):
        memo.pop(eid, None)
    return eid, {r["event"]["type"]: r["event_id"] for r in outbox_rows(eid)}


def _stored(eid, event_id, type_, payload, **extra):
    """An event the (dishonest) Gateway lists for this election."""
    ev = {"event_id": event_id, "schema_version": "1", "election_id": eid, "type": type_, "source": "core",
          "checkpoint": 0, "occurred_at": "2026-01-01T00:00:00Z", "time_precision": "exact",
          "actor": {"kind": "system"}, "payload": payload, "anchor": {"status": "PENDING"}}
    if isinstance(payload, dict):
        ev["payload_hash"] = hashing.Hc(payload)
    ev |= extra
    fake_gateway.state.events[str(event_id)] = ev
    return ev


def test_outbox_comparison_honest_gateway(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    eid, _ = _frozen(c, auth, t0, "Honest")
    assert ledger_compare.compare(eid) == [] and ledger_compare.compare(eid) == []   # two passes: nothing absent
    assert len(ledger_compare._equal[eid]) == 3                  # not compared with the outbox again
    with db.get_engine().begin() as conn:                         # the ACCEPTED answer was lost: still PENDING here
        conn.execute(text("UPDATE credential.outbox SET status = 'PENDING' WHERE election_id = :e "
                          "AND event->>'type' = 'ROSTER_FROZEN'"), {"e": eid})
    ledger_compare._equal.pop(eid)
    _stored(eid, ulid("evt"), "APPROVAL_RECORDED", {"x": 1}, source="gateway")   # a Gateway event type
    assert ledger_compare.compare(eid) == []
    assert not _fifo_stopped(eid) and _alerts(c, auth, eid) == {}


def test_outbox_comparison_detects_injected_and_altered_events(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    eid, ids = _frozen(c, auth, t0, "Injected")
    assert ledger_compare.compare(eid) == []                     # all three found equal and memoised
    st = fake_gateway.state.events
    created = st[ids["ELECTION_CREATED"]]                        # altered, with a matching payload_hash
    p = dict(created["payload"], approval_threshold=1)
    st[ids["ELECTION_CREATED"]] = dict(created, payload=p, payload_hash=hashing.Hc(p))
    frozen = st[ids["ROSTER_FROZEN"]]                            # payload altered, stale payload_hash
    st[ids["ROSTER_FROZEN"]] = dict(frozen, payload=dict(frozen["payload"], voter_count=4))
    cp = st[ids["CHECKPOINT_COMMITTED"]]                         # only the checkpoint field moved
    st[ids["CHECKPOINT_COMMITTED"]] = dict(cp, checkpoint=7)
    injected, tally = ulid("evt"), ulid("evt")
    _stored(eid, injected, "ELECTION_OPENED", {"opened": True})
    _stored(eid, tally, "TALLY_PUBLISHED", {"t": 1}, source="gateway")   # a Core type marked as the Gateway's
    _stored(eid, "evr_" + "7" * 26, "BALLOT_RECEIVED", {"ballot_hash": "0" * 64})
    _stored(eid, ["not", "an", "id"], "<b>X</b>", "not an object")
    problems = ledger_compare.compare(eid)
    assert sorted(problems) == sorted([
        f"ELECTION_CREATED {ids['ELECTION_CREATED']}: differs: payload, payload_hash",
        f"ROSTER_FROZEN {ids['ROSTER_FROZEN']}: differs: payload",
        f"CHECKPOINT_COMMITTED {ids['CHECKPOINT_COMMITTED']}: differs: checkpoint",
        f"ELECTION_OPENED {injected}: not in the outbox", f"TALLY_PUBLISHED {tally}: not in the outbox",
        "BALLOT_RECEIVED: not in the outbox", "unknown type: malformed event_id"])
    alerts = _alerts(c, auth, eid)
    assert set(alerts) == {"OUTBOX_MISMATCH", "FIFO_STOPPED"} and _fifo_stopped(eid)
    assert "evr_" not in alerts["OUTBOX_MISMATCH"] and "0" * 64 not in alerts["OUTBOX_MISMATCH"]
    # the operators resume: known problems do not stop the FIFO again; a new one does and is added to the alert
    _fifo_stopped(eid, False)
    assert ledger_compare.compare(eid) and not _fifo_stopped(eid)
    later = ulid("evt")
    _stored(eid, later, "ELECTION_CLOSED", {"c": 1})
    ledger_compare.compare(eid)
    assert _fifo_stopped(eid)
    assert f"ELECTION_CLOSED {later}: not in the outbox" in _alerts(c, auth, eid)["OUTBOX_MISMATCH"]
    # with the FIFO stopped, a later event is not delivered
    before = len(fake_gateway.state.received)
    clock.frozen += 600
    scheduler_tick([eid])
    deliver_until_idle(eid)
    assert len(fake_gateway.state.received) == before


def test_outbox_comparison_hostile_values(env):
    """Values meant to break the comparison (NUL in an ID, checkpoint 2**64, deep nesting) are reported, never an
    exception that rolls back or skips the pass."""
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    eid, _ = _frozen(c, auth, t0, "Hostile")
    honest = GatewayClient(http=TestClient(fake_gateway.app))
    deep = []
    for _ in range(5000):
        deep = [deep]
    big, nested = ulid("evt"), ulid("evt")

    class Hostile:
        def events(self, election_id):
            return honest.events(election_id) + [
                {"event_id": "evt_" + "A" * 25 + "\x00", "type": "ELECTION_OPENED", "source": "core"},
                {"event_id": big, "type": "ELECTION_OPENED", "source": "core", "checkpoint": 2**64},
                {"event_id": nested, "type": "ELECTION_OPENED", "source": "core", "payload": deep},
                "not an object"]
    set_gateway(Hostile())
    problems = ledger_compare.compare(eid)
    set_gateway(honest)
    assert problems[0] == "ELECTION_OPENED: malformed event_id" and problems[3] == "unknown type: not an object"
    assert problems[1] in (f"ELECTION_OPENED {big}: not in the outbox", f"ELECTION_OPENED {big}: unreadable")
    assert problems[2] == f"ELECTION_OPENED {nested}: unreadable" and len(problems) == 4
    assert _fifo_stopped(eid) and "\x00" not in _alerts(c, auth, eid)["OUTBOX_MISMATCH"]


def test_outbox_comparison_absent_rejected_and_unreadable(env):
    """An accepted event missing from the listing in two passes in a row is reported; an event answered REJECTED but
    listed is reported; an unreadable listing alerts after 90 s without stopping the FIFO."""
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    eid, ids = _frozen(c, auth, t0, "Absent")
    del fake_gateway.state.events[ids["ROSTER_FROZEN"]]          # the Gateway drops an accepted event
    assert ledger_compare.compare(eid) == []                     # first pass: may be a lagging listing
    assert ledger_compare.compare(eid) == [f"ROSTER_FROZEN {ids['ROSTER_FROZEN']}: accepted but not listed"]
    assert _fifo_stopped(eid)

    eid, ids = _frozen(c, auth, t0, "Unreadable")
    honest = GatewayClient(http=TestClient(fake_gateway.app))

    class Down:
        def get(self, *a, **k):
            raise httpx.ConnectError("refused")
    set_gateway(GatewayClient(http=Down()))
    assert ledger_compare.compare(eid) == [] and _alerts(c, auth, eid) == {}
    clock.frozen += 90
    assert ledger_compare.compare(eid) == []
    set_gateway(honest)
    assert set(_alerts(c, auth, eid)) == {"LEDGER_UNAVAILABLE"} and not _fifo_stopped(eid)
    with db.get_engine().begin() as conn:     # the Gateway answered REJECTED but lists the event as stored
        conn.execute(text("UPDATE credential.outbox SET status = 'REJECTED' WHERE election_id = :e "
                          "AND event->>'type' = 'ROSTER_FROZEN'"), {"e": eid})
    assert ledger_compare.compare(eid) == [f"ROSTER_FROZEN {ids['ROSTER_FROZEN']}: stored although answered REJECTED"]
    assert _fifo_stopped(eid)


def test_outbox_comparison_delivery_between_the_two_reads(env):
    """Events written and delivered after the outbox was read but before the listing are not a mismatch."""
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    eid = _create(c, auth, t0, title="Race").json()["election_id"]
    clock.frozen = t0 + B
    deliver_until_idle(eid)
    c.put(f"/v1/elections/{eid}/roster", headers=auth,
          json={"voters": [{"external_ref": f"v{i}", "name": "n"} for i in range(100)]})
    honest = GatewayClient(http=TestClient(fake_gateway.app))

    class Racing:
        def events(self, election_id):
            # the outbox worker runs between the comparison's two reads: freeze → ROSTER_FROZEN + checkpoint 0
            assert c.post(f"/v1/elections/{eid}/roster:freeze", headers=auth).status_code == 202
            clock.frozen = t0 + 2 * B
            set_gateway(honest)
            deliver_until_idle(eid)
            return honest.events(election_id)
    set_gateway(Racing())
    try:
        assert ledger_compare.compare(eid) == []
    finally:
        set_gateway(honest)
    assert len(fake_gateway.state.events) >= 3 and not _fifo_stopped(eid) and _alerts(c, auth, eid) == {}


def test_outbox_comparison_second_voter_injection_after_resume(env):
    c, auth, t0 = env["client"], env["auth"], env["t0"]
    eid, _ = _frozen(c, auth, t0, "Voter")
    _stored(eid, "evr_" + "7" * 26, "BALLOT_RECEIVED", {"ballot_hash": "1" * 64})
    assert ledger_compare.compare(eid) == ["BALLOT_RECEIVED: not in the outbox"] and _fifo_stopped(eid)
    _fifo_stopped(eid, False)                                    # resumed by the operators
    assert ledger_compare.compare(eid) and not _fifo_stopped(eid)
    _stored(eid, "evr_" + "8" * 26, "BALLOT_RECEIVED", {"ballot_hash": "2" * 64})   # same wording, another event
    ledger_compare.compare(eid)
    assert _fifo_stopped(eid)
