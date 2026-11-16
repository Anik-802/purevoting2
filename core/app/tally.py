"""Tally and finalization (core-api runTally / finalizeElection, README "Election state machine").

CLOSED → TALLIED is strict: TALLIED is applied at once and kept even if the Gateway rejects TALLY_PUBLISHED (alert); the
TALLY checkpoint is committed only after TALLY_PUBLISHED is ACCEPTED, and from it on the election private key is shown
to the Rule Engine and staff (views.election_config). TALLIED → FINALIZED is permissive: FINALIZED only after
ELECTION_FINALIZED is ACCEPTED. The tally itself is the TALLY_PUBLISHED payload in the outbox (no second copy).
"""
import json

import nacl.exceptions
import nacl.public
from sqlalchemy import text

from app import elgamal, hashing, ledger, lifecycle
from app.elections import Refused
from app.errors import Problem
from app.gateway import GatewayUnavailable, get_gateway
from app.store import jsonb, raise_alert, set_status

ACCEPTED = ("ACCEPTED", "DUPLICATE")
RULES = ("R1", "R2", "R3", "R4", "R5")
CLOSED_STATUSES = ("NOT_REQUIRED", "RESOLVED", "SUPERSEDED")


def _special(conn, eid: str, reason: str):
    """(checkpoint, outbox status of its CHECKPOINT_COMMITTED) of the checkpoint committed with `reason`, or None."""
    return conn.execute(text(
        "SELECT c.checkpoint, o.status FROM credential.checkpoints c JOIN credential.outbox o ON o.event_id = c.event_id "
        "WHERE c.election_id = :e AND c.reason = :r"), {"e": eid, "r": reason}).first()


def _published(conn, eid: str):
    return conn.execute(text("SELECT event FROM credential.outbox WHERE election_id = :e "
                             "AND event->>'type' = 'TALLY_PUBLISHED'"), {"e": eid}).first()


def to_api(e: dict, event: dict) -> dict:
    """core-api Tally from the TALLY_PUBLISHED event."""
    p = event["payload"]
    return {"election_id": e["election_id"], "final_checkpoint": p["final_checkpoint"],
            "ballot_count": p["ballot_count"], "invalid_ballots": p["invalid_ballots"],
            "invalid_ballot_hashes": p["invalid_ballot_hashes"], "excluded_ballots": p["excluded_ballots"],
            "results": p["results"], "ballot_set_root": p["ballot_set_root"], "result_hash": p["result_hash"],
            "published_at": event["occurred_at"]}


def get(conn, e: dict) -> dict:
    row = _published(conn, e["election_id"])
    if row is None:
        raise Problem(404, "NOT_FOUND", "no tally yet")
    return to_api(e, row.event)


def valid_choice(plaintext: bytes, candidate_ids: set[str]) -> str | None:
    """The candidate_id of a valid ballot, else None. Valid = UTF-8 JSON object with exactly one key "candidate_id"
    whose value is one of the election's candidate_ids, compared after parsing (whitespace and key order do not
    matter); a duplicated key is invalid, since a parser would silently keep only one of the values."""
    pairs_seen: list[list] = []

    def keep_pairs(pairs):
        pairs_seen.append(pairs)
        return dict(pairs)
    try:
        obj = json.loads(plaintext.decode("utf-8"), object_pairs_hook=keep_pairs)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or len(pairs_seen[-1]) != 1 or list(obj) != ["candidate_id"]:
        return None
    cid = obj["candidate_id"]
    return cid if isinstance(cid, str) and cid in candidate_ids else None


def _accepted(conn, e: dict, final_k: int) -> tuple[list[str], list[tuple[dict, bytes | None]], int]:
    """(ballot_hash of every ACCEPTED BALLOT_RECEIVED with checkpoint <= final_k, [(payload, sealed bytes, or None when
    the row does not match its event or its hash)] in ballot_hash order, number of REJECTED ones)."""
    eid = e["election_id"]
    events = conn.execute(text(
        "SELECT event_id, status, event->'payload' AS p FROM credential.outbox WHERE election_id = :e "
        "AND event->>'type' = 'BALLOT_RECEIVED' AND (event->>'checkpoint')::int <= :k"), {"e": eid, "k": final_k}).all()
    rows = {r.event_id: r for r in conn.execute(text(
        "SELECT event_id, nullifier_hash, ballot_hash, receipt_hash, sealed_ballot FROM ballot.ballot_receipts "
        "WHERE election_id = :e"), {"e": eid})}
    accepted, matched, excluded = [], [], 0
    for ev in events:
        if ev.status not in ACCEPTED:
            excluded += ev.status == "REJECTED"
            continue
        p = ev.p
        accepted.append(p["ballot_hash"])
        row = rows.get(ev.event_id)
        ok = (row is not None and (row.nullifier_hash, row.ballot_hash, row.receipt_hash)
              == (p["nullifier_hash"], p["ballot_hash"], p["receipt_hash"])
              and hashing.H(bytes(row.sealed_ballot)) == p["ballot_hash"])
        matched.append((p, bytes(row.sealed_ballot) if ok else None))
    return accepted, sorted(matched, key=lambda m: m[0]["ballot_hash"]), excluded


def _candidate_ids(e: dict) -> list[str]:
    return sorted(c["candidate_id"] for c in e["candidates"])


def _sealed_box_votes(e: dict, matched: list) -> tuple[dict, list[str]]:
    box = nacl.public.SealedBox(nacl.public.PrivateKey(bytes(e["private_key"])))
    candidate_ids = set(_candidate_ids(e))
    votes, invalid = {cid: 0 for cid in candidate_ids}, []
    for p, sealed in matched:
        choice = None
        if sealed is not None:
            try:
                choice = valid_choice(box.decrypt(sealed), candidate_ids)
            except nacl.exceptions.CryptoError:
                choice = None
        if choice is None:
            invalid.append(p["ballot_hash"])
        else:
            votes[choice] += 1
    return votes, invalid


def elgamal_aggregate(e: dict, matched: list) -> tuple[list, int, list[str]]:
    """D-30: (per-candidate aggregate of the ballots whose proofs verify, their number, invalid ballot hashes)."""
    n, Y = len(e["candidates"]), bytes(e["public_key"])
    valid, invalid = [], []
    for p, sealed in matched:
        cts = None if sealed is None else elgamal.verify_ballot(sealed, Y, e["election_id"], p["nullifier_hash"], n)
        if cts is None:
            invalid.append(p["ballot_hash"])
        else:
            valid.append(cts)
    return elgamal.aggregate(valid, n), len(valid), invalid


def _shares(conn, e: dict, agg: list) -> tuple[list[dict], list[str]]:
    """(verified stored shares in org_id order, org_ids still missing)."""
    stored = {r.org_id: r.share for r in conn.execute(text(
        "SELECT org_id, share FROM credential.decryption_shares WHERE election_id = :e"), {"e": e["election_id"]})}
    good, missing = [], []
    for t in e["trustee_keys"]:
        share = stored.get(t["org_id"])
        if share is not None and elgamal.verify_share(share, bytes.fromhex(t["public_key"]), e["election_id"], agg):
            good.append(share)
        else:
            missing.append(t["org_id"])
    return good, missing


def _elgamal_votes(conn, e: dict, matched: list) -> tuple[dict, list[str]]:
    agg, n_valid, invalid = elgamal_aggregate(e, matched)
    ids = _candidate_ids(e)
    if n_valid == 0:
        return {cid: 0 for cid in ids}, invalid
    shares, missing = _shares(conn, e, agg)
    if missing:
        raise Problem(409, "AWAITING_DECRYPTION_SHARES", "decryption shares missing from " + ", ".join(missing))
    return dict(zip(ids, elgamal.combine(agg, shares, n_valid))), invalid


def count(conn, e: dict, final_k: int, now: int) -> dict:
    """TALLY_PUBLISHED payload: one ballot per ACCEPTED BALLOT_RECEIVED with checkpoint <= final_k; REJECTED ones are
    excluded; a row that does not match its event, a hash mismatch, or an undecryptable / invalid plaintext
    (SEALED_BOX_V1) or failing proofs (ELGAMAL_ED25519_V1, where only aggregates are decrypted) is invalid."""
    eid = e["election_id"]
    accepted, matched, excluded = _accepted(conn, e, final_k)
    if e["ballot_scheme"] == "ELGAMAL_ED25519_V1":
        votes, invalid = _elgamal_votes(conn, e, matched)
    else:
        votes, invalid = _sealed_box_votes(e, matched)
    if invalid:
        # no hashes in alert details (README PII); the hashes are in the published tally for review
        raise_alert(conn, eid, e["open_checkpoint"], "BALLOT_EXCLUDED", "core-api", now,
                    f"{len(invalid)} ballot(s) invalid at tally (row mismatch, hash mismatch or invalid ballot)")
    result = {
        "election_id": eid, "final_checkpoint": final_k,
        "results": [{"candidate_id": cid, "votes": votes[cid]} for cid in _candidate_ids(e)],
        "ballot_count": len(accepted) - len(invalid), "invalid_ballots": len(invalid),
        "invalid_ballot_hashes": sorted(invalid), "excluded_ballots": excluded,
        "ballot_set_root": hashing.ballot_set_root(accepted),
    }
    payload = {k: v for k, v in result.items() if k != "election_id"}
    return payload | {"result_hash": hashing.Hc(result)}


def _final_checkpoint(conn, e: dict) -> int:
    """The CLOSE_FINAL checkpoint once everything up to it is settled with the ledger; otherwise the refusal."""
    eid = e["election_id"]
    final = _special(conn, eid, "CLOSE_FINAL")
    if final is not None and final.status in ("REJECTED", "CONFLICT"):
        raise Refused(409, "INVALID_STATE_TRANSITION",
                      "the Gateway refused the CLOSE_FINAL checkpoint; the tally cannot be completed (ELECTION_VOID)")
    if final is None or final.status not in ACCEPTED:
        raise Problem(409, "OUTBOX_NOT_DRAINED", "the CLOSE_FINAL checkpoint is not ACCEPTED by the Gateway yet")
    # a stopped FIFO (CONFLICT, outbox mismatch) or a CONFLICT event means Core and the ledger disagree about what was
    # received: a person resolves it first (README "Consistency model"), otherwise counted + invalid + excluded would
    # not add up to the ledger's BALLOT_RECEIVED events (R3)
    if e["fifo_stopped"]:
        raise Problem(409, "OUTBOX_NOT_DRAINED", "the outbox FIFO is stopped; resolve the alert and resume first")
    stuck = conn.execute(text("SELECT count(*) FILTER (WHERE status = 'PENDING') AS pending, "
                              "count(*) FILTER (WHERE status = 'CONFLICT') AS conflict "
                              "FROM credential.outbox WHERE election_id = :e"), {"e": eid}).one()
    if stuck.conflict:
        raise Problem(409, "OUTBOX_NOT_DRAINED", f"{stuck.conflict} event(s) in CONFLICT with the ledger; resolve first")
    if stuck.pending:
        raise Problem(409, "OUTBOX_NOT_DRAINED", f"{stuck.pending} event(s) not yet delivered to the Gateway")
    return final.checkpoint


def run_tally(conn, e: dict, staff, now: int) -> dict:
    """CLOSED → TALLIED. Calling it again when already TALLIED (or later) returns the existing tally."""
    if e["status"] in ("TALLIED", "FINALIZED"):
        return get(conn, e)
    if e["status"] != "CLOSED":
        raise Refused(409, "INVALID_STATE_TRANSITION", "the election is not CLOSED")
    payload = count(conn, e, _final_checkpoint(conn, e), now)
    event = ledger.emit_system(conn, e, "TALLY_PUBLISHED", payload, now, ledger.staff_actor(staff))
    set_status(conn, e, "TALLIED", e["open_checkpoint"])
    return to_api(e, event)


# ---- D-30 decryption shares (ELGAMAL_ED25519_V1) ----

# (election_id, final checkpoint) → (matched, aggregate, valid count, invalid hashes) for the two public endpoints: after
# CLOSE_FINAL is settled nothing they read can change, so anonymous calls never re-verify every ballot. runTally
# always recomputes (count), so a later database edit is still caught there.
_inputs: dict[tuple[str, int], tuple] = {}


def _tally_inputs(conn, e: dict, final_k: int) -> tuple:
    key = (e["election_id"], final_k)
    if key not in _inputs:
        _, matched, _ = _accepted(conn, e, final_k)
        if len(_inputs) >= 8:
            _inputs.pop(next(iter(_inputs)))
        _inputs[key] = (matched, *elgamal_aggregate(e, matched))
    return _inputs[key]

def _require_elgamal(e: dict) -> None:
    if e["ballot_scheme"] != "ELGAMAL_ED25519_V1":
        raise Problem(409, "INVALID_STATE_TRANSITION", "this election uses a sealed box, not decryption shares")


def tally_input(conn, e: dict) -> dict:
    """What an organization checks before it decrypts: every ledger-accepted ballot (bytes null when the row is missing
    or does not match its event), the aggregate Core derived from the valid ones and the shares still missing. The
    trustee tool decides validity itself from these bytes. The ciphertexts reveal nothing without every key."""
    _require_elgamal(e)
    if e["status"] == "CLOSED":
        final_k = _final_checkpoint(conn, e)
    elif e["status"] in ("TALLIED", "FINALIZED"):
        final_k = _special(conn, e["election_id"], "CLOSE_FINAL").checkpoint
    else:
        raise Problem(409, "INVALID_STATE_TRANSITION", "the election is not closed yet")
    matched, agg, n_valid, invalid = _tally_inputs(conn, e, final_k)
    missing = _shares(conn, e, agg)[1] if n_valid else []
    return {"election_id": e["election_id"], "ballot_scheme": e["ballot_scheme"], "final_checkpoint": final_k,
            "public_key": bytes(e["public_key"]).hex(), "trustee_keys": e["trustee_keys"],
            "candidate_ids": _candidate_ids(e),
            "ballots": [{"nullifier_hash": p["nullifier_hash"], "ballot_hash": p["ballot_hash"],
                         "sealed_ballot": None if sealed is None else hashing.b64std(sealed)} for p, sealed in matched],
            "aggregate": [{"a": a.hex(), "b": b.hex()} for a, b in agg], "valid_ballots": n_valid,
            "invalid_ballot_hashes": sorted(invalid), "missing_shares": missing}


def submit_share(conn, e: dict, share: dict) -> dict:
    """Stores an organization's share once its proofs verify against that organization's key in the election."""
    _require_elgamal(e)
    if e["status"] != "CLOSED":
        raise Problem(409, "INVALID_STATE_TRANSITION", "shares are taken while the election is CLOSED")
    keys = {t["org_id"]: bytes.fromhex(t["public_key"]) for t in e["trustee_keys"]}
    org_id = share["org_id"]
    if org_id not in keys:
        raise Problem(422, "VALIDATION_ERROR", "org_id is not a trustee of this election")
    _, agg, n_valid, _ = _tally_inputs(conn, e, _final_checkpoint(conn, e))
    if n_valid == 0:
        raise Problem(409, "INVALID_STATE_TRANSITION", "no valid ballot, nothing to decrypt")
    if not elgamal.verify_share(share, keys[org_id], e["election_id"], agg):
        raise Problem(422, "VALIDATION_ERROR", "the share's proofs do not verify against this election's aggregate")
    conn.execute(text("INSERT INTO credential.decryption_shares (election_id, org_id, share) "
                      "VALUES (:e, :o, CAST(:s AS jsonb)) ON CONFLICT (election_id, org_id) DO UPDATE SET share = EXCLUDED.share"),
                 {"e": e["election_id"], "o": org_id, "s": jsonb({"org_id": org_id, "shares": share["shares"]})})
    return {"org_id": org_id, "missing_shares": _shares(conn, e, agg)[1]}


def on_published(conn, e: dict, now: int) -> None:
    """TALLY_PUBLISHED ACCEPTED (or DUPLICATE): commit the TALLY checkpoint once."""
    if _special(conn, e["election_id"], "TALLY") is None:
        lifecycle.commit_checkpoint(conn, e, "TALLY", now)


def _finding_rows(conn, eid: str) -> list:
    return conn.execute(text("SELECT finding_id, body, review_status FROM credential.findings WHERE election_id = :e"),
                        {"e": eid}).all()


def check_finalizable(conn, e: dict) -> None:
    """finalizeElection preconditions 1–3 (also enforced by the contract from the ledger finding records)."""
    eid = e["election_id"]
    tally_k = _special(conn, eid, "TALLY")
    if tally_k is None or tally_k.status not in ACCEPTED:
        raise Refused(409, "INVALID_STATE_TRANSITION", "the TALLY checkpoint is not committed and ACCEPTED yet")
    rows = _finding_rows(conn, eid)
    stored = {r.finding_id for r in rows}
    try:
        recorded = get_gateway().finding_records(eid)
    except GatewayUnavailable:
        raise Problem(503, "LEDGER_UNAVAILABLE", "Gateway unreachable; finding records cannot be checked") from None
    missing = [r["finding_id"] for r in recorded if r["finding_id"] not in stored]
    if missing:
        raise Refused(409, "FINDINGS_UNREVIEWED", f"{len(missing)} recorded finding(s) without a stored body")
    # the ledger records are authoritative: rule, checkpoint and the contract's status (finding.schema.json review:
    # "the contract's findingRecord status as exposed by the Gateway; Core does not re-derive the status")
    at_tally = {r["recorded"]["payload"]["rule_id"] for r in recorded
                if r["recorded"]["payload"]["finding_checkpoint"] == tally_k.checkpoint}
    if set(RULES) - at_tally:
        raise Refused(409, "FINDINGS_UNREVIEWED",
                      "no finding at the TALLY checkpoint for " + ", ".join(sorted(set(RULES) - at_tally)))
    db_status = {r.finding_id: r.review_status for r in rows}
    if any(r["contract_status"] == "CONFIRMED_ISSUE" or db_status.get(r["finding_id"]) == "CONFIRMED_ISSUE"
           for r in recorded):
        raise Refused(409, "AUDIT_UNRESOLVED", "a finding is CONFIRMED_ISSUE; only ELECTION_VOID can follow")
    open_ = [r for r in recorded if r["contract_status"] not in CLOSED_STATUSES]
    if open_:
        raise Refused(409, "FINDINGS_UNREVIEWED", f"{len(open_)} FAIL / INCONCLUSIVE finding(s) not resolved")


def finalize(conn, e: dict, staff, now: int) -> None:
    """TALLIED → FINALIZED (applied when ELECTION_FINALIZED is ACCEPTED). Retry-safe."""
    eid = e["election_id"]
    if e["status"] == "FINALIZED":
        return
    if e["status"] != "TALLIED":
        raise Refused(409, "INVALID_STATE_TRANSITION", "the election is not TALLIED")
    sent = conn.execute(text("SELECT status FROM credential.outbox WHERE election_id = :e "
                             "AND event->>'type' = 'ELECTION_FINALIZED' AND status <> 'REJECTED'"), {"e": eid}).first()
    if sent is not None:
        return   # already requested; FINALIZED follows when the Gateway accepts it
    check_finalizable(conn, e)
    result_hash = _published(conn, eid).event["payload"]["result_hash"]
    ledger.emit_system(conn, e, "ELECTION_FINALIZED", {"result_hash": result_hash}, now, ledger.staff_actor(staff))


def on_finalized(conn, e: dict) -> None:
    set_status(conn, e, "FINALIZED", e["open_checkpoint"])

