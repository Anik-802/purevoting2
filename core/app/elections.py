"""Elections, roster and links (core-api "elections" tag)."""
import csv
import hashlib
import io
import time

import nacl.public
import nacl.secret
import nacl.utils
from sqlalchemy import text

from app import hashing, ledger, lifecycle, timeutil
from app.config import settings
from app.errors import Problem
from app.gateway import GatewayUnavailable, get_gateway
from app.ids import random_id, token128, ulid
from app.store import jsonb, load_election, record_rejection, set_status
from app.timeutil import from_dt, iso, parse_iso, to_dt

TRANSITION_TO = {"ELECTION_CREATED": "DRAFT", "ROSTER_FROZEN": "ROSTER_FROZEN", "ELECTION_OPENED": "OPEN",
                 "ELECTION_CLOSED": "CLOSED", "TALLY_PUBLISHED": "TALLIED", "ELECTION_FINALIZED": "FINALIZED",
                 "ELECTION_VOIDED": "VOIDED"}
LEDGER_STATUS = {"PENDING": "PENDING", "ACCEPTED": "ACCEPTED", "DUPLICATE": "ACCEPTED", "REJECTED": "REJECTED",
                 "CONFLICT": "REJECTED"}


def config_hash(e: dict) -> str:
    """hashing.md config_hash (window at creation)."""
    return hashing.Hc({
        "title": e["title"], "candidates": e["candidates"],
        "window_start": iso(e["window_start"]), "window_end": iso(e["window_end"]),
        "time_bucket_seconds": e["time_bucket_seconds"], "checkpoint_interval_seconds": e["checkpoint_interval_seconds"],
        "organizations": e["organizations"], "approval_threshold": e["approval_threshold"],
        "proposal_ttl_hours": e["proposal_ttl_hours"], "operator_org": e["operator_org"],
        "public_key_hash": e["public_key_hash"],
    })


def _signer(s: str) -> str:
    return s.lower() if s.startswith("0x") else s


def create(conn, staff, body: dict, now: int) -> str:
    b, interval = body["time_bucket_seconds"], body["checkpoint_interval_seconds"]
    start, end = parse_iso(body["window_start"]), parse_iso(body["window_end"])
    org_ids = body["org_ids"]
    threshold = body["approval_threshold"]
    if start % b or end % b or end <= start:
        raise Problem(422, "VALIDATION_ERROR", "window must be bucket-aligned and window_end after window_start")
    if interval % b:
        raise Problem(422, "VALIDATION_ERROR", "checkpoint_interval_seconds must be a multiple of time_bucket_seconds")
    if len(set(org_ids)) != len(org_ids) or len(org_ids) < 3 or not 2 <= threshold <= len(org_ids) - 1:
        raise Problem(422, "VALIDATION_ERROR", "need >= 3 distinct orgs and 2 <= approval_threshold <= orgs - 1")

    try:
        registry = {o["org_id"]: o for o in get_gateway().organizations()}
    except GatewayUnavailable:
        raise Problem(503, "LEDGER_UNAVAILABLE", "organization registry not readable; retry") from None
    missing = [o for o in org_ids if o not in registry]
    if missing:
        raise Problem(422, "ORG_NOT_REGISTERED", ", ".join(missing))
    operators = [o for o, item in registry.items() if item.get("is_operator")]
    if len(operators) != 1 or operators[0] not in org_ids:
        raise Problem(422, "VALIDATION_ERROR", "the registry's operatorOrg must be one of org_ids")
    operator = operators[0]
    if staff.org_id != operator:
        raise Problem(403, "FORBIDDEN", "only staff of the operator organization can create elections")

    key = nacl.public.PrivateKey.generate()
    public = bytes(key.public_key)
    candidates = sorted(({"candidate_id": ulid("cand"), "name": c["name"]} for c in body["candidates"]),
                        key=lambda c: c["candidate_id"])
    e = {
        "election_id": ulid("elc"), "title": body["title"], "candidates": candidates,
        "window_start": start, "window_end": end, "time_bucket_seconds": b, "checkpoint_interval_seconds": interval,
        "organizations": sorted(({"org_id": o, "signer": _signer(registry[o]["signer"])} for o in org_ids),
                                key=lambda o: o["org_id"]),
        "approval_threshold": threshold, "proposal_ttl_hours": body.get("proposal_ttl_hours", 24),
        "operator_org": operator, "public_key_hash": hashing.H(public), "open_checkpoint": 0,
    }
    e["config_hash"] = config_hash(e)
    conn.execute(text(
        "INSERT INTO credential.elections (election_id, title, candidates, config_window_start, config_window_end, "
        "time_bucket_seconds, checkpoint_interval_seconds, organizations, approval_threshold, proposal_ttl_hours, "
        "operator_org, public_key, private_key, public_key_hash, config_hash, created_at) "
        "VALUES (:id, :title, CAST(:cand AS jsonb), :ws, :we, :b, :ci, CAST(:orgs AS jsonb), :thr, :ttl, :op, "
        ":pub, :priv, :pkh, :ch, :now)"),
        {"id": e["election_id"], "title": e["title"], "cand": jsonb(candidates), "ws": to_dt(start), "we": to_dt(end),
         "b": b, "ci": interval, "orgs": jsonb(e["organizations"]), "thr": threshold, "ttl": e["proposal_ttl_hours"],
         "op": operator, "pub": public, "priv": bytes(key), "pkh": e["public_key_hash"], "ch": e["config_hash"],
         "now": to_dt(now)})
    conn.execute(text("INSERT INTO credential.seq_counters (election_id) VALUES (:e)"), {"e": e["election_id"]})
    set_status(conn, e, "DRAFT", 0)
    ledger.emit_system(conn, e, "ELECTION_CREATED", {
        "config_hash": e["config_hash"], "window_start": iso(start), "window_end": iso(end),
        "time_bucket_seconds": b, "checkpoint_interval_seconds": interval, "candidate_count": len(candidates),
        "organizations": e["organizations"], "approval_threshold": threshold, "public_key_hash": e["public_key_hash"],
        "proposal_ttl_hours": e["proposal_ttl_hours"], "operator_org": operator,
    }, now, ledger.staff_actor(staff))
    return e["election_id"]


def _active_keys(conn, eid: str) -> list[str]:
    return [r[0] for r in conn.execute(text("SELECT voter_key FROM credential.roster WHERE election_id = :e "
                                            "AND removed_checkpoint IS NULL"), {"e": eid})]


def to_public(e: dict) -> dict:
    return {"election_id": e["election_id"], "title": e["title"], "status": e["status"],
            "window_start": iso(e["window_start"]), "window_end": iso(e["window_end"]),
            "time_bucket_seconds": e["time_bucket_seconds"], "candidates": e["candidates"],
            "public_key": hashing.b64std(bytes(e["public_key"]))}


def to_staff(conn, e: dict) -> dict:
    eid = e["election_id"]
    active = _active_keys(conn, eid)
    out = to_public(e) | {
        "created_at": iso(from_dt(e["created_at"])), "voter_count": len(active),
        "organizations": e["organizations"], "operator_org": e["operator_org"],
        "approval_threshold": e["approval_threshold"], "proposal_ttl_hours": e["proposal_ttl_hours"],
        "checkpoint_interval_seconds": e["checkpoint_interval_seconds"], "creation_check": e["creation_check"],
        "open_checkpoint": e["open_checkpoint"],
    }
    if active:
        out["roster_hash"] = hashing.roster_hash(active)
    last = conn.execute(text(
        "SELECT c.checkpoint, o.event FROM credential.checkpoints c JOIN credential.outbox o ON o.event_id = c.event_id "
        "WHERE c.election_id = :e ORDER BY c.checkpoint DESC LIMIT 1"), {"e": eid}).first()
    if last:
        c = last.event["payload"]["counts"]
        out["counters"] = {"as_of_checkpoint": last.checkpoint, "tokens_active": c["tokens_active"],
                           "tokens_used": c["token_used"], "ballots_received": c["ballots_received"]}
    else:
        out["counters"] = {"as_of_checkpoint": 0, "tokens_active": 0, "tokens_used": 0, "ballots_received": 0}
    tr = conn.execute(text(
        "SELECT event_id, event->>'type' AS type, status FROM credential.outbox WHERE election_id = :e "
        "AND kind = 'system' AND event->>'type' = ANY(:types) ORDER BY seq DESC LIMIT 1"),
        {"e": eid, "types": list(TRANSITION_TO)}).first()
    if tr:
        out["transition"] = {"to_status": TRANSITION_TO[tr.type], "event_id": tr.event_id,
                             "ledger_status": LEDGER_STATUS[tr.status]}
    return out


def require_operator_admin(staff, e: dict) -> None:
    if staff.role != "admin" or staff.org_id != e["operator_org"]:
        raise Problem(403, "FORBIDDEN", "admin of the operator organization only")


def delete(conn, e: dict) -> None:
    created_rejected = conn.execute(text(
        "SELECT 1 FROM credential.outbox WHERE election_id = :e AND event->>'type' = 'ELECTION_CREATED' "
        "AND status = 'REJECTED'"), {"e": e["election_id"]}).first() is not None
    if e["status"] != "DRAFT" or not (created_rejected or e["creation_check"] == "MISMATCH"):
        raise Problem(409, "INVALID_STATE_TRANSITION")
    conn.execute(text("DELETE FROM credential.elections WHERE election_id = :e"), {"e": e["election_id"]})


def replace_roster(conn, e: dict, voters: list[dict]) -> dict:
    if e["status"] != "DRAFT":
        raise Refused(409, "ROSTER_FROZEN")
    refs = [v["external_ref"] for v in voters]
    if len(set(refs)) != len(refs):
        raise Problem(422, "VALIDATION_ERROR", "external_ref must be unique")
    conn.execute(text("DELETE FROM credential.roster WHERE election_id = :e"), {"e": e["election_id"]})
    keys = []
    for v in voters:
        k = random_id("vtr")
        keys.append(k)
        conn.execute(text("INSERT INTO credential.roster (election_id, voter_key, name, email, external_ref, "
                          "added_checkpoint) VALUES (:e, :k, :n, :m, :r, :c)"),
                     {"e": e["election_id"], "k": k, "n": v["name"], "m": v.get("email"), "r": v["external_ref"],
                      "c": e["open_checkpoint"]})
    return {"voter_count": len(keys), "roster_hash": hashing.roster_hash(keys)}


def read_roster(conn, e: dict, with_names: bool) -> dict:
    rows = conn.execute(text("SELECT voter_key, removed_checkpoint, external_ref, name FROM credential.roster "
                             "WHERE election_id = :e ORDER BY voter_key"), {"e": e["election_id"]})
    out = []
    for r in rows:
        v = {"voter_key": r.voter_key, "status": "REMOVED" if r.removed_checkpoint is not None else "ACTIVE"}
        if with_names:
            v |= {"external_ref": r.external_ref, "name": r.name}
        out.append(v)
    return {"voters": out}


class Refused(Problem):
    """A refusal that is also recorded in the rejected_attempts view (kind = code)."""

    def __init__(self, status: int, code: str, detail: str | None = None, subject_hash: str | None = None):
        super().__init__(status, code, detail)
        self.subject_hash = subject_hash


def freeze(conn, e: dict, now: int) -> None:
    if e["status"] == "ROSTER_FROZEN":
        return   # retry-safe
    if e["status"] != "DRAFT" or e["creation_check"] != "PASSED":
        raise Refused(409, "INVALID_STATE_TRANSITION")
    active = _active_keys(conn, e["election_id"])
    if not active:
        raise Refused(409, "INVALID_STATE_TRANSITION", "roster is empty")
    ledger.emit_system(conn, e, "ROSTER_FROZEN",
                       {"roster_hash": hashing.roster_hash(active), "voter_count": len(active)}, now)
    set_status(conn, e, "ROSTER_FROZEN", e["open_checkpoint"])
    lifecycle.commit_checkpoint(conn, e, "STATE_CHANGE", now)


def close_all_voted(conn, e: dict, now: int) -> None:
    """OPEN → CLOSED before window_end once every voter has voted (decisions v1.5, no proposal): every ACTIVE voter
    has a non-revoked token, every such token is USED and every registered credential has a ballot, so nobody can
    lose a vote. closed_at is the bucket boundary after now, so every accepted voter event has
    occurred_at + bucket <= closed_at (R2); in the last bucket that boundary is window_end and the scheduled close
    follows within one bucket, so the request is refused (closedAt < windowEnd on chain)."""
    if e["status"] == "CLOSED":
        return   # retry-safe
    if e["status"] != "OPEN":
        raise Refused(409, "INVALID_STATE_TRANSITION")
    if not settings.all_voted_close:
        raise Refused(409, "INVALID_STATE_TRANSITION", "ALL_VOTED_CLOSE is off in this deployment")
    eid = e["election_id"]
    tokens, used = conn.execute(text(
        "SELECT count(*), count(used_checkpoint) FROM credential.token_status "
        "WHERE election_id = :e AND revoked_checkpoint IS NULL"), {"e": eid}).one()
    credentials = conn.execute(text("SELECT count(*) FROM ballot.credential_issuance WHERE election_id = :e"),
                               {"e": eid}).scalar_one()
    ballots = conn.execute(text("SELECT count(*) FROM ballot.ballot_receipts WHERE election_id = :e"),
                           {"e": eid}).scalar_one()
    if not tokens or used != tokens or ballots != credentials or not lifecycle.all_voters_have_tokens(conn, eid):
        raise Refused(409, "INVALID_STATE_TRANSITION", f"not everyone has voted ({ballots} of {tokens} ballots)")
    b = e["time_bucket_seconds"]
    closed_at = timeutil.floor_bucket(now, b) + b
    if closed_at >= e["window_end"]:
        raise Refused(409, "INVALID_STATE_TRANSITION", "last bucket: the scheduled close at window_end comes first")
    lifecycle.close(conn, e, closed_at, "ALL_VOTED", now)


def issue_tokens(conn, e: dict, now: int) -> tuple[list[dict], int]:
    """Issue one token per ACTIVE voter without a non-revoked token. Returns (CSV rows, batch_no or 0)."""
    if e["status"] not in ("ROSTER_FROZEN", "OPEN") or e["creation_check"] != "PASSED":
        raise Refused(409, "INVALID_STATE_TRANSITION")
    eid = e["election_id"]
    voters = conn.execute(text(
        "SELECT r.voter_key, r.external_ref, r.name FROM credential.roster r WHERE r.election_id = :e "
        "AND r.removed_checkpoint IS NULL AND NOT EXISTS (SELECT 1 FROM credential.token_status t "
        "WHERE t.election_id = r.election_id AND t.voter_key = r.voter_key AND t.revoked_checkpoint IS NULL) "
        "ORDER BY r.external_ref"), {"e": eid}).all()
    rows = []
    for v in voters:
        token = token128()
        conn.execute(text("INSERT INTO credential.token_status (election_id, token_hash, voter_key, issued_checkpoint) "
                          "VALUES (:e, :h, :k, :c)"),
                     {"e": eid, "h": hashing.H(token), "k": v.voter_key, "c": e["open_checkpoint"]})
        rows.append({"external_ref": v.external_ref, "name": v.name,
                     "link": f"{settings.voter_web_url}/vote/{eid}#t={token}"})
    if not rows:
        return [], 0
    live = [r[0] for r in conn.execute(text("SELECT token_hash FROM credential.token_status WHERE election_id = :e "
                                            "AND revoked_checkpoint IS NULL"), {"e": eid})]
    batch_no = 1 + conn.execute(text("SELECT count(*) FROM credential.outbox WHERE election_id = :e "
                                     "AND event->>'type' = 'TOKEN_BATCH_ISSUED'"), {"e": eid}).scalar_one()
    ledger.emit_system(conn, e, "TOKEN_BATCH_ISSUED", {"batch_no": batch_no, "token_count": len(rows),
                                                       "token_set_root": hashing.token_set_root(live)}, now)
    lifecycle.schedule_open(conn, e, now)
    return rows, batch_no


def _cell(value: str) -> str:
    # roster names come from an uploaded file: never let a spreadsheet run them as formulas
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["external_ref", "name", "link"], lineterminator="\n")
    w.writeheader()
    w.writerows({k: _cell(v) for k, v in r.items()} for r in rows)
    return buf.getvalue()


class IssueCache:
    """Lost-response retry for tokens:issue: the CSV is kept encrypted in memory for 15 minutes, then wiped (core-api).
    The key itself is recorded in credential.idempotency, so a retry after the wipe (or a restart) gets 409."""
    TTL = 15 * 60

    def __init__(self):
        self._key = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
        self._items: dict[tuple[str, str], tuple[float, bytes]] = {}

    def _purge(self):
        now = time.monotonic()
        for k in [k for k, v in self._items.items() if now - v[0] > self.TTL]:
            del self._items[k]

    def get(self, eid: str, key: str) -> str | None:
        self._purge()
        item = self._items.get((eid, key))
        return None if item is None else nacl.secret.SecretBox(self._key).decrypt(item[1]).decode("utf-8")

    def put(self, eid: str, key: str, csv_text: str) -> None:
        self._items.setdefault((eid, key), (time.monotonic(),
                                            nacl.secret.SecretBox(self._key).encrypt(csv_text.encode("utf-8"))))


issue_cache = IssueCache()


def body_hash(body: dict) -> str:
    return hashlib.sha256(jsonb(body).encode("utf-8")).hexdigest()


def load(conn, eid: str, lock: bool = False) -> dict:
    return load_election(conn, eid, lock)


def record(conn, e: dict, exc: Refused) -> None:
    record_rejection(conn, e["election_id"], e["open_checkpoint"], exc.code, exc.subject_hash)
