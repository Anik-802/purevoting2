"""Small DB helpers shared by the services. All functions take an open connection inside a transaction."""
import json

from sqlalchemy import text

from app.errors import Problem
from app.ids import random_id
from app.timeutil import from_dt, to_dt


def load_election(conn, election_id: str, lock: bool = False) -> dict:
    """The election row plus its current status and effective window end. lock=True takes the per-election lock
    that serializes voter acceptance with boundary commits (README "Checkpoints", boundary serialization)."""
    row = conn.execute(text("SELECT * FROM credential.elections WHERE election_id = :e"
                            + (" FOR UPDATE" if lock else "")), {"e": election_id}).mappings().first()
    if row is None:
        raise Problem(404, "NOT_FOUND")
    e = dict(row)
    h = conn.execute(text("SELECT status, window_end FROM credential.election_history WHERE election_id = :e "
                          "ORDER BY checkpoint DESC LIMIT 1"), {"e": election_id}).first()
    e["status"] = h.status
    e["window_end"] = from_dt(h.window_end)
    e["window_start"] = from_dt(e["config_window_start"])
    return e


def set_status(conn, e: dict, status: str, checkpoint: int) -> None:
    """Record a status as of `checkpoint` (election_config view is derived from this history)."""
    conn.execute(text("INSERT INTO credential.election_history (election_id, checkpoint, status, window_end) "
                      "VALUES (:e, :k, :s, :w) ON CONFLICT (election_id, checkpoint) DO UPDATE SET status = :s"),
                 {"e": e["election_id"], "k": checkpoint, "s": status, "w": to_dt(e["window_end"])})
    e["status"] = status


def raise_alert(conn, election_id: str, checkpoint: int, kind: str, source: str, at: int,
                detail: str | None = None, append: bool = False) -> None:
    """Idempotent by (election_id, checkpoint, kind, source). `detail` must already be scrubbed (no hashes, no evr_ IDs).
    append: an existing alert with the same key keeps its time and gets this detail added (up to 500 characters)."""
    on_conflict = ("DO UPDATE SET detail = left(audit_alerts.detail || '; ' || EXCLUDED.detail, 500)" if append
                   else "DO NOTHING")
    conn.execute(text("INSERT INTO credential.audit_alerts (election_id, checkpoint, kind, source, raised_at, detail) "
                      f"VALUES (:e, :k, :kind, :src, :at, :d) ON CONFLICT (election_id, checkpoint, kind, source) "
                      f"{on_conflict}"),
                 {"e": election_id, "k": checkpoint, "kind": kind, "src": source, "at": to_dt(at), "d": detail})


def record_rejection(conn, election_id: str, checkpoint: int, kind: str, subject_hash: str | None = None) -> None:
    """rejected_attempts row: coarse time (checkpoint) only, random key, never a raw token or credential."""
    conn.execute(text("INSERT INTO credential.rejected_attempts (attempt_id, election_id, kind, subject_hash, checkpoint) "
                      "VALUES (:a, :e, :k, :s, :c)"),
                 {"a": random_id("att"), "e": election_id, "k": kind, "s": subject_hash, "c": checkpoint})


def claim_idempotency_key(conn, scope: str, key: str, body_hash: str, now: int, ttl: int | None):
    """Record an Idempotency-Key in the caller's transaction. Returns None when the key is new (the caller does the
    work and stores its response), else the stored row; a different body under the same key → 409. A concurrent
    request with the same key waits on the primary key until the first one commits. ttl=None: the key never expires."""
    new = conn.execute(text("INSERT INTO credential.idempotency (scope, key, body_hash, created_at) "
                            "VALUES (:s, :k, :b, :t) ON CONFLICT DO NOTHING RETURNING key"),
                       {"s": scope, "k": key, "b": body_hash, "t": to_dt(now)}).first()
    if new is not None:
        return None
    row = conn.execute(text("SELECT body_hash, response, created_at FROM credential.idempotency "
                            "WHERE scope = :s AND key = :k FOR UPDATE"), {"s": scope, "k": key}).first()
    if ttl is not None and from_dt(row.created_at) + ttl <= now:
        conn.execute(text("UPDATE credential.idempotency SET body_hash = :b, response = NULL, created_at = :t "
                          "WHERE scope = :s AND key = :k"), {"s": scope, "k": key, "b": body_hash, "t": to_dt(now)})
        return None
    if row.body_hash != body_hash:
        raise Problem(409, "CONFLICT", "Idempotency-Key reused with a different body")
    return row


def store_idempotent_response(conn, scope: str, key: str, response) -> None:
    conn.execute(text("UPDATE credential.idempotency SET response = CAST(:r AS jsonb) WHERE scope = :s AND key = :k"),
                 {"s": scope, "k": key, "r": jsonb(response)})


def jsonb(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
