"""Operational polling, idempotency, and fail-closed scheduler/transport regression tests."""
from contextlib import ExitStack, closing
from copy import deepcopy

import httpx
import pytest

from purevoting_rules.fixtures import development_cases
from purevoting_rules.live_chain import ChainPins, ChainVisibility
from purevoting_rules.live_inputs import InputError
from purevoting_rules.live_worker import PollJournal, Worker, main
from purevoting_rules.publication import CORE_URL, GATEWAY_URL, LocalServices
from purevoting_rules.signing import EIP191Signer
from purevoting_rules.storage import FindingStore


class Inputs:
    def __init__(self, snap):
        self.snap = snap
        self.problem = None
        self.status = "ACCEPTED"

    def elections(self):
        return [{"election_id": self.snap["election_id"]}]

    def checkpoints(self, eid):
        cp = next(e for e in self.snap["events"] if e["event_id"] == self.snap["checkpoint_event_id"])
        return [{"checkpoint": self.snap["checkpoint"], "ledger_status": self.status, "event_id": cp["event_id"],
                 "reason": cp["payload"]["reason"]}]

    def snapshot(self, eid, k, cps):
        if self.problem:
            raise self.problem
        return deepcopy(self.snap)


class Node:
    def __init__(self):
        self.visibility = ChainVisibility(True, True, None, 1500, True)

    def assess(self, *args):
        return self.visibility


class Rig:
    def __init__(self, tmp, *, body_failures=0, register_failures=0):
        self.tmp = tmp
        self.inputs = Inputs(development_cases(8)[0]["snapshot"])
        self.pins = ChainPins("99", "0x" + "2"*40, ("https://observer.invalid",), 2)
        self.gw_config = {"platform":"other", "interface_version":"1.1.0", "rpc_urls":list(self.pins.urls),
                          "chain_id":"99", "confirmation_depth":2, "contract_address":self.pins.contract_address}
        self.signer = EIP191Signer()
        self.backend = LocalServices(self.signer.address, "test-only", {
            self.inputs.snap["election_id"]: self.inputs.snap["checkpoint"]},
            transient_body_failures=body_failures, transient_gateway_failures=register_failures)
        self.node = Node()
        self.clock = 1000
        self.alerts = []
        self.stack = ExitStack()

        def handle(r):
            if r.url.path == "/v1/chain":
                return httpx.Response(200,json=self.gw_config)
            if r.url.path == "/internal/v1/audit-alerts":
                import json
                self.alerts.append(json.loads(r.content))
                return httpx.Response(202)
            return self.backend.handle(r)
        opts = {"transport":httpx.MockTransport(handle), "headers":{"X-Service-Key":"test-only"}}
        self.core = self.stack.enter_context(httpx.Client(base_url=CORE_URL, **opts))
        self.gateway = self.stack.enter_context(httpx.Client(base_url=GATEWAY_URL, **opts))
        self.restart()

    def restart(self):
        if getattr(self, "journal", None):
            self.journal.close()
            self.store.close()
        self.store = self.stack.enter_context(FindingStore(self.tmp/"queue.db"))
        self.journal = self.stack.enter_context(closing(PollJournal(self.tmp/"seen.db", self.pins, self.signer)))
        self.worker = Worker(self.core, self.gateway, self.inputs, self.node, self.pins, self.signer,
                             self.store, self.journal, now=lambda: self.clock)

    def close(self):
        self.stack.close()


def test_signed_publication_resumes_exact_body_after_restart(tmp_path):
    with closing(Rig(tmp_path, body_failures=1)) as r:
        first = r.worker.poll()
        assert (first.evaluated, first.stored, first.retry, first.rejected) == (1, 4, 1, 0)
        r.restart()
        second = r.worker.poll()
        assert len(r.backend.registered_records) == len(r.backend.stored_bodies) == 5
        assert second.stored == 1
        assert sorted(len(v) for v in r.backend.body_attempts.values()) == [1,1,1,1,2]
        assert all(len(set(raws)) == 1 for raws in r.backend.body_attempts.values())
        records = r.store.records()
        assert all(x["state"] == "STORED" for x in records)
        assert [(x["rule_id"], x["result"]) for x in records] == [
            ("R1","PASS"),("R2","PASS"),("R3","PASS"),("R4","INCONCLUSIVE"),("R5","INCONCLUSIVE")]
        assert all("not implemented" in x["body"]["summary"] for x in records if x["rule_id"] in {"R4","R5"})


def test_preflight_mismatch_stops_even_pending_publication(tmp_path):
    with closing(Rig(tmp_path, register_failures=5)) as r:
        assert r.worker.poll().retry == 5
        r.gw_config["chain_id"] = "777"
        second = r.worker.poll()
        assert second.evaluated == 0
        assert not r.backend.registered_records and not r.backend.stored_bodies
        assert r.alerts[-1]["kind"] == "CHAIN_CONFIG_MISMATCH"


@pytest.mark.parametrize(("source","kind","alert"),[
    ("gateway","unavailable","LEDGER_UNAVAILABLE"),("gateway","pending","AUDIT_DELAYED"),
    ("core","invalid","AUDIT_DELAYED")])
def test_incomplete_inputs_suppress_findings(tmp_path, source, kind, alert):
    with closing(Rig(tmp_path)) as r:
        r.inputs.problem = InputError(source, kind)
        assert r.worker.poll().evaluated == 0 and not r.store.records()
        assert r.alerts[-1]["kind"] == alert


def test_pending_or_rejected_core_checkpoint_produces_no_finding(tmp_path):
    with closing(Rig(tmp_path)) as r:
        r.inputs.status = "PENDING"
        assert r.worker.poll().alerts == 1
        r.inputs.status = "REJECTED"
        assert r.worker.poll().alerts == 0
        assert not r.store.records()


def test_unavailable_node_timeout_survives_restart_without_false_failure(tmp_path):
    with closing(Rig(tmp_path)) as r:
        r.node.visibility = ChainVisibility(False,False,"NODE_UNAVAILABLE",None,False)
        assert r.worker.poll().evaluated == 0
        r.clock += 179
        r.restart()
        assert r.worker.poll().evaluated == 0
        r.clock += 1
        stats = r.worker.poll()
        assert stats.stored == 5
        rows = r.store.records()
        assert rows[-1]["body"]["inconclusive_reason"] == "NODE_UNAVAILABLE"
        assert rows[-1]["body"]["observed"]["events_unanchored"] > 0
        assert [x["result"] for x in rows[:3]] == ["PASS"] * 3
        # Revisit the same checkpoint after a recoverable evidence failure.
        r.node.visibility = ChainVisibility(True,True,None,1500,True)
        assert r.worker.poll().stored >= 1
        assert len([x for x in r.store.records() if x["rule_id"] == "R1"]) == 1


def test_rpc_identity_conflict_blocks_even_after_timeout(tmp_path):
    with closing(Rig(tmp_path)) as r:
        r.node.visibility = ChainVisibility(False,False,"NODE_UNAVAILABLE",None,False,True)
        assert r.worker.poll().evaluated == 0
        r.clock += 3600
        assert r.worker.poll().evaluated == 0
        assert {a["kind"] for a in r.alerts} == {"CHAIN_CONFIG_MISMATCH"}
        assert not r.store.records()


def test_healthcheck_needs_fresh_heartbeat_without_network_or_secrets(tmp_path,monkeypatch):
    monkeypatch.setenv("RULE_STATE_DIR", str(tmp_path))
    assert main(["--healthcheck"]) == 1
    (tmp_path/"heartbeat").touch()
    assert main(["--healthcheck"]) == 0


def test_state_volume_cannot_be_reused_for_other_signer(tmp_path):
    with closing(Rig(tmp_path)) as r:
        with pytest.raises(ValueError, match="different deployment"):
            PollJournal(tmp_path/"seen.db", r.pins, EIP191Signer())


def test_lagging_pinned_node_does_not_suppress_offchain_rules_after_timeout(tmp_path):
    with closing(Rig(tmp_path)) as r:
        r.node.visibility = ChainVisibility(False,False,"NODE_UNAVAILABLE",100,True)
        assert r.worker.poll().evaluated == 0
        r.clock += 179
        assert r.worker.poll().evaluated == 0 and not r.store.records()
        r.clock += 1
        assert r.worker.poll().stored == 5
        rows = r.store.records()
        assert [row["result"] for row in rows[:3]] == ["PASS"] * 3
        assert all(row["result"] == "INCONCLUSIVE" and row["body"]["inconclusive_reason"] == "NODE_UNAVAILABLE"
                   for row in rows[3:])
        assert {a["kind"] for a in r.alerts} == {"AUDIT_DELAYED"}


def test_partial_rules_rechecked_at_interval_not_every_poll(tmp_path):
    with closing(Rig(tmp_path)) as r:
        assert r.worker.poll().evaluated == 1
        r.clock += 599
        assert r.worker.poll().skipped == 1
        r.restart()
        r.clock += 1
        assert r.worker.poll().evaluated == 1
        assert len(r.store.records()) == 5


def test_clock_starts_before_gateway_read_succeeds(tmp_path):
    with closing(Rig(tmp_path)) as r:
        r.inputs.problem = InputError("gateway", "unavailable")
        assert r.worker.poll().evaluated == 0
        r.clock += 180
        r.restart()
        r.inputs.problem = None
        s = r.inputs.snap["events"]
        # One voter anchor is pending; the accepted system checkpoint itself is confirmed.
        for ev in s:
            if ev["type"] == "TOKEN_USED":
                ev["anchor"] = {"status": "PENDING"}
                break
        r.node.visibility = ChainVisibility(True,False,"BATCH_NOT_ANCHORED",1500,True)
        assert r.worker.poll().evaluated == 1
        assert r.store.records()[-1]["body"]["inconclusive_reason"] == "BATCH_NOT_ANCHORED"


def test_later_finding_cannot_overtake_older_same_rule_on_retry(tmp_path):
    from purevoting_rules.engine import evaluate_rule
    from purevoting_rules.findings import build_finding
    from purevoting_rules.live_publish import deliver
    with closing(Rig(tmp_path, register_failures=1)) as r:
        snap = deepcopy(r.inputs.snap)
        # Input unavailable, then recovered: the on-chain PASS must be recorded later.
        snap["views"]["token_status"] = None
        f1 = r.store.enqueue(build_finding(evaluate_rule("R1", snap), snap))
        snap = deepcopy(r.inputs.snap)
        f2 = r.store.enqueue(build_finding(evaluate_rule("R1", snap), snap))
        assert f1 != f2
        assert deliver(r.store, r.signer, r.gateway, r.core).retry == 2
        assert not r.backend.registered_records
        assert deliver(r.store, r.signer, r.gateway, r.core).stored == 2
        assert list(r.backend.registered_records) == [f1, f2]


def test_gateway_pending_commitment_never_starts_audit(tmp_path):
    with closing(Rig(tmp_path)) as r:
        commit = next(e for e in r.inputs.snap["events"]
                      if e["event_id"] == r.inputs.snap["checkpoint_event_id"])
        commit["anchor"] = {"status": "PENDING"}
        for now in (1000, 1500):
            r.clock = now
            assert r.worker.poll().evaluated == 0
        assert not r.store.records()
        assert {a["kind"] for a in r.alerts} == {"AUDIT_DELAYED"}
