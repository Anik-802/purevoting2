"""Voter endpoints: redeem and ballots (core-api "voter" tag, D-07).

The caller (tx.run_election) holds the per-election lock and has already performed the boundary
transitions due now, so a request with t >= T only proceeds after the commit / close at T (README "Checkpoints").
"""
import base64
import binascii

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import elgamal, hashing, ledger, timeutil
from app.elections import Refused
from app.errors import Problem
from app.ids import random_id


def _is_open(e: dict, t: int) -> bool:
    return e["status"] == "OPEN" and t < e["window_end"]


def redeem(conn, e: dict, token: str, nullifier_hash: str, t: int) -> None:
    """ONE transaction: lock the token row → USED → nullifier into the ballot schema → TOKEN_USED.
    No column links the token and the nullifier."""
    token_hash = hashing.H(token)
    if not _is_open(e, t):
        raise Refused(409, "ELECTION_NOT_OPEN", subject_hash=token_hash)
    row = conn.execute(text("SELECT used_checkpoint, revoked_checkpoint FROM credential.token_status "
                            "WHERE election_id = :e AND token_hash = :h FOR UPDATE"),
                       {"e": e["election_id"], "h": token_hash}).first()
    if row is None or row.revoked_checkpoint is not None:
        raise Refused(404, "TOKEN_INVALID", subject_hash=token_hash)   # unknown and revoked look the same
    if row.used_checkpoint is not None:
        raise Refused(409, "TOKEN_ALREADY_USED", subject_hash=token_hash)
    k = e["open_checkpoint"]
    conn.execute(text("UPDATE credential.token_status SET used_checkpoint = :k WHERE election_id = :e "
                      "AND token_hash = :h"), {"k": k, "e": e["election_id"], "h": token_hash})
    try:
        with conn.begin_nested():
            conn.execute(text("SELECT ballot.register_nullifier(:e, :n, :k)"),
                         {"e": e["election_id"], "n": nullifier_hash, "k": k})
    except IntegrityError:
        raise Refused(409, "NULLIFIER_ALREADY_REGISTERED", subject_hash=nullifier_hash) from None
    ledger.emit_voter(conn, e, "TOKEN_USED", {"token_hash": token_hash}, t)


def cast(conn, e: dict, credential: str, sealed_b64: str, t: int) -> tuple[int, dict]:
    """Returns (201 | 200, BallotReceipt). Idempotency is checked before the window."""
    try:
        sealed = base64.b64decode(sealed_b64, validate=True)
    except (binascii.Error, ValueError):
        raise Problem(400, "VALIDATION_ERROR", "sealed_ballot is not base64") from None
    if not sealed or len(sealed) > 3072:
        raise Problem(400, "VALIDATION_ERROR", "sealed_ballot size")
    eid = e["election_id"]
    nullifier_hash = hashing.H(credential)
    ballot_hash = hashing.H(sealed)
    receipt_hash = hashing.receipt_hash(eid, nullifier_hash, ballot_hash)

    existing = conn.execute(text("SELECT ballot_hash, receipt_hash FROM ballot.ballot_receipts "
                                 "WHERE election_id = :e AND nullifier_hash = :n"),
                            {"e": eid, "n": nullifier_hash}).first()
    if existing is not None:
        if existing.ballot_hash == ballot_hash:
            return 200, receipt(conn, existing.receipt_hash)
        raise Refused(409, "CREDENTIAL_ALREADY_USED", subject_hash=nullifier_hash)
    registered = conn.execute(text("SELECT 1 FROM ballot.credential_issuance WHERE election_id = :e "
                                   "AND nullifier_hash = :n"), {"e": eid, "n": nullifier_hash}).first()
    if registered is None:
        raise Refused(403, "CREDENTIAL_UNKNOWN", subject_hash=nullifier_hash)
    if not _is_open(e, t):
        raise Refused(409, "ELECTION_NOT_OPEN", subject_hash=nullifier_hash)
    if e["ballot_scheme"] == "ELGAMAL_ED25519_V1" and elgamal.verify_ballot(
            sealed, bytes(e["public_key"]), eid, nullifier_hash, len(e["candidates"])) is None:
        # D-30: a ballot that is not one 1 and the rest 0 never enters (its proofs bind it to this credential)
        raise Problem(400, "VALIDATION_ERROR", "sealed_ballot proofs do not verify")

    payload = {"nullifier_hash": nullifier_hash, "ballot_hash": ballot_hash, "receipt_hash": receipt_hash}
    # the ballot row stores its BALLOT_RECEIVED event_id; row and event are written in this one transaction
    event_id = random_id("evr")
    bucket = timeutil.floor_bucket(t, e["time_bucket_seconds"])
    outcome = conn.execute(text("SELECT ballot.cast_ballot(:e, :n, :bh, :sb, :rh, :k, :bucket, :ev)"),
                           {"e": eid, "n": nullifier_hash, "bh": ballot_hash, "sb": sealed, "rh": receipt_hash,
                            "k": e["open_checkpoint"], "bucket": timeutil.to_dt(bucket), "ev": event_id}).scalar_one()
    if outcome == "RETRY":     # a concurrent identical request won the race
        return 200, receipt(conn, receipt_hash)
    if outcome == "CONFLICT":
        raise Refused(409, "CREDENTIAL_ALREADY_USED", subject_hash=nullifier_hash)
    if outcome == "UNKNOWN":
        raise Refused(403, "CREDENTIAL_UNKNOWN", subject_hash=nullifier_hash)
    ledger.emit_voter(conn, e, "BALLOT_RECEIVED", payload, t, event_id=event_id)
    return 201, {"receipt_hash": receipt_hash, "status": "RECEIVED"}


def receipt(conn, receipt_hash: str) -> dict:
    row = conn.execute(text("SELECT r.event_id, o.status FROM ballot.ballot_receipts r "
                            "LEFT JOIN credential.outbox o ON o.event_id = r.event_id "
                            "WHERE r.receipt_hash = :h"), {"h": receipt_hash}).first()
    if row is None:
        raise Problem(404, "NOT_FOUND")
    out = {"receipt_hash": receipt_hash, "status": "RECEIVED"}
    if row.status == "REJECTED":
        out["status"] = "LEDGER_REJECTED"
    elif row.status in ("ACCEPTED", "DUPLICATE"):
        out["anchor_event_id"] = row.event_id   # resolved by the caller (Gateway GET /v1/events/{id})
    return out
