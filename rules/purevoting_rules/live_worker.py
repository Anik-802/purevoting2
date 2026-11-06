"""Poll Core/Gateway over HTTP. Run from rules/ with python -m purevoting_rules.live_worker."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from contextlib import ExitStack, closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import httpx

from .engine import evaluate_all
from .findings import build_finding
from .live_chain import ChainPins, PinnedChain, reported_anchor_block
from .live_inputs import InputError, LiveInputs
from .live_publish import deliver
from .scheduler import run_decision
from .signing import EIP191Signer
from .storage import FindingStore


class PollJournal:
    """Separate from immutable findings. Persist first observed acceptance across process restarts."""
    def __init__(self, path, pins, signer):
        self.db = sqlite3.connect(path)
        try:
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS network (id INTEGER PRIMARY KEY, identity TEXT NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS seen (election TEXT, event TEXT, at INTEGER NOT NULL,"
                                " PRIMARY KEY(election, event))")
                self.db.execute("CREATE TABLE IF NOT EXISTS evaluated (election TEXT, event TEXT, at INTEGER NOT NULL,"
                                " policy TEXT NOT NULL, PRIMARY KEY(election, event))")
                value = json.dumps([str(pins.chain_id), pins.contract_address.lower(), signer.address.lower()])
                self.db.execute("INSERT OR IGNORE INTO network VALUES (1,?)", (value,))
                if self.db.execute("SELECT identity FROM network WHERE id=1").fetchone() != (value,):
                    raise ValueError("State directory belongs to a different deployment or signer")
        except BaseException:
            self.db.close()
            raise

    def elapsed(self, eid, event, now):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (eid, event, int(now)))
        first = self.db.execute("SELECT at FROM seen WHERE election=? AND event=?", (eid, event)).fetchone()[0]
        return max(0, int(now) - first)

    def due(self, eid, event, now, partial_interval):
        row = self.db.execute("SELECT at,policy FROM evaluated WHERE election=? AND event=?", (eid, event)).fetchone()
        if row is None or row[1] == "recoverable":
            return True
        return row[1] == "partial" and (now < row[0] or now - row[0] >= partial_interval)

    def completed(self, eid, event, now, outcomes):
        unresolved = [o for o in outcomes if o.result == "INCONCLUSIVE"]
        # Known implementation gaps do not justify repeated full historical reads every minute.
        # Evidence failures do: promptly re-evaluate when a view, node or batch may have recovered.
        if not unresolved:
            policy = "settled"
        elif all(o.coverage == "partial" and o.inconclusive_reason == "OTHER" for o in unresolved):
            policy = "partial"
        else:
            policy = "recoverable"
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO evaluated VALUES (?,?,?,?)", (eid, event, int(now), policy))

    def close(self):
        self.db.close()


@dataclass
class PollResult:
    elections: int = 0
    evaluated: int = 0
    skipped: int = 0
    alerts: int = 0
    read_errors: int = 0
    alert_errors: int = 0
    stored: int = 0
    retry: int = 0
    rejected: int = 0


class Worker:
    def __init__(self, core, gateway, inputs, chain, pins, signer, store, journal, *, batch_seconds=60,
                 now=time.time, heartbeat=lambda: None, partial_interval=600):
        self.core, self.gateway, self.inputs = core, gateway, inputs
        self.chain, self.pins, self.signer = chain, pins, signer
        self.store, self.journal, self.batch_seconds = store, journal, batch_seconds
        self.now, self.heartbeat, self.partial_interval = now, heartbeat, partial_interval

    def _alert(self, result, election, k, kind):
        # Only known ids from validated discovery and fixed alert names leave the process.
        try:
            r = self.core.post("/internal/v1/audit-alerts", json={
                "election_id": election, "checkpoint": k, "kind": kind})
        except httpx.RequestError:
            result.alert_errors += 1
            return
        if r.status_code == 202:
            result.alerts += 1
        else:
            result.alert_errors += 1

    def _gateway_preflight(self):
        try:
            response = self.gateway.get("/v1/chain")
            if response.status_code != 200:
                return "LEDGER_UNAVAILABLE"
            info = response.json()
            if not isinstance(info, dict) or self.pins.differences(info):
                return "CHAIN_CONFIG_MISMATCH"
        except (httpx.RequestError, ValueError, TypeError):
            return "LEDGER_UNAVAILABLE"
        return None

    def poll(self):
        result = PollResult()
        blocked = self._gateway_preflight()
        if blocked:
            result.read_errors += 1
        try:
            elections = self.inputs.elections()
        except InputError:
            result.read_errors += 1
            elections = []
        result.elections = len(elections)
        for election in elections:
            eid = election["election_id"]
            try:
                checkpoints = self.inputs.checkpoints(eid)
            except InputError:
                result.read_errors += 1
                continue  # No verified checkpoint id for a contract-valid alert.
            for cp in sorted(checkpoints, key=lambda v: v["checkpoint"]):
                self.heartbeat()
                k = cp["checkpoint"]
                if cp["ledger_status"] == "ACCEPTED":
                    # Neither API exposes the exact timestamp of acceptance. Remember Core's first
                    # acknowledged acceptance immediately, even if evidence fetching fails this poll.
                    # A matching Gateway commit and the depth gate are still required to evaluate.
                    self.journal.elapsed(eid, cp["event_id"], self.now())
                if blocked:
                    self._alert(result, eid, k, blocked)
                elif cp["ledger_status"] == "PENDING":
                    self._alert(result, eid, k, "AUDIT_DELAYED")
                elif cp["ledger_status"] == "ACCEPTED":
                    if not self.journal.due(eid, cp["event_id"], self.now(), self.partial_interval):
                        result.skipped += 1
                        continue
                    try:
                        snap = self.inputs.snapshot(eid, k, checkpoints)
                    except InputError as e:
                        result.read_errors += 1
                        self._alert(result, eid, k, "LEDGER_UNAVAILABLE" if e.source == "gateway" and
                                    e.kind != "pending" else "AUDIT_DELAYED")
                        continue
                    commit = next((e for e in snap["events"] if e["event_id"] == cp["event_id"] and
                                   e["type"] == "CHECKPOINT_COMMITTED" and e["checkpoint"] == k), None)
                    if commit is None:
                        self._alert(result, eid, k, "AUDIT_DELAYED")
                        continue
                    elapsed = self.journal.elapsed(eid, commit["event_id"], self.now())
                    seen = self.chain.assess(commit, snap["events"])
                    if seen.config_mismatch:
                        self._alert(result, eid, k, "CHAIN_CONFIG_MISMATCH")
                        # Do not publish even old queue items in this poll on a pin conflict.
                        blocked = "CHAIN_CONFIG_MISMATCH"
                        result.read_errors += 1
                        continue
                    cp_block = reported_anchor_block(commit)
                    if cp_block is None:
                        # An absent/incomplete Gateway commitment cannot start an audit, even at timeout.
                        self._alert(result, eid, k, "AUDIT_DELAYED")
                        continue
                    readiness = run_decision(True, True, seen.checkpoint_deep and seen.anchors_deep,
                                             elapsed, self.batch_seconds)
                    if not readiness["run"]:
                        self._alert(result, eid, k, readiness["alert"])
                        continue
                    # Gateway acceptance plus a matching commitment in its ledger is established.
                    # When local depth is unknown/insufficient after the timeout, still evaluate
                    # the off-chain rules, but never use a not-yet-confirmed item as a chain mismatch.
                    # The Gateway declares ACCEPTED only at D-deep on its own node. The
                    # independent reader may lag that node. After timeout a lagging reader
                    # supplies NO usable state for R4/R5; off-chain predicates still run.
                    snap["node"] = {"available": seen.node_available and seen.checkpoint_deep}
                    if seen.latest is not None and seen.latest >= self.pins.depth:
                        snap["ledger_last_block"] = seen.latest - self.pins.depth
                    for i, event in enumerate(snap["events"]):
                        a = event.get("anchor", {})
                        b = reported_anchor_block(event)
                        if a.get("status") == "ANCHORED" and (seen.latest is None or type(b) is not int or
                                                               b + self.pins.depth > seen.latest):
                            snap["events"][i] = {**event, "anchor": {"status": "PENDING"}}
                    snap["trigger"] = {"CLOSE_FINAL": "ELECTION_CLOSED", "TALLY": "TALLY"}.get(cp["reason"], "SCHEDULED")
                    outcomes = evaluate_all(snap)
                    for outcome in outcomes:
                        if outcome.coverage == "partial":
                            outcome = replace(outcome, summary=outcome.summary + " Checks not implemented: " +
                                              "; ".join(outcome.unimplemented_checks) + ".")
                        self.store.enqueue(build_finding(outcome, snap))
                    self.journal.completed(eid, cp["event_id"], self.now(), outcomes)
                    result.evaluated += 1
        if not blocked:
            sent = deliver(self.store, self.signer, self.gateway, self.core)
            result.stored, result.retry, result.rejected = sent.stored, sent.retry, sent.rejected
        return result


def _interval(name, default):
    value = int(os.environ.get(name, default))
    if not 1 <= value <= 86400:
        raise ValueError("Invalid interval")
    return value


def _secret(name):
    value = os.environ.get(name, "").strip()
    if not value or value == "<set>":
        raise ValueError("Missing service credential")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="PureVoting rule-engine polling service")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="single poll; print counts; nonzero on read/write failure")
    mode.add_argument("--healthcheck", action="store_true", help="worker liveness (loop heartbeat; not integration PASS)")
    args = parser.parse_args(argv)
    state = Path(os.environ.get("RULE_STATE_DIR", "/var/lib/purevoting/rules"))
    try:
        interval = _interval("RULE_POLL_SECONDS", 60)
        pulse = state / "heartbeat"
        if args.healthcheck:
            return 0 if pulse.is_file() and 0 <= time.time() - pulse.stat().st_mtime < max(180, 3 * interval) else 1
        pins = ChainPins.from_env()
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        signer = EIP191Signer(_secret("RULE_ENGINE_SIGNER_KEY"))
        key = _secret("RULE_KEY")
        with ExitStack() as stack:
            opts = dict(headers={"X-Service-Key": key}, timeout=10.0, trust_env=False, follow_redirects=False)
            core = stack.enter_context(httpx.Client(base_url=os.environ["CORE_URL"], **opts))
            gateway = stack.enter_context(httpx.Client(base_url=os.environ["GATEWAY_URL"], **opts))
            rpc = stack.enter_context(httpx.Client(timeout=5.0, trust_env=False, follow_redirects=False))
            store = stack.enter_context(FindingStore(state / "findings.sqlite3"))
            journal = stack.enter_context(closing(PollJournal(state / "poll.sqlite3", pins, signer)))
            worker = Worker(core, gateway, LiveInputs(core, gateway), PinnedChain(pins, rpc), pins, signer, store, journal,
                            batch_seconds=_interval("ANCHOR_BATCH_SECONDS", 60), heartbeat=pulse.touch,
                            partial_interval=_interval("RULE_PARTIAL_RECHECK_SECONDS", 600))
            while True:
                pulse.touch()
                r = worker.poll()
                print(json.dumps(asdict(r), sort_keys=True), flush=True)
                pulse.touch()
                if args.once:
                    return int(bool(r.read_errors or r.alert_errors or r.retry or r.rejected))
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Exception text can include the URL (including credentials), response, or evidence data.
        print("Rule worker stopped: " + type(exc).__name__ + ". Check configuration, contracts and state volume.", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
