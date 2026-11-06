"""Development ONLY. Extends Core's in-memory fake for a local HTTP smoke.

No blockchain, real anchors, signature checks or independent ground truth.
Not used by the production worker or either November 19 demo stack.
Run with the Core interpreter; see rules/README_WORKER.md. Bind to loopback.
"""
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.hashing import Hc
from app.ids import random_id
from devtools import fake_gateway as base

app = FastAPI(title="Rules development smoke Gateway (not a blockchain)")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.post("/v1/events")
async def submit(request: Request):
    reply = await base.submit(request)
    if isinstance(reply, dict):
        for outcome in reply["results"]:
            if outcome["status"] != "ACCEPTED":
                continue
            e = base.state.events[outcome["event_id"]]
            # This stand-in commits one leaf per batch; never assign exact voter timestamps.
            e["anchor"].update(anchor_id=random_id("anc"), merkle_root=Hc({k: v for k, v in e.items() if k != "anchor"}))
            if e["type"] not in ("TOKEN_USED", "BALLOT_RECEIVED"):
                e["received_at"] = now()
                outcome["tx_hash"] = e["anchor"]["tx_hash"]
    return reply


@app.get("/v1/events")
def events(election_id: str, checkpoint_max: int | None = None, limit: int = 100, cursor: str | None = None):
    ev = [e for e in base.state.events.values() if "anchor" in e and e["election_id"] == election_id
          and (checkpoint_max is None or e["checkpoint"] <= checkpoint_max)]
    ev.sort(key=lambda e: (e["occurred_at"], e["event_id"]))
    i = int(cursor or 0)
    return {"items": ev[i:i+limit], "next_cursor": str(i+limit) if i+limit < len(ev) else None,
            "last_core_seq": base.state.last_seq.get(election_id, 0)}


@app.get("/v1/rejections")
def rejections(election_id: str, checkpoint_max: int | None = None):
    # The upstream fake keeps refused events without the rejection metadata. Refuse to fake complete evidence.
    if any(e["election_id"] == election_id and "anchor" not in e and
           (checkpoint_max is None or e["checkpoint"] <= checkpoint_max) for e in base.state.events.values()):
        return JSONResponse({"detail":"rejected-event audit trail unavailable in test double"}, status_code=503)
    return {"items": [], "next_cursor": None}


@app.post("/rpc")
async def rpc(request: Request):
    q = await request.json()
    if q.get("method") == "eth_chainId" and not base.state.node_down:
        return {"jsonrpc":"2.0", "id":q["id"], "result":hex(int(base.state.chain_id, 0))}
    return await base.rpc(request)


app.mount("/", base.app)
