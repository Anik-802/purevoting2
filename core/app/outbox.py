"""Outbox worker (README "Consistency model", Gateway POST /v1/events "Delivery order").

Release unit = time bucket: once bucket b has ended, send its voter events (shuffled) and then its system events in seq
order; the next request goes out only after every event of the previous one has a final outcome. Until the creation
check has PASSED, nothing but ELECTION_CREATED is delivered.
"""
import logging
import random

from sqlalchemy import text

from app import tally, timeutil
from app.db import get_engine
from app.gateway import GatewayRefused, GatewayUnavailable, get_gateway
from app.store import load_election, raise_alert, set_status
from app.timeutil import from_dt

log = logging.getLogger("core.outbox")

MAX_BATCH = 500
FINAL = ("ACCEPTED", "DUPLICATE", "REJECTED", "CONFLICT")
STRICT_KEPT = ("ROSTER_FROZEN", "ELECTION_CLOSED", "TALLY_PUBLISHED")
STOP_ON_REJECT = ("ROSTER_AMENDED", "TOKEN_BATCH_ISSUED", "TOKEN_REVOKED", "ADMIN_ACTION_EXECUTED",
                  "ADMIN_ACTION_FAILED", "ELECTION_VOIDED")
# WINDOW_CHANGED (week 5): a rejected extension keeps the old end and a rejected shortening keeps the shorter end,
# both with an alert and without stopping the FIFO (README "Consistency model")

# per-election retry backoff, kept in memory (outbox voter rows must not carry exact times)
# election_id -> (next_try_at, delay, retry_later_since or None)
_backoff: dict[str, tuple[int, int, int | None]] = {}
FAST_RETRY = 30   # RETRY_LATER: every 1 s for the first 30 s (a block + D confirmations), then exponential (v1.1)


def _delay(eid: str, now: int, retry_later: bool = False) -> None:
    """Gateway unavailable / refused: exponential 1 s → 60 s. RETRY_LATER: 1 s for FAST_RETRY seconds from the first
    RETRY_LATER of the current run, then exponential up to 60 s (contracts README "Timeouts / retries")."""
    _, d, since = _backoff.get(eid, (0, 0, None))
    if not retry_later:
        since = None
    elif since is None:
        since, d = now, 0
    if retry_later and now - since < FAST_RETRY:
        d = 1
    else:
        d = min(60, max(1, d * 2))
    _backoff[eid] = (now + d, d, since)


def next_unit(conn, e: dict, now: int) -> list[dict]:
    """The events to send next for this election, in delivery order (empty if nothing is due)."""
    b = e["time_bucket_seconds"]
    first = conn.execute(text("SELECT min(bucket_start) FROM credential.outbox WHERE election_id = :e "
                              "AND status = 'PENDING'"), {"e": e["election_id"]}).scalar()
    if first is None or from_dt(first) + b > now:
        return []
    rows = conn.execute(text("SELECT event_id, kind, seq, event FROM credential.outbox WHERE election_id = :e "
                             "AND status = 'PENDING' AND bucket_start = :b"),
                        {"e": e["election_id"], "b": first}).mappings().all()
    voter = [dict(r) for r in rows if r["kind"] == "voter"]
    random.SystemRandom().shuffle(voter)
    system = sorted((dict(r) for r in rows if r["kind"] == "system"), key=lambda r: r["seq"])
    unit = voter + system
    if e["creation_check"] != "PASSED":
        unit = [r for r in unit if r["event"]["type"] == "ELECTION_CREATED"]
    return unit


def deliver(election_id: str) -> bool:
    """One delivery step for one election. Returns True if something was sent."""
    now = timeutil.clock.now()
    nt, _, _ = _backoff.get(election_id, (0, 0, 0))
    if now < nt:
        return False
    engine = get_engine()
    with engine.begin() as conn:
        e = load_election(conn, election_id)
        if e["fifo_stopped"]:
            return False
        unit = next_unit(conn, e, now)
    if not unit:
        return False
    # a unit larger than 500 events goes out as consecutive requests; the next one only after every event of the
    # previous one has a final outcome
    for i in range(0, len(unit), MAX_BATCH):
        chunk = unit[i:i + MAX_BATCH]
        try:
            results = get_gateway().submit_events([r["event"] for r in chunk])
        except GatewayRefused as exc:
            # 4xx: Core sent something the Gateway cannot take (schema, key); retrying alone will not fix it
            log.error("gateway refused the request: %s", exc)
            with engine.begin() as conn:
                raise_alert(conn, election_id, chunk[0]["event"]["checkpoint"], "LEDGER_UNAVAILABLE", "core-api",
                            now, f"Gateway answered {exc}")
            _delay(election_id, now)
            return False
        except GatewayUnavailable as exc:
            log.warning("gateway unavailable: %s", exc)
            _delay(election_id, now)
            return False
        by_id = {r["event_id"]: r for r in results}
        retry = False
        with engine.begin() as conn:
            e = load_election(conn, election_id, lock=True)
            for row in chunk:
                res = by_id.get(row["event_id"], {"status": "RETRY_LATER"})
                if res["status"] in FINAL:
                    apply_outcome(conn, e, row["event"], res, now)
                else:
                    retry = True
        if retry:
            _delay(election_id, now, retry_later=True)
            return True
        if e["fifo_stopped"]:     # an outcome in this chunk stopped the FIFO: send nothing more
            return True
    _backoff.pop(election_id, None)
    return True


def apply_outcome(conn, e: dict, event: dict, res: dict, now: int) -> None:
    status, type_, k = res["status"], event["type"], event["checkpoint"]
    conn.execute(text("UPDATE credential.outbox SET status = :s, reject_code = :rc, block_number = :bn "
                      "WHERE event_id = :id"),
                 {"s": status, "rc": res.get("reject_code"), "bn": res.get("block_number"), "id": event["event_id"]})
    eid = e["election_id"]
    # alert details name system events only; voter-side event IDs and hashes never leave the DB (README PII)
    what = type_ if event["event_id"].startswith("evr_") else f"{type_} {event['event_id']}"

    if status in ("ACCEPTED", "DUPLICATE"):
        if type_ == "ELECTION_CREATED":
            conn.execute(text("UPDATE credential.elections SET created_block = :b WHERE election_id = :e"),
                         {"b": res.get("block_number"), "e": eid})
            e["created_block"] = res.get("block_number")   # the scheduler runs the creation check from here
        elif type_ == "ELECTION_OPENED":
            set_status(conn, e, "OPEN", k)
            conn.execute(text("UPDATE credential.elections SET open_event_id = NULL WHERE election_id = :e"), {"e": eid})
            e["open_event_id"] = None
        elif type_ == "TALLY_PUBLISHED":
            tally.on_published(conn, e, now)      # the TALLY checkpoint follows only an ACCEPTED TALLY_PUBLISHED
        elif type_ == "ELECTION_FINALIZED":
            tally.on_finalized(conn, e)           # permissive: FINALIZED only once ACCEPTED
        return

    if status == "CONFLICT":
        raise_alert(conn, eid, k, "LEDGER_CONFLICT", "core-api", now, what)
        _stop(conn, e, k, now, what)
        return

    # REJECTED: one rule per event type (README "Consistency model")
    code = res.get("reject_code")
    if type_ == "ELECTION_CREATED":
        conn.execute(text("UPDATE credential.elections SET ledger_rejected = true WHERE election_id = :e"), {"e": eid})
        raise_alert(conn, eid, k, "TRANSITION_REJECTED", "core-api", now, f"{what} {code}")
    elif type_ == "ELECTION_FINALIZED":
        # permissive: stays TALLIED; finalize can be requested again once the cause is fixed (no checkpoint follows,
        # so a repeated refusal is appended to the same alert instead of being dropped)
        raise_alert(conn, eid, k, "TRANSITION_REJECTED", "core-api", now, f"{what} {code}", append=True)
    elif type_ == "ELECTION_OPENED":
        # permissive: reverted, no ballots are accepted
        conn.execute(text("UPDATE credential.elections SET open_event_id = NULL WHERE election_id = :e"), {"e": eid})
        e["open_event_id"] = None
        raise_alert(conn, eid, k, "TRANSITION_REJECTED", "core-api", now, f"{what} {code}")
    elif type_ in STRICT_KEPT or type_ == "WINDOW_CHANGED":
        raise_alert(conn, eid, k, "TRANSITION_REJECTED", "core-api", now, f"{what} {code}")
    elif type_ == "CHECKPOINT_COMMITTED":
        raise_alert(conn, eid, k, "CHECKPOINT_LEDGER_REJECTED", "core-api", now, f"{what} {code}")
    elif type_ == "BALLOT_RECEIVED":
        raise_alert(conn, eid, k, "BALLOT_EXCLUDED", "core-api", now, code)
    elif type_ == "TOKEN_USED":
        pass   # the token stays USED and its ballot still counts; R5's row-to-ledger match reports it
    elif type_ in STOP_ON_REJECT:
        _stop(conn, e, k, now, f"{what} {code}")


def _stop(conn, e: dict, k: int, now: int, detail: str) -> None:
    conn.execute(text("UPDATE credential.elections SET fifo_stopped = true WHERE election_id = :e"),
                 {"e": e["election_id"]})
    e["fifo_stopped"] = True
    raise_alert(conn, e["election_id"], k, "FIFO_STOPPED", "core-api", now, detail)


def deliver_all(only: list[str] | None = None) -> None:
    with get_engine().connect() as conn:
        ids = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT election_id FROM credential.outbox WHERE status = 'PENDING'"))]
    for eid in ids:
        if only is not None and eid not in only:
            continue
        try:
            while deliver(eid):
                pass
        except Exception:   # noqa: BLE001 — one election's failure must not stop the others
            log.exception("outbox delivery failed for an election")
