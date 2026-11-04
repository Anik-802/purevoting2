"""Stateful stand-in for the Ledger Gateway, for Core tests and local runs before week 3 (not a contract
implementation: no chain, no signature checks, anchors are pretend).

    uvicorn devtools.fake_gateway:app --port 8100      (from core/)

It follows the parts of ledger-gateway.openapi.yaml that Core depends on: per-event outcomes of POST /v1/events
(seq order, DUPLICATE / CONFLICT by event_id, voter events wait for ELECTION_OPENED), the organization registry,
finding records and GET /v1/chain. POST /rpc stands in for the pinned node of the creation check
(PINNED_NODE_URL=http://ledger-gateway:8100/rpc with CREATION_CHECK_MODE=node).
"""
import hashlib
import json
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ORGS = [
    {"org_id": "org_commission", "signer": "0x" + "a1" * 20, "is_operator": True},
    {"org_id": "org_neutral", "signer": "0x" + "b2" * 20, "is_operator": False},
    {"org_id": "org_observer", "signer": "0x" + "c3" * 20, "is_operator": False},
]


class State:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.last_seq: dict[str, int] = {}
        self.opened: set[str] = set()
        self.block = 100
        self.reject_types: set[str] = set()     # tests: force REJECTED for these event types
        self.records: dict[str, dict] = {}      # finding_id -> FINDING_RECORDED event
        self.received: list[dict] = []          # every submission, in arrival order (tests check delivery order)
        self.fail_status: int | None = None     # tests: answer POST /v1/events with this HTTP status
        # pinned chain configuration it reports, and the node stand-in (/rpc)
        self.chain_id = os.environ.get("CHAIN_ID", "7777")
        self.contract_address = os.environ.get("CONTRACT_ADDRESS", "0x" + "cc" * 20)
        self.confirmation_depth = int(os.environ.get("CONFIRMATION_DEPTH", "2"))
        self.node_ahead = self.confirmation_depth   # node's latest block = last included block + this
        self.node_down = False
        self.node_tamper: dict = {}             # tests: on-chain fields that differ from ELECTION_CREATED
        self.rpc_urls = [u.strip() for u in os.environ.get("PINNED_NODE_URL", "").split(",") if u.strip()] \
            or ["http://ledger-gateway:8100/rpc"]


state = State()
app = FastAPI(title="fake ledger gateway")


def _tx() -> str:
    return "0x" + secrets.token_hex(32)


@app.post("/v1/events")
async def submit(request: Request):
    if state.fail_status:
        return JSONResponse({"type": "about:blank", "title": "refused", "status": state.fail_status,
                             "code": "VALIDATION_ERROR"}, status_code=state.fail_status)
    body = await request.json()
    results = []
    for ev in body["events"]:
        state.received.append(ev)
        eid, el = ev["event_id"], ev["election_id"]
        old = state.events.get(eid)
        if old is not None:
            res = {"event_id": eid, "status": "DUPLICATE" if old["payload_hash"] == ev["payload_hash"] else "CONFLICT"}
            if res["status"] == "DUPLICATE" and "anchor" in old and not eid.startswith("evr_"):
                res |= {"tx_hash": old["anchor"]["tx_hash"], "block_number": old["anchor"]["block_number"]}
            results.append(res)
            continue
        voter = eid.startswith("evr_")
        if voter and el not in state.opened:
            results.append({"event_id": eid, "status": "RETRY_LATER"})
            continue
        if not voter:
            expected = state.last_seq.get(el, 0) + 1
            if ev["seq"] != expected:
                results.append({"event_id": eid, "status": "RETRY_LATER", "reject_code": "SEQUENCE_GAP"})
                continue
            state.last_seq[el] = ev["seq"]
        if ev["type"] in state.reject_types:
            state.events[eid] = ev
            results.append({"event_id": eid, "status": "REJECTED", "reject_code": "INVALID_STATE_TRANSITION",
                            "rejection_id": "rej_" + "0" * 26})
            continue
        state.block += 1
        state.events[eid] = dict(ev, anchor={"status": "ANCHORED", "anchor_id": "anc_" + "1" * 26,
                                             "mode": "batched" if voter else "immediate",
                                             "merkle_root": "0" * 64, "tx_hash": _tx(), "block_number": state.block})
        if ev["type"] == "ELECTION_OPENED":
            state.opened.add(el)
        res = {"event_id": eid, "status": "ACCEPTED", "anchor_mode": "batched" if voter else "immediate"}
        if not voter:
            res |= {"tx_hash": _tx(), "block_number": state.block}
        results.append(res)
    return {"results": results}


@app.get("/v1/events")
def list_events(election_id: str, limit: int = 100, cursor: str | None = None):
    """Stored events (REJECTED ones are rejection records, not events), by occurred_at then event_id; the cursor is an
    offset. Tests inject or alter entries of state.events to play a dishonest Gateway."""
    items = sorted((ev for ev in state.events.values() if ev.get("election_id") == election_id and "anchor" in ev),
                   key=lambda ev: (str(ev.get("occurred_at")), str(ev.get("event_id"))))
    start = int(cursor or 0)
    page = items[start:start + limit]
    return {"items": page, "next_cursor": str(start + limit) if start + limit < len(items) else None,
            "last_core_seq": state.last_seq.get(election_id, 0)}


@app.get("/v1/events/{event_id}")
def get_event(event_id: str):
    ev = state.events.get(event_id)
    if ev is None:
        return JSONResponse({"type": "about:blank", "title": "not found", "status": 404, "code": "NOT_FOUND"},
                            status_code=404)
    return ev


@app.get("/v1/organizations")
def organizations():
    return {"items": [o | {"registered_by": "0x" + "00" * 20, "tx_hash": "0x" + "00" * 32, "block_number": 1}
                      for o in ORGS]}


@app.post("/v1/finding-records", status_code=201)
async def record_finding(request: Request):
    b = await request.json()
    fid = b["finding_id"]
    if fid in state.records:
        same = state.records[fid]["payload"]["finding_hash"] == b["finding_hash"]
        return JSONResponse({"event_id": state.records[fid]["event_id"]}, status_code=200 if same else 409)
    payload = {k: b[k] for k in ("finding_id", "rule_id", "result", "finding_checkpoint", "finding_hash", "signature")}
    payload["submitted_via"] = "GATEWAY"
    event_id = "evt_" + "".join(secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(26))
    state.records[fid] = {"event_id": event_id, "election_id": b["election_id"], "type": "FINDING_RECORDED",
                          "source": "gateway", "payload": payload,
                          "payload_hash": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}
    return {"event_id": event_id}


@app.get("/v1/finding-records")
def finding_records(election_id: str, limit: int = 100, cursor: str | None = None):
    items = [{"finding_id": fid, "recorded": ev, "reviews": [],
              "contract_status": "NOT_REQUIRED" if ev["payload"]["result"] == "PASS" else "PENDING"}
             for fid, ev in state.records.items() if ev["election_id"] == election_id]
    return {"items": items, "next_cursor": None}


@app.get("/v1/health")
def health():
    return {"status": "ok"}


@app.get("/v1/chain")
def chain():
    return {"platform": "other", "chain_id": state.chain_id, "contract_address": state.contract_address,
            "interface_version": "1.1.0", "rpc_urls": state.rpc_urls,
            "confirmation_depth": state.confirmation_depth}


# ---- pinned node stand-in: JSON-RPC for the creation check (eth_blockNumber, eth_call of two getters) ----
# The "chain" holds what an accepted ELECTION_CREATED would have written; tests can make the node lag, go down or
# hold different values. Encoding is written here independently of app/chain.py so the two check each other.

SEL_STATE, SEL_GOVERNANCE = "19dfd414", "cbce74a6"   # electionState(bytes32), electionGovernance(bytes32)


def _idh(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _unix(iso_z: str) -> int:
    from datetime import datetime
    return int(datetime.fromisoformat(iso_z.replace("Z", "+00:00")).timestamp())


def _word(v) -> str:
    return v.rjust(64, "0") if isinstance(v, str) else format(v, "064x")


def _on_chain(election_id_h: str) -> dict | None:
    for ev in state.events.values():
        if ev["type"] == "ELECTION_CREATED" and "anchor" in ev and _idh(ev["election_id"]) == election_id_h:
            p = ev["payload"]
            onchain = {"config_hash": p["config_hash"], "window_start": _unix(p["window_start"]),
                       "window_end": _unix(p["window_end"]), "time_bucket_seconds": p["time_bucket_seconds"],
                       "checkpoint_interval_seconds": p["checkpoint_interval_seconds"],
                       "org_ids": [_idh(o["org_id"]) for o in sorted(p["organizations"], key=lambda o: o["org_id"])],
                       "approval_threshold": p["approval_threshold"], "operator_org_id": _idh(p["operator_org"]),
                       "proposal_ttl_hours": p["proposal_ttl_hours"]}
            return onchain | state.node_tamper
    return None


def _eth_call(data: str) -> str:
    sel, arg = data[2:10], data[10:74]
    e = _on_chain(arg)
    if sel == SEL_STATE:
        if e is None:
            return "0x" + "0" * 64 * 11                  # unknown election: all zeros (configHash == 0)
        words = [0, e["window_start"], e["window_end"], e["time_bucket_seconds"], e["config_hash"], 0, 0, 0, 0, 0,
                 e["operator_org_id"]]
    elif sel == SEL_GOVERNANCE:
        if e is None:
            return "0x" + _word(5 * 32) + "0" * 64 * 5    # empty orgIds
        words = [5 * 32, e["approval_threshold"], e["operator_org_id"], e["proposal_ttl_hours"],
                 e["checkpoint_interval_seconds"], len(e["org_ids"])] + e["org_ids"]
    else:
        raise ValueError("unknown selector")
    return "0x" + "".join(_word(w) for w in words)


@app.post("/rpc")
async def rpc(request: Request):
    if state.node_down:
        return JSONResponse({"error": "down"}, status_code=503)
    b = await request.json()
    if b["method"] == "eth_blockNumber":
        result = hex(state.block + state.node_ahead)
    elif b["method"] == "eth_call" and b["params"][0]["to"].lower() == state.contract_address.lower():
        result = _eth_call(b["params"][0]["data"])
    else:
        return {"jsonrpc": "2.0", "id": b.get("id"), "error": {"code": -32601, "message": "not supported"}}
    return {"jsonrpc": "2.0", "id": b.get("id"), "result": result}
