"""Core of the Ledger Gateway: event intake, contract writes, confirmation, rejections and anchors.

M1 scope (docs/schedule.md, Mon 11-09): ELECTION_CREATED, ROSTER_FROZEN, TOKEN_BATCH_ISSUED, ELECTION_OPENED,
ELECTION_CLOSED (scheduled), CHECKPOINT_COMMITTED, TALLY_PUBLISHED; voter events TOKEN_USED and BALLOT_RECEIVED with
window / duplicate checks and shuffled Merkle batches. Everything else is answered REJECTED with the detail
"not implemented in M1". Temporary choices are listed in ledger/DECISIONS.md.

One worker thread owns every chain write (so there is one nonce sequence); a poller thread confirms transactions;
a batcher thread closes voter batches. A system event is ACCEPTED only when its transaction is in a confirmed block
(latest >= block + D). The HTTP request waits a few seconds for that; if it is not confirmed in time the answer is
RETRY_LATER and the caller's retry (same event_id) finds the result.
"""
import json
import logging
import queue
import random
import secrets
import threading
import time
import urllib.request
from calendar import timegm
from datetime import datetime, timezone

import gw_abi
import pv_hash
from gw_chain import ChainError, Revert

log = logging.getLogger("gateway")
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ZERO32 = "0x" + "00" * 32
STATUS_NAMES = ["DRAFT", "ROSTER_FROZEN", "OPEN", "CLOSED", "TALLIED", "FINALIZED", "VOIDED"]
VOTER_TYPES = ("TOKEN_USED", "BALLOT_RECEIVED")
SYSTEM_TYPES = ("ELECTION_CREATED", "ROSTER_FROZEN", "TOKEN_BATCH_ISSUED", "ELECTION_OPENED", "ELECTION_CLOSED",
                "CHECKPOINT_COMMITTED", "TALLY_PUBLISHED")
CHECKPOINT_REASON = {"PERIODIC": 0, "STATE_CHANGE": 1, "CLOSE_FINAL": 2, "TALLY": 3}
ALREADY_APPLIED = ("ElectionExists", "SeqNotIncreasing", "CloseFinalAlreadyCommitted")
RETRY_REVERTS = ("TimeNotReached", "NotGateway")
ORG_REVERTS = ("OrgNotRegistered", "OperatorOrgMismatch")


class ValidationError(Exception):
    """The request body is malformed (HTTP 400)."""


class Unsupported(Exception):
    pass


def rand_id(prefix):
    return prefix + "".join(secrets.choice(CROCKFORD) for _ in range(26))


def to_unix(ts):
    return timegm(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").timetuple())


def to_iso(unix):
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return to_iso(int(time.time()))


def problem_code_for(revert_name):
    return "ORG_NOT_REGISTERED" if revert_name in ORG_REVERTS else "INVALID_STATE_TRANSITION"


class Engine:
    def __init__(self, store, chain, org_ids, batch_seconds=60, batch_events=50, request_wait=3.5,
                 confirm_timeout=180, core_url="", gateway_key=""):
        self.store, self.chain = store, chain
        self.core_url, self.gateway_key = core_url, gateway_key
        self.org_ids = list(org_ids)
        self.org_by_b32 = {gw_abi.idh(o): o for o in self.org_ids}
        self.batch_seconds, self.batch_events = batch_seconds, batch_events
        self.request_wait, self.confirm_timeout = request_wait, confirm_timeout
        self.lock = threading.RLock()
        self.q = queue.Queue()
        self.stop = threading.Event()
        self.local_nonce = 0
        self.batch_open = {}          # election_id -> monotonic time the open batch got its first event (memory only)
        self.batch_queued = set()
        self.anchor_blocked = {}      # election_id -> detail (ANCHOR_REJECTED raised, manual recovery)
        self.alerts = []              # in-memory alert log (Core alert delivery is not wired in M1)
        self._state_cache = {}
        self._rebroadcast = {}
        self.threads = []

    # ------------------------------------------------------------------ life cycle
    def start(self):
        with self.lock:
            for r in self.store.all("SELECT event_id FROM events WHERE status='QUEUED' ORDER BY rowid"):
                self.q.put(("event", r["event_id"]))
        for fn in (self._worker, self._poller, self._batcher):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self):
        self.stop.set()

    def alert(self, code, detail):
        log.error("ALERT %s: %s", code, detail)
        self.alerts.append({"code": code, "detail": detail, "at": now_iso()})
        del self.alerts[:-100]

    # ------------------------------------------------------------------ helpers
    def _state(self, election_id):
        """electionState at the confirmed block, cached for one second. None if the election does not exist."""
        hit = self._state_cache.get(election_id)
        if hit and time.monotonic() - hit[0] < 1.0:
            return hit[1]
        r = self.chain.call("electionState", gw_abi.idh(election_id), block="confirmed")
        keys = ["status", "window_start", "window_end", "bucket", "config_hash", "roster_hash", "token_set_root",
                "result_hash", "last_checkpoint", "last_core_seq", "operator_org"]
        st = dict(zip(keys, r))
        st = None if int(st["config_hash"], 16) == 0 else st
        self._state_cache[election_id] = (time.monotonic(), st)
        return st

    def _make_rejection(self, ev, code, detail, voter):
        p = ev.get("payload", {})
        subject = None
        if ev["type"] in ("TOKEN_USED", "TOKEN_REVOKED"):
            subject = p.get("token_hash")
        elif ev["type"] == "BALLOT_RECEIVED":
            subject = p.get("nullifier_hash")
        rec = {"rejection_id": rand_id("rej_"), "election_id": ev["election_id"], "event_id": ev["event_id"],
               "type": ev["type"], "checkpoint": ev["checkpoint"], "reject_code": code,
               "recorded_at": ev["occurred_at"] if voter else now_iso(),
               "payload_hash": ev["payload_hash"], "subject_hash": subject}
        leaf = pv_hash.Hc(rec)
        self.store.insert("rejections", dict(rec, detail=detail, leaf=leaf, anchor_id=None))
        return rec["rejection_id"]

    def _pending_rejections(self, election_id):
        return self.store.all("SELECT rejection_id, leaf FROM rejections WHERE election_id=? AND anchor_id IS NULL "
                              "ORDER BY rejection_id", (election_id,))

    def _res(self, event_id, status, **kw):
        out = {"event_id": event_id, "status": status}
        out.update({k: v for k, v in kw.items() if v is not None})
        return out

    # ------------------------------------------------------------------ submit
    def submit(self, events):
        for ev in events:
            self._check_shape(ev)
        results, deadline = [], time.monotonic() + self.request_wait
        for ev in events:
            try:
                if ev["type"] in VOTER_TYPES:
                    results.append(self._submit_voter(ev))
                else:
                    results.append(self._submit_system(ev, deadline))
            except ChainError as e:
                log.warning("chain unavailable: %s", e)
                results.append(self._res(ev["event_id"], "RETRY_LATER", reject_code="LEDGER_UNAVAILABLE",
                                         detail="node not reachable"))
        return results

    @staticmethod
    def _check_shape(ev):
        need = ["event_id", "schema_version", "election_id", "type", "source", "checkpoint", "occurred_at",
                "time_precision", "actor", "payload", "payload_hash"]
        if not isinstance(ev, dict) or any(k not in ev for k in need):
            raise ValidationError("event is missing required fields")
        if ev["type"] not in SYSTEM_TYPES + VOTER_TYPES and ev["type"] not in (
                "ROSTER_AMENDED", "TOKEN_REVOKED", "ADMIN_ACTION_REQUESTED", "APPROVAL_RECORDED", "PROPOSAL_CLOSED",
                "ADMIN_ACTION_EXECUTED", "ADMIN_ACTION_FAILED", "WINDOW_CHANGED", "FINDING_RECORDED",
                "FINDING_REVIEWED", "ELECTION_FINALIZED", "ELECTION_VOIDED"):
            raise ValidationError("unknown event type " + str(ev["type"]))
        if not isinstance(ev["payload"], dict) or not isinstance(ev["checkpoint"], int):
            raise ValidationError("payload must be an object and checkpoint an integer")
        try:
            to_unix(ev["occurred_at"])
        except ValueError:
            raise ValidationError("occurred_at must be YYYY-MM-DDTHH:MM:SSZ")
        if ev["type"] not in VOTER_TYPES and not isinstance(ev.get("seq"), int):
            raise ValidationError("system events need an integer seq")

    # ---- system events
    def _submit_system(self, ev, deadline):
        eid = ev["event_id"]
        with self.lock:
            row = self.store.one("SELECT * FROM events WHERE event_id=?", (eid,))
            if row:
                if row["payload_hash"] != ev["payload_hash"]:
                    return self._res(eid, "CONFLICT")
                if row["status"] == "DEFERRED":
                    if not self._requeue(row):
                        return self._res(eid, "RETRY_LATER", reject_code="SEQUENCE_GAP",
                                         detail="an earlier event of this election is not applied yet")
                else:
                    return self._row_result(row, duplicate=True)
            else:
                outcome = self._admit_system(ev)
                if outcome is not None:
                    return outcome
        return self._wait_result(eid, deadline)

    def _admit_system(self, ev):
        """Called with the lock held. Stores the event (QUEUED or REJECTED), or answers RETRY_LATER."""
        eid, el, seq = ev["event_id"], ev["election_id"], ev["seq"]
        last = self._last_assigned(el)
        if seq <= last:
            return self._reject_system(ev, "INVALID_STATE_TRANSITION", f"seq {seq} was already used", consume=False)
        if seq != last + 1:
            return self._res(eid, "RETRY_LATER", reject_code="SEQUENCE_GAP", detail=f"expected seq {last + 1}")
        if ev["source"] != "core":
            return self._reject_system(ev, "INVALID_STATE_TRANSITION", "only core events are accepted here")
        if pv_hash.Hc(ev["payload"]) != ev["payload_hash"]:
            return self._reject_system(ev, "VALIDATION_ERROR", "payload_hash is not Hc(payload)")
        try:
            self._call_for(ev)
        except Unsupported as e:
            return self._reject_system(ev, "INVALID_STATE_TRANSITION", "not implemented in M1: " + str(e))
        except (KeyError, ValueError, TypeError) as e:
            return self._reject_system(ev, "VALIDATION_ERROR", "payload does not fit the event type: " + repr(e))
        self._insert_event(ev, "QUEUED")
        self._set_last_assigned(el, seq)
        self.q.put(("event", eid))
        return None

    def _insert_event(self, ev, status, **extra):
        row = {"event_id": ev["event_id"], "election_id": ev["election_id"], "type": ev["type"], "seq": ev["seq"],
               "checkpoint": ev["checkpoint"], "occurred_at": ev["occurred_at"],
               "submission": json.dumps(ev, sort_keys=True), "payload_hash": ev["payload_hash"],
               "leaf_hash": pv_hash.Hc(ev), "received_at": now_iso(), "status": status}
        row.update(extra)
        self.store.insert("events", row)

    def _reject_system(self, ev, code, detail, consume=True):
        rid = self._make_rejection(ev, code, detail, voter=False)
        if consume:
            if not self.store.one("SELECT 1 AS x FROM events WHERE event_id=?", (ev["event_id"],)):
                self._insert_event(ev, "REJECTED", rejection_id=rid, detail=detail)
            self._set_last_assigned(ev["election_id"], max(ev["seq"], self._last_assigned(ev["election_id"])))
        return self._res(ev["event_id"], "REJECTED", reject_code=code, rejection_id=rid, detail=detail)

    def _last_assigned(self, el):
        r = self.store.one("SELECT last_assigned FROM election_seq WHERE election_id=?", (el,))
        return r["last_assigned"] if r else 0

    def _set_last_assigned(self, el, n):
        self.store.run("INSERT INTO election_seq(election_id,last_assigned) VALUES(?,?) "
                       "ON CONFLICT(election_id) DO UPDATE SET last_assigned=excluded.last_assigned", (el, n))

    def _requeue(self, row):
        el = row["election_id"]
        if row["seq"] != self._last_assigned(el) + 1:
            return False
        self.store.update("events", "event_id", row["event_id"], {"status": "QUEUED", "detail": None})
        self._set_last_assigned(el, row["seq"])
        self.q.put(("event", row["event_id"]))
        return True

    def _wait_result(self, eid, deadline):
        while True:
            row = self.store.one("SELECT * FROM events WHERE event_id=?", (eid,))
            if row["status"] in ("ACCEPTED", "REJECTED", "DEFERRED") or time.monotonic() >= deadline:
                return self._row_result(row)
            time.sleep(0.1)

    def _row_result(self, row, duplicate=False):
        st, eid = row["status"], row["event_id"]
        if st == "ACCEPTED":
            return self._res(eid, "DUPLICATE" if duplicate else "ACCEPTED", anchor_mode="immediate",
                             tx_hash=row["tx_hash"], block_number=row["block_number"])
        if st == "REJECTED":
            rej = self.store.one("SELECT reject_code FROM rejections WHERE rejection_id=?", (row["rejection_id"],))
            return self._res(eid, "REJECTED", reject_code=rej["reject_code"] if rej else None,
                             rejection_id=row["rejection_id"], detail=row["detail"])
        if st == "DEFERRED":
            return self._res(eid, "RETRY_LATER", reject_code=row["detail"] or "TIME_NOT_REACHED",
                             detail="try again later")
        return self._res(eid, "RETRY_LATER", detail="transaction is waiting for confirmation (depth %d)"
                         % self.chain.depth)

    # ---- event to contract call
    def _call_for(self, ev):
        p, el, seq, t = ev["payload"], gw_abi.idh(ev["election_id"]), ev["seq"], ev["type"]
        if t == "ELECTION_CREATED":
            orgs = sorted(p["organizations"], key=lambda o: o["org_id"])
            return "createElection", [el, gw_abi.hex32(p["config_hash"]), to_unix(p["window_start"]),
                                      to_unix(p["window_end"]), p["time_bucket_seconds"],
                                      p["checkpoint_interval_seconds"], [gw_abi.idh(o["org_id"]) for o in orgs],
                                      p["approval_threshold"], gw_abi.idh(p["operator_org"]),
                                      p["proposal_ttl_hours"], seq]
        if t == "ROSTER_FROZEN":
            return "freezeRoster", [el, gw_abi.hex32(p["roster_hash"]), seq]
        if t == "TOKEN_BATCH_ISSUED":
            return "setTokenSetRoot", [el, gw_abi.hex32(p["token_set_root"]), ZERO32, seq]
        if t == "ELECTION_OPENED":
            return "openElection", [el, seq]
        if t == "ELECTION_CLOSED":
            if p["reason"] == "SCHEDULED":
                return "closeElection", [el, to_unix(p["closed_at"]), False, ZERO32, 0, 0, seq]
            return "closeElection", [el, to_unix(p["closed_at"]), True, gw_abi.idh(p["proposal_id"]),
                                     to_unix(p["requested_closed_at"]), 0, seq]
        if t == "CHECKPOINT_COMMITTED":
            return "commitCheckpoint", [el, p["checkpoint"], CHECKPOINT_REASON[p["reason"]],
                                        gw_abi.hex32(pv_hash.Hc(p["tables"])), seq]
        if t == "TALLY_PUBLISHED":
            return "publishTally", [el, gw_abi.hex32(p["result_hash"]), p["final_checkpoint"], seq]
        raise Unsupported(t)

    # ------------------------------------------------------------------ worker thread (all chain writes)
    def _worker(self):
        while not self.stop.is_set():
            try:
                kind, arg = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if kind == "event":
                    self._process_event(arg)
                elif kind == "anchor_event":
                    self._anchor_system_event(arg)
                elif kind == "anchor_batch":
                    self._anchor_batch(arg)
            except Exception:  # keep the worker alive; the poller and retries recover
                log.exception("worker task %s failed", kind)
                if kind == "anchor_batch":
                    self.batch_queued.discard(arg)

    def _process_event(self, eid):
        row = self.store.one("SELECT * FROM events WHERE event_id=?", (eid,))
        if not row or row["status"] != "QUEUED":
            return
        ev = json.loads(row["submission"])
        name, args = self._call_for(ev)
        try:
            self.chain.call(name, *args, block="latest")
            pre = None
        except Revert as r:
            pre = r.name
            if pre in RETRY_REVERTS:
                if pre == "NotGateway":
                    self.alert("LEDGER_UNAVAILABLE", "Gateway account is not registered or confirmed on chain")
                self._defer(row, "TIME_NOT_REACHED" if pre == "TimeNotReached" else "LEDGER_UNAVAILABLE")
                return
        except ChainError as e:
            log.warning("precheck failed (%s)", e)
            self._defer(row, "LEDGER_UNAVAILABLE")
            return
        nonce = max(self.local_nonce, self.chain.next_nonce())
        raw, txh = self.chain.build_tx(name, args, nonce)
        with self.lock:
            self.store.update("events", "event_id", eid, {"status": "SENT", "tx_hash": txh, "nonce": nonce,
                                                         "raw_tx": raw, "precheck": pre})
        self.local_nonce = nonce + 1
        try:
            self.chain.send_raw(raw)
        except ChainError as e:
            log.warning("broadcast failed, the poller will retry the same transaction: %s", e)
        end, last_cast = time.monotonic() + self.confirm_timeout, time.monotonic()
        while time.monotonic() < end and not self.stop.is_set():
            if self._finalize_event(eid):
                return
            time.sleep(0.25)
            if time.monotonic() - last_cast > 8:
                self._rebroadcast_tx(raw)
                last_cast = time.monotonic()

    def _rebroadcast_tx(self, raw):
        try:
            self.chain.send_raw(raw)
        except ChainError:
            pass

    def _defer(self, row, code):
        """RETRY_LATER: the event was not applied; give its seq back and hold every later queued event of the election."""
        with self.lock:
            el = row["election_id"]
            self.store.update("events", "event_id", row["event_id"],
                              {"status": "DEFERRED", "detail": code, "tx_hash": None, "raw_tx": None})
            self.store.run("UPDATE events SET status='DEFERRED', detail=? WHERE election_id=? AND seq>? AND status='QUEUED'",
                           (code, el, row["seq"]))
            self._set_last_assigned(el, row["seq"] - 1)

    # ------------------------------------------------------------------ confirmation
    def _finalize_event(self, eid):
        """True once the event has a final outcome (ACCEPTED, REJECTED or DEFERRED)."""
        with self.lock:
            row = self.store.one("SELECT * FROM events WHERE event_id=?", (eid,))
            if row["status"] in ("ACCEPTED", "REJECTED", "DEFERRED"):
                return True
            if row["status"] != "SENT":
                return False
            try:
                rc = self.chain.receipt(row["tx_hash"])
                if not rc:
                    return False
                bn = int(rc["blockNumber"], 16)
                if not self.chain.is_confirmed(bn):
                    return False
                blk = self.chain.rpc("eth_getBlockByNumber", [hex(bn), False])
                if not blk or blk["hash"] != rc["blockHash"]:
                    return False                      # the block was replaced by a fork; same tx is re-broadcast
            except ChainError:
                return False
            ev = json.loads(row["submission"])
            if rc["status"] == "0x1":
                self._accept_event(row, rc["transactionHash"], bn)
                return True
            return self._handle_revert(row, ev, bn)

    def _accept_event(self, row, tx_hash, bn):
        self.store.update("events", "event_id", row["event_id"],
                          {"status": "ACCEPTED", "tx_hash": tx_hash, "block_number": bn, "raw_tx": None})
        self.q.put(("anchor_event", row["event_id"]))

    def _handle_revert(self, row, ev, bn):
        name, args = self._call_for(ev)
        reason = row["precheck"]
        try:
            self.chain.call(name, *args, block=max(bn - 1, 0))
        except Revert as r:
            reason = r.name
        except ChainError:
            pass
        reason = reason or "UnknownRevert"
        if reason in RETRY_REVERTS:
            if reason == "NotGateway":
                self.alert("LEDGER_UNAVAILABLE", "Gateway account is not registered or confirmed on chain")
            self._defer(row, "TIME_NOT_REACHED" if reason == "TimeNotReached" else "LEDGER_UNAVAILABLE")
            return True
        if reason in ALREADY_APPLIED:
            found = self._already_applied(ev, name, args)
            if found:
                self._accept_event(row, found[0] or row["tx_hash"], found[1] or bn)
                return True
        code = problem_code_for(reason)
        rid = self._make_rejection(ev, code, "contract revert " + reason, voter=False)
        self.store.update("events", "event_id", row["event_id"],
                          {"status": "REJECTED", "rejection_id": rid, "detail": "contract revert " + reason,
                           "raw_tx": None, "block_number": bn})
        return True

    def _already_applied(self, ev, name, args):
        """Exact match in confirmed state (chain-interface.md "Confirmations"). Returns (tx_hash, block) or None."""
        el = args[0]
        cb = self.chain.confirmed_block()
        if name == "createElection":
            st = self.chain.call("electionState", el, block=cb)
            gov = self.chain.call("electionGovernance", el, block=cb)
            same = (st[4] == args[1] and st[1] == args[2] and st[2] == args[3] and st[3] == args[4]
                    and gov[0] == args[6] and gov[1] == args[7] and gov[2] == args[8] and gov[3] == args[9]
                    and gov[4] == args[5])
            if not same:
                return None
        elif name == "commitCheckpoint":
            snap = self.chain.call("electionStateAtCheckpoint", el, args[1], block=cb)
            if snap[1] == 0 or snap[5] != ev["seq"]:
                return None
            return ("", None)
        for lg in self.chain.logs(gw_abi.TOPIC_STATE_CHANGED, el, to_block=cb):
            data = bytes.fromhex(lg["data"][2:])
            if int.from_bytes(data[32:64], "big") == ev["seq"]:
                return lg["transactionHash"], int(lg["blockNumber"], 16)
        return ("", None) if name == "createElection" else None

    # ------------------------------------------------------------------ voter events
    def _submit_voter(self, ev):
        eid, el = ev["event_id"], ev["election_id"]
        with self.lock:
            row = self.store.one("SELECT * FROM voter_events WHERE event_id=?", (eid,))
            if row:
                if row["payload_hash"] != ev["payload_hash"]:
                    return self._res(eid, "CONFLICT")
                if row["status"] == "REJECTED":
                    rej = self.store.one("SELECT reject_code, detail FROM rejections WHERE rejection_id=?",
                                         (row["rejection_id"],))
                    return self._res(eid, "REJECTED", reject_code=rej["reject_code"], rejection_id=row["rejection_id"],
                                     detail=rej["detail"])
                return self._res(eid, "DUPLICATE", anchor_mode="batched")
            st = self._state(el)
            if st is None or st["status"] < 2:
                return self._res(eid, "RETRY_LATER", detail="election is not open on chain yet")
            code = detail = None
            kind, key = ("token", ev["payload"].get("token_hash")) if ev["type"] == "TOKEN_USED" else \
                ("nullifier", ev["payload"].get("nullifier_hash"))
            t = to_unix(ev["occurred_at"])
            if pv_hash.Hc(ev["payload"]) != ev["payload_hash"] or not key:
                code, detail = "VALIDATION_ERROR", "payload_hash is not Hc(payload) or the key field is missing"
            elif ev.get("time_precision") != "bucketed" or t % st["bucket"] != 0:
                code, detail = "VALIDATION_ERROR", "voter events carry a bucketed time"
            elif st["status"] >= 4 or t < st["window_start"] or t + st["bucket"] > st["window_end"]:
                code, detail = "ELECTION_NOT_OPEN", "outside the effective window"
            elif self.store.one("SELECT 1 AS x FROM voter_keys WHERE election_id=? AND kind=? AND hash=?",
                                (el, kind, key)):
                code, detail = "DUPLICATE_CONTENT", kind + " already accepted"
            base = {"event_id": eid, "election_id": el, "type": ev["type"], "checkpoint": ev["checkpoint"],
                    "occurred_at": ev["occurred_at"], "submission": json.dumps(ev, sort_keys=True),
                    "payload_hash": ev["payload_hash"], "leaf_hash": pv_hash.Hc(ev)}
            if code:
                rid = self._make_rejection(ev, code, detail, voter=True)
                self.store.insert("voter_events", dict(base, status="REJECTED", rejection_id=rid, anchor_id=None))
                return self._res(eid, "REJECTED", reject_code=code, rejection_id=rid, detail=detail)
            self.store.insert("voter_events", dict(base, status="PENDING", rejection_id=None, anchor_id=None))
            self.store.insert("voter_keys", {"election_id": el, "kind": kind, "hash": key})
            self.batch_open.setdefault(el, time.monotonic())
            return self._res(eid, "ACCEPTED", anchor_mode="batched")

    # ------------------------------------------------------------------ anchors
    def _anchor_system_event(self, eid):
        row = self.store.one("SELECT * FROM events WHERE event_id=?", (eid,))
        if not row or row["status"] != "ACCEPTED" or row["anchor_id"]:
            return
        t = to_unix(row["occurred_at"])
        self._send_anchor(row["election_id"], "immediate", [(eid, row["leaf_hash"])], t, t, [eid], row["checkpoint"])

    def _anchor_batch(self, el):
        try:
            with self.lock:
                rows = self.store.all("SELECT event_id, leaf_hash, occurred_at, checkpoint FROM voter_events WHERE election_id=? "
                                      "AND status='PENDING' AND anchor_id IS NULL LIMIT 1000", (el,))
                if not rows:
                    self.batch_open.pop(el, None)
                    return
                random.SystemRandom().shuffle(rows)               # tree order is random: no arrival order leaks
                times = [to_unix(r["occurred_at"]) for r in rows]
                self.batch_open.pop(el, None)
            self._send_anchor(el, "batched", [(r["event_id"], r["leaf_hash"]) for r in rows], min(times), max(times),
                              None, max(r["checkpoint"] for r in rows))
        finally:
            self.batch_queued.discard(el)

    def _send_anchor(self, el, mode, leaves, bmin, bmax, system_event_ids, checkpoint=0):
        with self.lock:
            rej = self._pending_rejections(el)
            anchor_id = rand_id("anc_")
            root = pv_hash.merkle_root([h for _, h in leaves])
            rroot = pv_hash.set_root([r["leaf"] for r in rej])
            args = [gw_abi.idh(el), gw_abi.idh(anchor_id), gw_abi.hex32(root), gw_abi.hex32(rroot),
                    0 if mode == "immediate" else 1, len(leaves), bmin, bmax]
            try:
                self.chain.call("commitAnchor", *args, block="latest")
            except Revert as r:
                self._anchor_failed(el, anchor_id, "precheck revert " + r.name, checkpoint)
                return
            nonce = max(self.local_nonce, self.chain.next_nonce())
            raw, txh = self.chain.build_tx("commitAnchor", args, nonce)
            self.store.insert("anchors", {
                "anchor_id": anchor_id, "election_id": el, "mode": mode, "merkle_root": root, "event_count": len(leaves),
                "rejections_root": rroot, "bucket_min": bmin, "bucket_max": bmax,
                "leaves": json.dumps([list(x) for x in leaves]),
                "rejection_ids": json.dumps([r["rejection_id"] for r in rej]), "status": "SENT",
                "tx_hash": txh, "raw_tx": raw})
            for r in rej:
                self.store.update("rejections", "rejection_id", r["rejection_id"], {"anchor_id": anchor_id})
            if mode == "batched":
                for eid, _ in leaves:
                    self.store.update("voter_events", "event_id", eid, {"anchor_id": anchor_id})
            else:
                self.store.update("events", "event_id", system_event_ids[0], {"anchor_id": anchor_id})
            self.local_nonce = nonce + 1
        try:
            self.chain.send_raw(raw)
        except ChainError as e:
            log.warning("anchor broadcast failed, the poller will retry: %s", e)

    def _notify_core(self, election_id, checkpoint, detail):
        """ANCHOR_REJECTED to Core (ledger-gateway.openapi.yaml: the Gateway raises it when the contract refuses an anchor).
        Retried in the background; without CORE_URL and GATEWAY_KEY it is only logged."""
        if not self.core_url or not self.gateway_key:
            return
        body = json.dumps({"election_id": election_id, "checkpoint": checkpoint, "kind": "ANCHOR_REJECTED",
                           "detail": detail[:500]}).encode()

        def send():
            for wait in (0, 2, 5, 15, 30, 60):
                time.sleep(wait)
                try:
                    req = urllib.request.Request(self.core_url.rstrip("/") + "/internal/v1/audit-alerts", body,
                                                 {"Content-Type": "application/json", "X-Service-Key": self.gateway_key},
                                                 method="POST")
                    with urllib.request.urlopen(req, timeout=5) as r:
                        if r.status in (200, 202):
                            return
                except Exception as e:  # Core down or refusing: try again later
                    log.warning("ANCHOR_REJECTED not delivered to Core yet: %s", e)
            log.error("ANCHOR_REJECTED could not be delivered to Core for %s", election_id)

        threading.Thread(target=send, daemon=True).start()

    def _anchor_failed(self, el, anchor_id, detail, checkpoint=0):
        """The contract refused an anchor. Release its events and rejections; Core is told (checkpoint = highest
        checkpoint of the batch); recovery is a manual operator action."""
        with self.lock:
            a = self.store.one("SELECT * FROM anchors WHERE anchor_id=?", (anchor_id,))
            if a:
                cp = self.store.one("SELECT MAX(checkpoint) AS c FROM voter_events WHERE anchor_id=?", (anchor_id,))["c"]
                if cp is not None:
                    checkpoint = cp
                self.store.update("anchors", "anchor_id", anchor_id, {"status": "FAILED", "detail": detail})
                self.store.run("UPDATE voter_events SET anchor_id=NULL WHERE anchor_id=?", (anchor_id,))
                self.store.run("UPDATE events SET anchor_id=NULL WHERE anchor_id=?", (anchor_id,))
                self.store.run("UPDATE rejections SET anchor_id=NULL WHERE anchor_id=?", (anchor_id,))
            self.anchor_blocked[el] = detail
        self.alert("ANCHOR_REJECTED", f"{el}: {detail}")
        self._notify_core(el, checkpoint, detail)

    def _finalize_anchor(self, a):
        try:
            rc = self.chain.receipt(a["tx_hash"])
            if not rc:
                return
            bn = int(rc["blockNumber"], 16)
            if not self.chain.is_confirmed(bn):
                return
            blk = self.chain.rpc("eth_getBlockByNumber", [hex(bn), False])
            if not blk or blk["hash"] != rc["blockHash"]:
                return
        except ChainError:
            return
        with self.lock:
            if rc["status"] != "0x1":
                self._anchor_failed(a["election_id"], a["anchor_id"], "anchor transaction reverted")
                return
            self.store.update("anchors", "anchor_id", a["anchor_id"],
                              {"status": "ANCHORED", "block_number": bn, "anchored_at": to_iso(int(blk["timestamp"], 16)),
                               "raw_tx": None})
            self.store.run("UPDATE voter_events SET status='ANCHORED' WHERE anchor_id=? AND status='PENDING'",
                           (a["anchor_id"],))

    # ------------------------------------------------------------------ poller and batcher threads
    def _poller(self):
        while not self.stop.is_set():
            try:
                for r in self.store.all("SELECT event_id, raw_tx FROM events WHERE status='SENT'"):
                    self._finalize_event(r["event_id"])
                for a in self.store.all("SELECT * FROM anchors WHERE status='SENT'"):
                    self._finalize_anchor(a)
                    if a["raw_tx"] and time.monotonic() - self._rebroadcast.get(a["anchor_id"], 0) > 8:
                        self._rebroadcast[a["anchor_id"]] = time.monotonic()
                        self._rebroadcast_tx(a["raw_tx"])
            except Exception:
                log.exception("poller")
            self.stop.wait(0.5)

    def _batcher(self):
        while not self.stop.is_set():
            try:
                for r in self.store.all("SELECT election_id, COUNT(*) AS n FROM voter_events WHERE status='PENDING' "
                                        "AND anchor_id IS NULL GROUP BY election_id"):
                    el, n = r["election_id"], r["n"]
                    if el in self.batch_queued or el in self.anchor_blocked:
                        continue
                    opened = self.batch_open.setdefault(el, time.monotonic())
                    due = n >= self.batch_events or time.monotonic() - opened >= self.batch_seconds
                    if not due:
                        st = self._state(el)
                        due = st is not None and st["status"] >= 3          # election closed: flush at once
                    if due:
                        self.batch_queued.add(el)
                        self.q.put(("anchor_batch", el))
            except Exception:
                log.exception("batcher")
            self.stop.wait(1.0)

    def flush(self, election_id):
        """Operator or test helper: close the open batch of an election now."""
        if election_id not in self.batch_queued:
            self.batch_queued.add(election_id)
            self.q.put(("anchor_batch", election_id))
