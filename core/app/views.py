"""The seven PII-free views "as of checkpoint k" (core-api ViewRow, README "Checkpoints").

Every field is derived from values stored with the checkpoint in which they changed, so a committed k always gives
the same rows and the same snapshot_hash (hashing.md). No view joins tokens with credentials or ballots.
"""
from sqlalchemy import text

from app import hashing
from app.timeutil import from_dt, iso

SNAPSHOT_TABLES = ("roster", "token_status", "credential_issuance", "ballot_receipts", "admin_actions",
                   "rejected_attempts")
VIEWS = ("election_config",) + SNAPSHOT_TABLES


def _asof(value, k):
    return value if value is not None and value <= k else None


def roster(conn, eid: str, k: int) -> list[dict]:
    rows = conn.execute(text("SELECT voter_key, added_checkpoint, removed_checkpoint FROM credential.roster "
                             "WHERE election_id = :e AND added_checkpoint <= :k ORDER BY voter_key"),
                        {"e": eid, "k": k})
    out = []
    for r in rows:
        removed = _asof(r.removed_checkpoint, k)
        out.append({"view": "roster", "row_key": r.voter_key, "status": "REMOVED" if removed is not None else "ACTIVE",
                    "added_checkpoint": r.added_checkpoint, "removed_checkpoint": removed})
    return out


def token_status(conn, eid: str, k: int) -> list[dict]:
    rows = conn.execute(text("SELECT token_hash, issued_checkpoint, used_checkpoint, revoked_checkpoint "
                             "FROM credential.token_status WHERE election_id = :e AND issued_checkpoint <= :k "
                             "ORDER BY token_hash"), {"e": eid, "k": k})
    out = []
    for r in rows:
        used, revoked = _asof(r.used_checkpoint, k), _asof(r.revoked_checkpoint, k)
        status = "REVOKED" if revoked is not None else "USED" if used is not None else "ISSUED"
        out.append({"view": "token_status", "row_key": r.token_hash, "status": status,
                    "issued_checkpoint": r.issued_checkpoint, "used_checkpoint": used, "revoked_checkpoint": revoked})
    return out


def credential_issuance(conn, eid: str, k: int) -> list[dict]:
    rows = conn.execute(text("SELECT nullifier_hash, issued_checkpoint, used_checkpoint FROM ballot.credential_issuance "
                             "WHERE election_id = :e AND issued_checkpoint <= :k ORDER BY nullifier_hash"),
                        {"e": eid, "k": k})
    return [{"view": "credential_issuance", "row_key": r.nullifier_hash, "issued_checkpoint": r.issued_checkpoint,
             "used_checkpoint": _asof(r.used_checkpoint, k)} for r in rows]


def ballot_receipts(conn, eid: str, k: int) -> list[dict]:
    rows = conn.execute(text("SELECT nullifier_hash, ballot_hash, sealed_ballot, receipt_hash, received_checkpoint, "
                             "received_bucket, event_id FROM ballot.ballot_receipts "
                             "WHERE election_id = :e AND received_checkpoint <= :k ORDER BY nullifier_hash"),
                        {"e": eid, "k": k})
    return [{"view": "ballot_receipts", "row_key": r.nullifier_hash, "ballot_hash": r.ballot_hash,
             "sealed_ballot": hashing.b64std(bytes(r.sealed_ballot)), "receipt_hash": r.receipt_hash,
             "received_checkpoint": r.received_checkpoint, "received_bucket": iso(from_dt(r.received_bucket)),
             "event_id": r.event_id} for r in rows]


def admin_actions(conn, eid: str, k: int) -> list[dict]:
    rows = conn.execute(text(
        "SELECT a.action_id, a.action_type, a.requested_by_org, a.proposal_id, a.target, a.target_hash, "
        "       a.requested_checkpoint, s.status, s.checkpoint AS status_checkpoint "
        "FROM credential.admin_actions a "
        "JOIN LATERAL (SELECT status, checkpoint FROM credential.admin_action_status "
        "              WHERE action_id = a.action_id AND checkpoint <= :k "
        "              ORDER BY checkpoint DESC, array_position(ARRAY['AWAITING_SIGNATURE','PENDING_APPROVAL',"
        "                'APPROVED','REJECTED','EXPIRED','EXECUTED','FAILED'], status) DESC LIMIT 1) s ON true "
        "WHERE a.election_id = :e AND a.requested_checkpoint <= :k ORDER BY a.action_id"), {"e": eid, "k": k})
    return [{"view": "admin_actions", "row_key": r.action_id, "action_type": r.action_type, "status": r.status,
             "requested_by_org": r.requested_by_org, "proposal_id": r.proposal_id, "target": r.target,
             "target_hash": r.target_hash, "requested_checkpoint": r.requested_checkpoint,
             "status_checkpoint": r.status_checkpoint} for r in rows]


def rejected_attempts(conn, eid: str, k: int) -> list[dict]:
    rows = conn.execute(text("SELECT attempt_id, kind, subject_hash, checkpoint FROM credential.rejected_attempts "
                             "WHERE election_id = :e AND checkpoint <= :k ORDER BY attempt_id"), {"e": eid, "k": k})
    return [{"view": "rejected_attempts", "row_key": r.attempt_id, "kind": r.kind, "subject_hash": r.subject_hash,
             "checkpoint": r.checkpoint} for r in rows]


def election_config(conn, eid: str, k: int, reveal_key: bool) -> list[dict]:
    """reveal_key: the caller may see the private key (ruleKey or staff); it is shown only from the TALLY checkpoint on.
    ELGAMAL_ED25519_V1 (D-30) has no private key; its decryption shares are shown from the TALLY checkpoint on."""
    e = conn.execute(text("SELECT * FROM credential.elections WHERE election_id = :e"), {"e": eid}).mappings().one()
    h = conn.execute(text("SELECT status, window_end FROM credential.election_history WHERE election_id = :e "
                          "AND checkpoint <= :k ORDER BY checkpoint DESC LIMIT 1"), {"e": eid, "k": k}).one()
    tally_k = conn.execute(text("SELECT checkpoint FROM credential.checkpoints WHERE election_id = :e "
                                "AND reason = 'TALLY'"), {"e": eid}).scalar()
    show = reveal_key and tally_k is not None and k >= tally_k
    elgamal = e["ballot_scheme"] == "ELGAMAL_ED25519_V1"
    shares = None
    if elgamal and tally_k is not None and k >= tally_k:
        shares = [r.share for r in conn.execute(text("SELECT share FROM credential.decryption_shares "
                                                     "WHERE election_id = :e ORDER BY org_id"), {"e": eid})]
    return [{"view": "election_config", "row_key": "config", "status": h.status, "title": e["title"],
             "candidates": e["candidates"], "config_window_start": iso(from_dt(e["config_window_start"])),
             "config_window_end": iso(from_dt(e["config_window_end"])),
             "window_start": iso(from_dt(e["config_window_start"])), "window_end": iso(from_dt(h.window_end)),
             "time_bucket_seconds": e["time_bucket_seconds"],
             "checkpoint_interval_seconds": e["checkpoint_interval_seconds"], "config_hash": e["config_hash"],
             "public_key": hashing.b64std(bytes(e["public_key"])), "organizations": e["organizations"],
             "approval_threshold": e["approval_threshold"], "proposal_ttl_hours": e["proposal_ttl_hours"],
             "operator_org": e["operator_org"],
             "private_key": hashing.b64std(bytes(e["private_key"])) if show and not elgamal else None,
             "ballot_scheme": e["ballot_scheme"], "trustee_keys": e["trustee_keys"], "decryption_shares": shares}]


_READERS = {"roster": roster, "token_status": token_status, "credential_issuance": credential_issuance,
            "ballot_receipts": ballot_receipts, "admin_actions": admin_actions, "rejected_attempts": rejected_attempts}


def read(conn, view: str, eid: str, k: int, reveal_key: bool = False) -> list[dict]:
    if view == "election_config":
        return election_config(conn, eid, k, reveal_key)
    return _READERS[view](conn, eid, k)
