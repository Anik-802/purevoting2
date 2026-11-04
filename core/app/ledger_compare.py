"""Outbox comparison (core-web.md "Outbox", threat model I12): the Gateway account could inject an unsigned Core event,
store one with other fields, or drop one; Core cannot prevent it (D-26), only detect it.

Every pass reads this election's outbox first, then every page of Gateway GET /v1/events (an injected event may carry
any occurred_at, so no cursor is kept between passes). Each listed event is Core's unless it is a Gateway event type
with `source: gateway`; a Core event must
- have a well-formed event_id and be in this election's outbox, not as a row the Gateway answered REJECTED;
- equal the outbox event as submitted: every field except the Gateway's `received_at` and `anchor` (the leaf_hash
  scope of hashing.md), so type, seq, checkpoint, time, actor, payload and payload_hash are all covered.
In the other direction, an outbox row answered ACCEPTED / DUPLICATE ("stored") must be listed; it is reported only when
it is missing in two passes in a row, so a listing that lags by one pass is not a mismatch.

An event delivered between the two reads is listed but missing from the first outbox read; those rows are read again.
A new problem → OUTBOX_MISMATCH alert (Core's open checkpoint; a later problem is appended to the same alert) and FIFO
stop. A problem is identified by the listed event's ID (or list position) and content, so the same problem does not
stop the FIFO again (a FIFO resumed by the operators stays resumed for it), while another injected or changed event does.
The listing unreadable for 90 s → LEDGER_UNAVAILABLE alert (the FIFO keeps running: an outage is not a mismatch).
State is in memory: after a restart every event is compared again and known problems are reported again.
Limits: an equivocating Gateway that lists different events to different keys is not detected here; a pass reads every
election with an outbox, including closed ones; a CONFLICT row is reported both as LEDGER_CONFLICT and here; the
problems already reported must move to the DB when outbox:resume is implemented (a restart re-stops a resumed FIFO).
"""
import json
import logging
import re
from collections import Counter
from functools import lru_cache

from sqlalchemy import text

from app import timeutil
from app.db import get_engine
from app.gateway import GatewayUnavailable, get_gateway
from app.hashing import Hc
from app.outbox import _stop
from app.schemas import contracts_dir
from app.store import load_election, raise_alert

log = logging.getLogger("core.ledger_compare")

PERIOD = 30                 # seconds between passes (main.py loop)
UNREADABLE_ALERT = 90       # seconds of unreadable listing before LEDGER_UNAVAILABLE
_ID = re.compile(r"^ev[tr]_[0-9A-HJKMNP-TV-Z]{26}$")
_NOT_LEAF = ("received_at", "anchor")    # added by the Gateway (hashing.md leaf_hash)

_equal: dict[str, dict[str, str]] = {}   # election_id -> {event_id: Hc of the listed event found equal}
_reported: dict[str, set[str]] = {}      # election_id -> problems already alerted
_absent: dict[str, set[str]] = {}        # election_id -> accepted event_ids missing from the previous listing
_unreadable: dict[str, int] = {}         # election_id -> first failed listing time


@lru_cache
def _schema() -> tuple[frozenset, frozenset, frozenset]:
    """(event types, Gateway event types, LedgerEventInput field names) from contracts/ledger-events.schema.json."""
    d = json.loads((contracts_dir() / "ledger-events.schema.json").read_text(encoding="utf-8"))["$defs"]
    return (frozenset(d["EventType"]["enum"]), frozenset(d["GatewayEventType"]["enum"]),
            frozenset(d["LedgerEventInput"]["properties"]))


def _name(ev, i=None) -> str:
    """How the alert names an event: its type, plus the event_id for a system event. Voter event IDs and hashes never
    leave the DB (README PII); a value the Gateway sent is copied only if it is a known type or a well-formed evt_ ID."""
    t = ev.get("type") if isinstance(ev, dict) else None
    t = t if isinstance(t, str) and t in _schema()[0] else "unknown type"
    i = i if i is not None else (ev.get("event_id") if isinstance(ev, dict) else None)
    return f"{t} {i}" if isinstance(i, str) and i.startswith("evt_") and _ID.match(i) else t


def _leaf(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in _NOT_LEAF}


def _differs(listed: dict, sent: dict) -> str:
    fields = _schema()[2]
    names = sorted({k if k in fields else "other field" for k in set(listed) | set(sent) if listed.get(k) != sent.get(k)})
    return "differs: " + (", ".join(names) or "encoding")   # equal in Python, different canonical JSON


def _check(ev, rows: dict, seen: dict, listed: set) -> str | None:
    """None if the listed event is fine, else the problem (without the event's name)."""
    if not isinstance(ev, dict):
        return "not an object"
    if ev.get("source") == "gateway" and ev.get("type") in _schema()[1]:
        return None
    i = ev.get("event_id")
    if not (isinstance(i, str) and _ID.match(i)):
        return "malformed event_id"
    listed.add(i)
    row = rows.get(i)
    if row is not None and row["status"] == "REJECTED":
        return "stored although answered REJECTED"
    leaf = _leaf(ev)
    h = Hc(leaf)
    if seen.get(i) == h:
        return None
    if row is None:
        return "not in the outbox"
    if h != Hc(row["event"]):
        return _differs(leaf, row["event"])
    seen[i] = h
    return None


def _key(ev, position: int) -> str:
    """What makes a problem new: the listed event's ID (or list position) and content, not the alert wording, so a
    second injected voter event or a second change of the same event is a new problem."""
    try:
        i = ev.get("event_id") if isinstance(ev, dict) else None
        i = i if isinstance(i, str) and _ID.match(i) else f"#{position}"
        return f"{i}|{Hc(_leaf(ev)) if isinstance(ev, dict) else Hc(ev)}"
    except Exception:   # noqa: BLE001
        return f"#{position}|unreadable"


def compare(election_id: str) -> list[str]:
    """One pass for one election; returns the problems found (new ones are alerted and stop the FIFO)."""
    now = timeutil.clock.now()
    engine = get_engine()
    with engine.connect() as conn:          # the outbox first: a row answered after the listing is never "REJECTED"
        rows = {r["event_id"]: r for r in conn.execute(text(
            "SELECT event_id, status, event FROM credential.outbox WHERE election_id = :e"),
            {"e": election_id}).mappings()}
    try:
        ledger = get_gateway().events(election_id)
    except GatewayUnavailable as exc:
        first = _unreadable.setdefault(election_id, now)
        log.info("outbox comparison: listing unreadable: %s", exc)
        if now - first >= UNREADABLE_ALERT:
            with engine.begin() as conn:
                e = load_election(conn, election_id, lock=True)
                raise_alert(conn, election_id, e["open_checkpoint"], "LEDGER_UNAVAILABLE", "core-api", now,
                            "outbox comparison: Gateway GET /v1/events unreadable")
        return []
    _unreadable.pop(election_id, None)

    # events written to the outbox and delivered between the two reads are listed but were not in `rows`: read those
    # rows again (their status may be newer than the listing, so "answered REJECTED" is judged on the first read only)
    late = [ev["event_id"] for ev in ledger if isinstance(ev, dict) and isinstance(ev.get("event_id"), str)
            and _ID.match(ev["event_id"]) and ev["event_id"] not in rows]
    stored = {i for i, r in rows.items() if r["status"] in ("ACCEPTED", "DUPLICATE")}
    if late:
        with engine.connect() as conn:
            for r in conn.execute(text("SELECT event_id, status, event FROM credential.outbox WHERE election_id = :e "
                                       "AND event_id = ANY(:ids)"), {"e": election_id, "ids": late}).mappings():
                rows[r["event_id"]] = dict(r, status="PENDING")

    seen, listed, found = _equal.setdefault(election_id, {}), set(), []   # found: (key, alert text)
    for n, ev in enumerate(ledger):
        try:
            p = _check(ev, rows, seen, listed)
        except Exception:   # noqa: BLE001 — anything the Gateway sent that cannot be compared is a mismatch
            p = "unreadable"
        if p:
            found.append((_key(ev, n), f"{_name(ev)}: {p}"))

    absent = stored - listed
    missing = absent & _absent.get(election_id, set())
    _absent[election_id] = absent
    voter = Counter()
    for i in sorted(missing):
        if i.startswith("evr_"):
            voter[_name(rows[i]["event"], i)] += 1
        else:
            found.append((f"absent|{i}", f"{_name(rows[i]['event'], i)}: accepted but not listed"))
    for t, c in sorted(voter.items()):
        ids = ",".join(sorted(i for i in missing if i.startswith("evr_") and _name(rows[i]["event"], i) == t))
        found.append((f"absent|{t}|{Hc(ids)}", f"{t}: {c} accepted but not listed"))

    reported = _reported.setdefault(election_id, set())
    new = [(k, p) for k, p in found if k not in reported]
    if new:
        detail = "; ".join(dict.fromkeys(p for _, p in new))
        detail = detail if len(detail) <= 500 else detail[:497] + "..."
        with engine.begin() as conn:
            e = load_election(conn, election_id, lock=True)
            k = e["open_checkpoint"]
            raise_alert(conn, election_id, k, "OUTBOX_MISMATCH", "core-api", now, detail, append=True)
            if not e["fifo_stopped"]:
                _stop(conn, e, k, now, "ledger event does not match the outbox")
        reported.update(k for k, _ in new)
    return [p for _, p in found]


def compare_all(only: list[str] | None = None) -> None:
    """Every election with an outbox (an injected event needs no accepted one); not an abandoned one (I12)."""
    with get_engine().connect() as conn:
        ids = [r[0] for r in conn.execute(text(
            "SELECT e.election_id FROM credential.elections e WHERE e.creation_check <> 'MISMATCH' "
            "AND EXISTS (SELECT 1 FROM credential.outbox o WHERE o.election_id = e.election_id)"))]
    for eid in ids:
        if only is not None and eid not in only:
            continue
        try:
            compare(eid)
        except Exception:   # noqa: BLE001 — one election's failure must not stop the others
            log.exception("outbox comparison failed for an election")
