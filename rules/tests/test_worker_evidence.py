"""Polling deadlines follow actual proof availability, not Gateway anchor claims.

Uses real LiveInputs HTTP validation, the real evaluator and durable outbox with
mock HTTP publication. No real network, deployment or finalization is claimed.
"""
from contextlib import closing, contextmanager
from copy import deepcopy

import pytest

from test_anchor_inputs import AnchorAPI, client
from test_live_worker import Rig


@contextmanager
def evidence_http(rig):
    api = AnchorAPI()
    api.snapshot = rig.inputs.snap
    api.evidence = deepcopy(rig.inputs.proofs)
    api.evidence["anchors"].sort(key=lambda a: (a["block_number"], a["anchor_id"]))
    with client(api) as reader:
        rig.inputs.anchor_evidence = reader.anchor_evidence
        yield api


def test_selected_proof_404_waits_until_deadline_then_recovers_without_duplicate_clean_rules(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig) as api:
        voter = next(e for e in rig.inputs.snap["events"] if e["type"] == "TOKEN_USED")
        missing = "/v1/events/" + voter["event_id"] + "/proof"
        api.errors[missing] = 404
        assert rig.node.visibility.checkpoint_deep and rig.node.visibility.anchors_deep
        assert rig.worker.poll().evaluated == 0
        assert not rig.store.records()
        rig.clock += 179
        rig.restart()
        assert rig.worker.poll().evaluated == 0
        assert not rig.store.records()
        rig.clock += 1
        result = rig.worker.poll()
        assert (result.evaluated, result.stored, result.retry, result.rejected) == (1, 5, 0, 0)
        stored = rig.store.records()
        assert [(r["rule_id"], r["result"]) for r in stored] == [
            ("R1", "PASS"), ("R2", "PASS"), ("R3", "PASS"), ("R4", "PASS"), ("R5", "INCONCLUSIVE")]
        assert stored[-1]["body"]["inconclusive_reason"] == "BATCH_NOT_ANCHORED"
        old_r5 = stored[-1]["finding_id"]
        # Recovery gets an immediate retry; it must not wait the partial-coverage
        # interval or enqueue identical clean-rule bodies with new IDs.
        del api.errors[missing]
        rig.clock += 1
        result = rig.worker.poll()
        assert (result.evaluated, result.stored) == (1, 1)
        stored = rig.store.records()
        assert len(stored) == 6
        for rule in ("R1", "R2", "R3", "R4"):
            assert len([r for r in stored if r["rule_id"] == rule]) == 1
        r5s = [r for r in stored if r["rule_id"] == "R5"]
        assert [r["result"] for r in r5s] == ["INCONCLUSIVE", "PASS"]
        assert r5s[0]["finding_id"] == old_r5 and r5s[1]["finding_id"] != old_r5
        assert len({(r["election_id"], r["checkpoint"]) for r in r5s}) == 1
        published_r5 = [r["request"] for r in rig.backend.registered_records.values() if r["request"]["rule_id"] == "R5"]
        assert [r["result"] for r in published_r5] == ["INCONCLUSIVE", "PASS"]
        # This verifies the ordered later PASS used for contract supersession;
        # the publication double does not simulate the contract review status.
        assert rig.worker.poll().skipped == 1
        assert len(rig.store.records()) == 6


def test_unavailable_anchor_index_uses_bounded_fallback_and_recovers(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig) as api:
        api.errors["/v1/anchors"] = 503
        first = rig.worker.poll()
        assert first.evaluated == 0 and first.read_errors == 1
        assert not rig.store.records()
        assert {a["kind"] for a in rig.alerts} == {"LEDGER_UNAVAILABLE", "AUDIT_DELAYED"}
        rig.clock += 180
        second = rig.worker.poll()
        assert (second.evaluated, second.stored, second.read_errors) == (1, 5, 1)
        rows = rig.store.records()
        assert [r["result"] for r in rows[:4]] == ["PASS"] * 4
        assert rows[-1]["result"] == "INCONCLUSIVE"
        assert rows[-1]["body"]["inconclusive_reason"] == "BATCH_NOT_ANCHORED"
        del api.errors["/v1/anchors"]
        assert rig.worker.poll().stored == 1
        assert rig.store.records()[-1]["result"] == "PASS"


def test_invalid_proof_response_aborts_all_findings_even_after_deadline(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig) as api:
        def malformed(path, query, data):
            if path.endswith("/proof"):
                data["leaf_hash"] = "malformed"
            return data
        api.change = malformed
        for now in (1000, 1180, 2000):
            rig.clock = now
            result = rig.worker.poll()
            assert result.evaluated == 0 and result.read_errors == 1
            assert not rig.store.records()
        assert {a["kind"] for a in rig.alerts} == {"LEDGER_UNAVAILABLE"}


def test_settled_checkpoint_with_no_pending_publication_is_not_realerted_by_preflight(tmp_path):
    with closing(Rig(tmp_path)) as rig:
        assert rig.worker.poll().stored == 5
        prior_alerts = list(rig.alerts)
        rig.gw_config["chain_id"] = "777"
        result = rig.worker.poll()
        assert result.evaluated == 0 and result.skipped == 1
        assert result.read_errors == 1 and result.alerts == 0
        assert rig.alerts == prior_alerts
        assert len(rig.backend.stored_bodies) == 5


@pytest.mark.parametrize("failures", [{"register_failures": 5}, {"body_failures": 1}])
def test_settled_checkpoint_still_alerts_when_preflight_blocks_pending_publication(tmp_path, failures):
    with closing(Rig(tmp_path, **failures)) as rig:
        first = rig.worker.poll()
        assert first.evaluated == 1 and first.retry > 0
        requests = len(rig.backend.requests)
        rig.gw_config["chain_id"] = "777"
        result = rig.worker.poll()
        assert result.evaluated == 0 and result.skipped == 1
        assert result.alerts == 1 and rig.alerts[-1]["kind"] == "CHAIN_CONFIG_MISMATCH"
        assert len(rig.backend.requests) == requests  # No publication on conflicting pins.


def test_actual_getter_unavailability_honors_deadline_despite_deep_packet_metadata(tmp_path):
    with closing(Rig(tmp_path)) as rig:
        assert all(e["anchor"]["status"] == "ANCHORED" for e in rig.inputs.snap["events"])
        assert rig.node.visibility.checkpoint_deep and rig.node.visibility.anchors_deep
        rig.node.collect = lambda snapshot, evidence: {"available": False, "reason": "NODE_UNAVAILABLE"}
        assert rig.worker.poll().evaluated == 0
        rig.clock += 179
        assert rig.worker.poll().evaluated == 0
        assert not rig.store.records()
        rig.clock += 1
        assert rig.worker.poll().stored == 5
        rows = rig.store.records()
        assert [r["result"] for r in rows[:3]] == ["PASS"] * 3
        assert all(r["result"] == "INCONCLUSIVE" and r["body"]["inconclusive_reason"] == "NODE_UNAVAILABLE" for r in rows[3:])
        assert all("ledger_last_block" not in r["body"]["data_as_of"] for r in rows)
        assert {a["kind"] for a in rig.alerts} == {"AUDIT_DELAYED"}


def test_collected_confirmed_height_replaces_older_scheduling_height(tmp_path):
    from purevoting_rules.live_chain import ChainVisibility
    with closing(Rig(tmp_path)) as rig:
        # The scheduling observation lagged, but the later independent collection
        # succeeds on a ready node. It must not be masked with the old head.
        rig.node.visibility = ChainVisibility(False, False, "NODE_UNAVAILABLE", 100, True)
        rig.node.collect = lambda snapshot, evidence: deepcopy(rig.node.evidence)
        assert rig.worker.poll().evaluated == 0
        rig.clock += 180
        assert rig.worker.poll().stored == 5
        assert all(r["result"] == "PASS" for r in rig.store.records())
