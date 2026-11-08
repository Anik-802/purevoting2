"""Voter (no login) and auth endpoints."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import elections, voting
from app.auth import Staff, issue_token, verify_password
from app.config import settings
from app.errors import Problem
from app.gateway import GatewayUnavailable, get_gateway
from app.models import BallotIn, LoginIn, RedeemIn
from app.store import load_election
from app.tx import run, run_election

router = APIRouter()


@router.get("/v1/public/elections/{election_id}", tags=["voter"])
def public_election(election_id: str):
    return run(lambda c: elections.to_public(load_election(c, election_id)))


@router.post("/v1/credentials:redeem", tags=["voter"])
def redeem(body: RedeemIn):
    run_election(body.election_id, lambda c, e, t: voting.redeem(c, e, body.token, body.nullifier_hash, t),
                 missing=Problem(404, "TOKEN_INVALID"))
    return {"election_id": body.election_id, "status": "REDEEMED"}


@router.post("/v1/ballots", tags=["voter"])
def ballots(body: BallotIn):
    status, rec = run_election(body.election_id,
                               lambda c, e, t: voting.cast(c, e, body.credential, body.sealed_ballot, t),
                               missing=Problem(403, "CREDENTIAL_UNKNOWN"))
    return JSONResponse(_resolve_anchor(rec), status_code=status)


@router.get("/v1/public/receipts/{receipt_hash}", tags=["voter"])
def receipt(receipt_hash: str):
    return _resolve_anchor(run(lambda c: voting.receipt(c, receipt_hash)))


def _resolve_anchor(rec: dict) -> dict:
    """ANCHORED once the Gateway reports the BALLOT_RECEIVED inside an anchor; otherwise RECEIVED."""
    event_id = rec.pop("anchor_event_id", None)
    if event_id:
        try:
            ev = get_gateway().event(event_id, timeout=1.5)   # voter endpoints answer within 3 s
        except GatewayUnavailable:
            ev = None
        anchor = (ev or {}).get("anchor")
        anchor = anchor if isinstance(anchor, dict) else {}
        if anchor.get("status") == "ANCHORED" and isinstance(anchor.get("anchor_id"), str) \
                and isinstance(anchor.get("tx_hash"), str):   # an incomplete anchor reads as not anchored yet
            rec |= {"status": "ANCHORED", "anchor_id": anchor["anchor_id"], "tx_hash": anchor["tx_hash"]}
    return rec


@router.post("/v1/auth/login", tags=["auth"])
def login(body: LoginIn):
    row = run(lambda c: c.execute(text("SELECT user_id, password_hash, org_id, role, username FROM credential.staff_users "
                                       "WHERE username = :u"), {"u": body.username}).first())
    if row is None or not verify_password(body.password, row.password_hash):
        raise Problem(401, "UNAUTHORIZED")
    staff = Staff(row.user_id, row.role, row.org_id, row.username)
    return {"access_token": issue_token(staff), "expires_in": settings.jwt_ttl_seconds, "user": staff.public()}
