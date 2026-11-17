"""PR #12: honest anchoring/index races must not publish irreversible FAILs.

Use the actual HTTP input reader, evaluator, journal and durable outbox. Node
responses/publication are controlled fixtures, not a deployed-chain claim.
"""
from contextlib import closing
from copy import deepcopy

from purevoting_rules.hashing import H
from test_live_worker import Rig
from test_worker_evidence import evidence_http


def _old_checkpoint(rig):
    s = rig.inputs.snap
    rig.journal.elapsed(s["election_id"], s["checkpoint_event_id"], rig.clock - 1000)


def _hide_anchor(rig, api):
    event = next(e for e in rig.inputs.snap["events"] if e["type"] == "TOKEN_USED")
    identity = event["anchor"]["anchor_id"]
    record = next(a for a in api.evidence["anchors"] if a["anchor_id"] == identity)
    api.evidence["anchors"].remove(record)
    return identity, record


def _r5(rig):
    return [r for r in rig.store.records() if r["rule_id"] == "R5"]


def _assert_no_fail(rig):
    assert all(r["result"] != "FAIL" for r in rig.store.records())
    assert all(r["request"]["result"] != "FAIL" for r in rig.backend.registered_records.values())


def test_cached_pending_event_with_later_confirmed_tree_recovers_without_any_fail(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig):
        event = next(e for e in rig.inputs.snap["events"] if e["type"] == "TOKEN_USED")
        anchored = deepcopy(event["anchor"])
        event["anchor"] = {"status": "PENDING"}
        # The actual reader keeps this cached selected body while the independent
        # node and Gateway tree already contain the newly confirmed anchor.
        assert rig.worker.poll().evaluated == 0
        rig.clock += 180
        assert rig.worker.poll().stored == 5
        first = _r5(rig)[0]
        assert first["result"] == "INCONCLUSIVE"
        assert first["body"]["inconclusive_reason"] == "BATCH_NOT_ANCHORED"
        assert first["body"]["observed"]["anchors_mismatch"] == 0
        _assert_no_fail(rig)
        event["anchor"] = anchored
        rig.clock += 1
        assert rig.worker.poll().stored == 1
        assert [r["result"] for r in _r5(rig)] == ["INCONCLUSIVE", "PASS"]
        _assert_no_fail(rig)


def test_old_checkpoint_gets_new_per_anchor_wait_and_restart_preserves_it(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig) as api:
        _old_checkpoint(rig)
        identity, _ = _hide_anchor(rig, api)
        assert rig.worker.poll().stored == 5
        first = _r5(rig)[0]
        assert first["result"] == "INCONCLUSIVE"
        assert first["body"]["inconclusive_reason"] == "BATCH_NOT_ANCHORED"
        assert first["body"]["observed"]["anchors_mismatch"] == 0
        assert first["body"]["observed"]["chain_anchor_ids_missing_from_gateway"] == [H(identity)]
        rig.clock += 179
        rig.restart()
        assert rig.worker.poll().stored == 0
        _assert_no_fail(rig)
        index_reads = sum(path == "/v1/anchors" for path, _ in api.calls)
        rig.clock += 1
        assert rig.worker.poll().stored == 1
        assert sum(path == "/v1/anchors" for path, _ in api.calls) > index_reads
        rows = _r5(rig)
        assert [r["result"] for r in rows] == ["INCONCLUSIVE", "FAIL"]
        assert rows[-1]["body"]["observed"]["anchors_mismatch"] == 1


def test_gateway_index_catchup_before_deadline_publishes_only_recovered_r5(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig) as api:
        _old_checkpoint(rig)
        _, record = _hide_anchor(rig, api)
        assert rig.worker.poll().stored == 5
        rig.clock += 30
        api.evidence["anchors"].append(record)
        api.evidence["anchors"].sort(key=lambda a: (a["block_number"], a["anchor_id"]))
        assert rig.worker.poll().stored == 1
        assert [r["result"] for r in _r5(rig)] == ["INCONCLUSIVE", "PASS"]
        assert len(rig.store.records()) == 6
        assert rig.journal.db.execute("SELECT COUNT(*) FROM anchor_index_gaps").fetchone()[0] == 0
        _assert_no_fail(rig)


def test_unavailable_index_after_expiry_does_not_confirm_a_gateway_omission(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig) as api:
        _old_checkpoint(rig)
        _hide_anchor(rig, api)
        assert rig.worker.poll().stored == 5
        rig.clock += 180
        api.errors["/v1/anchors"] = 503
        assert rig.worker.poll().read_errors == 1
        _assert_no_fail(rig)
        del api.errors["/v1/anchors"]
        rig.clock += 1
        assert rig.worker.poll().stored == 1
        assert _r5(rig)[-1]["result"] == "FAIL"


def test_index_read_started_before_deadline_cannot_mature_during_slow_proof_reads(tmp_path):
    with closing(Rig(tmp_path)) as rig, evidence_http(rig) as api:
        _old_checkpoint(rig)
        _hide_anchor(rig, api)
        assert rig.worker.poll().stored == 5
        rig.clock += 179

        def slow_tree(path, query, data):
            if path.endswith("/leaves"):
                rig.clock = 1181  # Index was fetched at 1179, before the 1180 deadline.
            return data

        api.change = slow_tree
        assert rig.worker.poll().stored == 0
        _assert_no_fail(rig)
        api.change = lambda path, query, data: data
        assert rig.worker.poll().stored == 1  # This fresh index read starts at 1181.
        assert _r5(rig)[-1]["result"] == "FAIL"


def test_new_missing_commitments_do_not_inherit_an_older_anchors_wait(tmp_path):
    with closing(Rig(tmp_path)) as rig:
        observe = rig.journal.confirmed_anchor_omissions
        assert observe("e", "cp", {"a": "commit-a"}, 0, 0, 180) == []
        assert observe("e", "cp", {"a": "commit-a", "b": "commit-b"}, 179, 179, 180) == []
        assert observe("e", "cp", {"a": "commit-a", "b": "commit-b"}, 180, 180, 180) == ["a"]
        assert observe("e", "cp", {"a": "commit-a", "b": "commit-b"}, 359, 359, 180) == ["a", "b"]


def test_recovered_or_changed_commitment_receives_a_new_wait(tmp_path):
    with closing(Rig(tmp_path)) as rig:
        observe = rig.journal.confirmed_anchor_omissions
        assert observe("e", "cp", {"a": "old-root-and-tx"}, 0, 0, 180) == []
        assert observe("e", "cp", {"a": "new-root-and-tx"}, 180, 180, 180) == []
        assert observe("e", "cp", {"a": "new-root-and-tx"}, 359, 359, 180) == []
        assert observe("e", "cp", {"a": "new-root-and-tx"}, 360, 360, 180) == ["a"]
        assert observe("e", "cp", {}, 361, 361, 180) == []
        assert observe("e", "cp", {"a": "new-root-and-tx"}, 500, 500, 180) == []


def test_slow_initial_collection_and_backwards_clock_never_shorten_the_wait(tmp_path):
    with closing(Rig(tmp_path)) as rig:
        observe = rig.journal.confirmed_anchor_omissions
        assert observe("e", "cp", {"a": "root"}, 0, 200, 180) == []
        assert observe("e", "cp", {"a": "root"}, 379.9, 381, 180) == []
        assert observe("e", "cp", {"a": "root"}, 380, 381, 180) == ["a"]
        assert observe("e", "cp", {"a": "root"}, 400, 100, 180) == []
        assert observe("e", "cp", {"a": "root"}, 279.9, 280, 180) == []
