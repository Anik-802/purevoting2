"""Checkpoints and boundary-scheduled transitions (README "Election state machine", "Checkpoints").

`advance` is called with the election lock held, both by the scheduler loop and by every voter request before it is
timestamped, so a request with t >= T always sees the boundary commit / close at T first (boundary serialization).
"""
from sqlalchemy import text

from app import views
from app.hashing import Hc, snapshot_hash
from app.ledger import emit_system
from app.store import jsonb, set_status
from app.timeutil import ceil_bucket, from_dt, iso, to_dt


def commit_checkpoint(conn, e: dict, reason: str, boundary: int) -> dict:
    """Commit the open checkpoint k: snapshot hashes and counts of the six tables as of k, CHECKPOINT_COMMITTED{k},
    then open k+1."""
    k, eid = e["open_checkpoint"], e["election_id"]
    rows = {t: views.read(conn, t, eid, k) for t in views.SNAPSHOT_TABLES}
    tables = [{"table": t, "row_count": len(rows[t]), "snapshot_hash": snapshot_hash(rows[t])}
              for t in views.SNAPSHOT_TABLES]
    counts = {
        "roster_active": sum(r["status"] == "ACTIVE" for r in rows["roster"]),
        "tokens_active": sum(r["status"] != "REVOKED" for r in rows["token_status"]),
        "token_used": sum(r["status"] == "USED" for r in rows["token_status"]),
        "credentials_issued": len(rows["credential_issuance"]),
        "ballots_received": len(rows["ballot_receipts"]),
        "rejected_attempts": len(rows["rejected_attempts"]),
    }
    voter_events = {"TOKEN_USED": sum(r["used_checkpoint"] == k for r in rows["token_status"]),
                    "BALLOT_RECEIVED": sum(r["received_checkpoint"] == k for r in rows["ballot_receipts"])}
    payload = {"checkpoint": k, "reason": reason, "tables": tables, "counts": counts, "voter_events": voter_events}
    event = emit_system(conn, e, "CHECKPOINT_COMMITTED", payload, boundary)
    conn.execute(text("INSERT INTO credential.checkpoints (election_id, checkpoint, reason, boundary, tables, event_id) "
                      "VALUES (:e, :k, :r, :b, CAST(:t AS jsonb), :ev)"),
                 {"e": eid, "k": k, "r": reason, "b": to_dt(boundary), "t": jsonb(tables), "ev": event["event_id"]})
    conn.execute(text("UPDATE credential.elections SET open_checkpoint = :n WHERE election_id = :e"),
                 {"n": k + 1, "e": eid})
    e["open_checkpoint"] = k + 1
    return event


def tables_hash(tables: list[dict]) -> str:
    """chain-interface.md commitCheckpoint tablesHash = Hc(payload.tables) (computed by the Gateway; kept for tests)."""
    return Hc(tables)


def all_voters_have_tokens(conn, eid: str) -> bool:
    missing = conn.execute(text(
        "SELECT count(*) FROM credential.roster r WHERE r.election_id = :e AND r.removed_checkpoint IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM credential.token_status t WHERE t.election_id = r.election_id "
        "                AND t.voter_key = r.voter_key AND t.revoked_checkpoint IS NULL)"), {"e": eid}).scalar_one()
    return missing == 0


def schedule_open(conn, e: dict, now: int) -> None:
    """After a token issue that covers every ACTIVE voter: open at window_start, or at the first bucket boundary
    after the issue if window_start has passed (README state machine, ROSTER_FROZEN → OPEN)."""
    if e["open_at"] is not None or not all_voters_have_tokens(conn, e["election_id"]):
        return
    at = max(e["window_start"], ceil_bucket(now, e["time_bucket_seconds"]))
    conn.execute(text("UPDATE credential.elections SET open_at = :t WHERE election_id = :e"),
                 {"t": to_dt(at), "e": e["election_id"]})
    e["open_at"] = to_dt(at)


def advance(conn, e: dict, now: int) -> None:
    """Perform every boundary transition due at `now`: open, periodic checkpoints, scheduled close."""
    eid = e["election_id"]
    if (e["status"] == "ROSTER_FROZEN" and e["creation_check"] == "PASSED" and e["open_event_id"] is None
            and e["opened_at"] is None and e["open_at"] is not None):
        t_open = from_dt(e["open_at"])
        if t_open < e["window_end"] and now >= t_open:
            ev = emit_system(conn, e, "ELECTION_OPENED",
                             {"window_start": iso(e["window_start"]), "window_end": iso(e["window_end"])}, t_open)
            conn.execute(text("UPDATE credential.elections SET open_event_id = :ev, opened_at = :t "
                              "WHERE election_id = :e"), {"ev": ev["event_id"], "t": to_dt(t_open), "e": eid})
            e["open_event_id"], e["opened_at"] = ev["event_id"], to_dt(t_open)
            commit_checkpoint(conn, e, "STATE_CHANGE", t_open)

    opening = e["status"] == "OPEN" or (e["status"] == "ROSTER_FROZEN" and e["open_event_id"] is not None)
    if not opening:
        return
    interval, end = e["checkpoint_interval_seconds"], e["window_end"]
    opened = from_dt(e["opened_at"])
    last = conn.execute(text("SELECT max(boundary) FROM credential.checkpoints WHERE election_id = :e "
                             "AND reason = 'PERIODIC'"), {"e": eid}).scalar()
    nxt = (from_dt(last) if last is not None else opened) + interval
    while nxt <= now and nxt < end:
        commit_checkpoint(conn, e, "PERIODIC", nxt)
        nxt += interval
    if now >= end:
        close(conn, e, end)


def close(conn, e: dict, closed_at: int, reason: str = "SCHEDULED", at: int | None = None) -> None:
    """Close (strict: applied at once): ELECTION_CLOSED, STATE_CHANGE, then CLOSE_FINAL (every voter event is already
    in the outbox, since each one is written in its own request's transaction). Scheduled at window_end; ALL_VOTED is
    decided at `at`, with closed_at the next bucket boundary."""
    at = closed_at if at is None else at
    emit_system(conn, e, "ELECTION_CLOSED", {"reason": reason, "closed_at": iso(closed_at)}, at)
    set_status(conn, e, "CLOSED", e["open_checkpoint"])
    commit_checkpoint(conn, e, "STATE_CHANGE", at)
    commit_checkpoint(conn, e, "CLOSE_FINAL", at)
