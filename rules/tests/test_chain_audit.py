"""Independent state/anchor evidence: a claimed block number is not a proof."""
from copy import deepcopy

import pytest

from purevoting_rules.chain_audit import audit_chain, gateway_anchor_omissions
from purevoting_rules.hashing import H, Hc, leaf_hash, rejections_root


def clean_snapshot():
    """A small fully anchored creation/freeze/checkpoint, independently specified."""
    election = "elc_" + "1" * 26
    stamp = "2026-11-17T00:00:00Z"
    unix = 1794873600
    orgs = [{"org_id": "org_" + str(i), "signer": "0x" + str(i) * 40} for i in range(1, 4)]
    creation = {"organizations": orgs, "operator_org": "org_1", "approval_threshold": 2,
                "proposal_ttl_hours": 24, "checkpoint_interval_seconds": 60,
                "window_start": stamp, "window_end": "2026-11-17T00:10:00Z",
                "time_bucket_seconds": 30, "config_hash": H("config")}
    spec = [("ELECTION_CREATED", creation), ("ROSTER_FROZEN", {"roster_hash": H("roster")}),
            ("CHECKPOINT_COMMITTED", {"reason": "STATE_CHANGE", "tables": []})]
    events, records, node_anchors, logs, leaves, proofs = [], [], {}, {}, {}, {}
    for i, (kind, payload) in enumerate(spec, 1):
        eid, aid = "evt_" + str(i) * 26, "anc_" + str(i) * 26
        tx = "0x" + str(i) * 64
        event = {"schema_version": "1.0", "event_id": eid, "election_id": election, "source": "core",
                 "seq": i, "checkpoint": 0, "type": kind, "occurred_at": stamp,
                 "payload": payload, "payload_hash": Hc(payload)}
        digest = leaf_hash(event)
        event["anchor"] = {"status": "ANCHORED", "anchor_id": aid, "mode": "immediate",
                           "merkle_root": digest, "block_number": i, "tx_hash": tx}
        events.append(event)
        record = {"anchor_id": aid, "election_id": election, "mode": "immediate", "merkle_root": digest,
                  "rejections_root": H(""), "event_count": 1, "block_number": i, "tx_hash": tx, "anchored_at": stamp}
        records.append(record)
        node_anchors[H(aid)] = {"mode": "immediate", "merkle_root": digest, "rejections_root": H(""),
                               "event_count": 1, "bucket_min": unix, "bucket_max": unix}
        logs[H(aid)] = {key: record[key] for key in ("mode", "merkle_root", "rejections_root", "event_count", "block_number", "tx_hash")}
        leaves[aid] = {"anchor_id": aid, "leaves": [{"event_id": eid, "leaf_hash": digest}],
                       "rejection_ids": [], "rejections_root": H("")}
        proofs[eid] = {"event_id": eid, "anchor_id": aid, "leaf_hash": digest, "merkle_root": digest,
                       "path": [], "block_number": i, "tx_hash": tx}
    saved = {"status": "ROSTER_FROZEN", "window_end": unix + 600, "roster_hash": H("roster"),
             "token_set_root": "0" * 64, "result_hash": "0" * 64, "last_core_seq": 3}
    state = dict(saved, window_start=unix, time_bucket_seconds=30, config_hash=H("config"),
                 last_checkpoint=0, operator_org_id=H("org_1"))
    chain = {"available": True, "reason": None, "confirmed_block": 5, "chain_id": 7777,
             "contract_address": "0x" + "ab" * 20, "state": state, "checkpoint_state": saved,
             "governance": {"org_ids": [H(o["org_id"]) for o in orgs], "approval_threshold": 2,
                            "operator_org_id": H("org_1"), "proposal_ttl_hours": 24, "checkpoint_interval_seconds": 60},
             "registry": {o["org_id"]: {"signer": o["signer"], "registered_by": o["signer"]} for o in orgs},
             "operator": {"org_id": H("org_1"), "confirmed_by_all": True}, "proposals": {},
             "anchors": node_anchors, "anchor_logs": logs}
    return {"election_id": election, "checkpoint": 0, "checkpoint_event_id": events[-1]["event_id"],
            "events": events, "rejections": [], "views": {"election_config": [{"organizations": orgs}]}, "chain": chain,
            "anchor_evidence": {"complete": True, "anchors": records, "leaves": leaves, "proofs": proofs,
                                "events": {e["event_id"]: e for e in events}, "rejections": {}}}


def audit(snapshot):
    out, refs = {"events_hash_mismatch": 0}, []
    failed, reason = audit_chain(snapshot, snapshot["events"], snapshot["rejections"], out, refs)
    return failed, reason, out, refs


def test_full_independent_checkpoint_and_merkle_evidence_passes():
    failed, reason, out, refs = audit(clean_snapshot())
    assert not failed and reason is None
    assert all(out[key] == 0 for key in ("chain_state_mismatch", "governance_mismatch", "anchors_mismatch", "events_unanchored"))


@pytest.mark.parametrize(("section", "key", "value", "counter"), [
    ("state", "config_hash", H("changed"), "governance_mismatch"),
    ("state", "window_start", 2, "governance_mismatch"),
    ("state", "time_bucket_seconds", 99, "governance_mismatch"),
    ("governance", "org_ids", [H("intruder")], "governance_mismatch"),
    ("governance", "approval_threshold", 1, "governance_mismatch"),
    ("governance", "operator_org_id", H("other"), "governance_mismatch"),
    ("governance", "proposal_ttl_hours", 1, "governance_mismatch"),
    ("governance", "checkpoint_interval_seconds", 99, "governance_mismatch"),
    ("checkpoint_state", "roster_hash", H("stuffed roster"), "chain_state_mismatch"),
    ("checkpoint_state", "status", "OPEN", "chain_state_mismatch"),
    ("checkpoint_state", "window_end", 2, "chain_state_mismatch"),
    ("checkpoint_state", "token_set_root", H("stuffed token"), "chain_state_mismatch"),
    ("checkpoint_state", "result_hash", H("forged result"), "chain_state_mismatch"),
    ("checkpoint_state", "last_core_seq", 2, "chain_state_mismatch"),
])
def test_proven_contract_discrepancies_fail(section, key, value, counter):
    s = clean_snapshot()
    s["chain"][section][key] = value
    failed, reason, out, _ = audit(s)
    assert failed and out[counter] >= 1


def test_later_mutable_contract_progress_does_not_change_old_checkpoint():
    s = clean_snapshot()
    s["chain"]["state"].update(status="TALLIED", window_end=9999, roster_hash=H("later"), result_hash=H("later"), last_core_seq=999)
    assert audit(s)[:2] == (False, None)


@pytest.mark.parametrize("missing", ["chain", "checkpoint_state", "state", "governance", "anchors", "anchor_logs"])
def test_missing_independent_evidence_cannot_pass(missing):
    s = clean_snapshot()
    if missing == "chain":
        del s["chain"]
    else:
        s["chain"][missing] = None
    failed, reason, _, _ = audit(s)
    assert not failed and reason == "NODE_UNAVAILABLE"


def test_lagging_node_is_inconclusive_not_an_anchor_mismatch():
    s = clean_snapshot()
    s["chain"]["confirmed_block"] = 0
    s["chain"]["checkpoint_state"] = None
    failed, reason, out, _ = audit(s)
    assert not failed and reason == "NODE_UNAVAILABLE"
    assert out["anchors_mismatch"] == 0 and out["events_unanchored"] == 3


def test_gateway_root_and_proof_cannot_replace_node_root():
    s = clean_snapshot()
    s["chain"]["anchors"][next(iter(s["chain"]["anchors"]))]["merkle_root"] = H("different node root")
    failed, _, out, refs = audit(s)
    assert failed and out["anchors_mismatch"] == 1 and any(r["kind"] == "anchor" for r in refs)


def test_gateway_future_block_claim_cannot_hide_conflict_with_existing_confirmed_log():
    s = clean_snapshot()
    s["anchor_evidence"]["anchors"][0]["block_number"] = 999999
    failed, _, out, _ = audit(s)
    assert failed and out["anchors_mismatch"] == 1


def test_gateway_cannot_reorder_creation_organizations_behind_sorted_hashes():
    s = clean_snapshot()
    s["events"][0]["payload"]["organizations"].reverse()
    failed, _, out, _ = audit(s)
    assert failed and out["governance_mismatch"] > 0


@pytest.mark.parametrize(("field", "value"), [("block_number", 4), ("tx_hash", "0x" + "a" * 64),
                                                  ("anchor_id", "anc_" + "9" * 26), ("leaf_hash", H("wrong")),
                                                  ("path", [{"position": "right", "hash": H("extra sibling")}])])
def test_wrong_proof_metadata_or_path_with_confirmed_anchor_fails(field, value):
    s = clean_snapshot()
    proof = s["anchor_evidence"]["proofs"][s["events"][0]["event_id"]]
    proof[field] = value
    assert audit(s)[0] is True


def test_missing_proof_or_leaf_body_stays_inconclusive():
    s = clean_snapshot()
    s["anchor_evidence"]["proofs"].pop(s["events"][0]["event_id"])
    assert audit(s)[:2] == (False, "BATCH_NOT_ANCHORED")


def test_changed_stored_event_is_detected_by_anchored_leaf():
    s = clean_snapshot()
    s["events"][0]["payload"]["approval_threshold"] = 1
    s["events"][0]["payload_hash"] = Hc(s["events"][0]["payload"])
    failed, _, out, _ = audit(s)
    assert failed and out["events_hash_mismatch"] > 0 and out["anchors_mismatch"] > 0


def test_rejections_are_rehashed_against_node_root_and_membership():
    s = clean_snapshot()
    aid = s["anchor_evidence"]["anchors"][0]["anchor_id"]
    rejection = {"rejection_id": "rej_" + "7" * 26, "election_id": s["election_id"],
                 "event_id": "evt_" + "8" * 26, "type": "ELECTION_OPENED", "checkpoint": 0,
                 "reject_code": "INVALID_STATE_TRANSITION", "recorded_at": "2026-11-17T00:00:00Z",
                 "payload_hash": H("rejected event"), "subject_hash": None, "anchor_id": aid}
    root = rejections_root([rejection])
    s["rejections"] = [rejection]
    s["anchor_evidence"]["rejections"][rejection["rejection_id"]] = rejection
    s["anchor_evidence"]["leaves"][aid].update(rejection_ids=[rejection["rejection_id"]], rejections_root=root)
    s["anchor_evidence"]["anchors"][0]["rejections_root"] = root
    s["chain"]["anchors"][H(aid)]["rejections_root"] = root
    s["chain"]["anchor_logs"][H(aid)]["rejections_root"] = root
    assert audit(s)[:2] == (False, None)
    rejection["reject_code"] = "OTHER"
    assert audit(s)[0] is True


def test_omitted_confirmed_anchor_requires_waited_reread_node_log_and_fixed_boundary():
    s = clean_snapshot()
    key = H("anc_" + "9" * 26)
    s["chain"]["anchors"][key] = deepcopy(next(iter(s["chain"]["anchors"].values())))
    # Merely having anchorAt without the confirmed log does not prove omission.
    assert audit(s)[:2] == (False, "BATCH_NOT_ANCHORED")
    s["chain"]["anchor_logs"][key] = dict(next(iter(s["chain"]["anchor_logs"].values())), block_number=1)
    failed, reason, out, _ = audit(s)
    assert not failed and reason == "BATCH_NOT_ANCHORED"
    assert out["anchors_mismatch"] == 0
    assert out["chain_anchor_ids_missing_from_gateway"] == [key]
    assert gateway_anchor_omissions(s["chain"], s["anchor_evidence"]) == [key]
    # Only the worker can establish this evidence after a persisted bounded wait
    # and a fresh complete /v1/anchors response still omitting the same anchor.
    s["confirmed_gateway_anchor_omissions"] = [key]
    assert audit(s)[0] is True
    # Voter batches can be confirmed AFTER the checkpoint. The fixed node read
    # happens before the Gateway index read, so they cannot be silently ignored.
    s["chain"]["anchor_logs"][key]["block_number"] = 4
    assert audit(s)[0] is True
    # A log beyond the fixed confirmed read cannot establish an omission.
    s["chain"]["anchor_logs"][key]["block_number"] = 6
    assert audit(s)[0] is False


@pytest.mark.parametrize("body_pending", [True, False])
def test_snapshot_pending_before_anchor_collection_is_unavailable_and_recovers(body_pending):
    s = clean_snapshot()
    complete = deepcopy(s)
    event = s["events"][0]
    # The body in the selected snapshot can be older than the later anchor
    # listing. It can also remain the same cached PENDING object in that listing.
    s["events"][0] = dict(event, anchor={"status": "PENDING"})
    if body_pending:
        s["anchor_evidence"]["events"][event["event_id"]] = deepcopy(s["events"][0])
    failed, reason, out, _ = audit(s)
    assert not failed and reason == "BATCH_NOT_ANCHORED"
    assert out["anchors_mismatch"] == 0 and out["events_hash_mismatch"] == 0
    assert out["events_unanchored"] == 1
    # A subsequent honest snapshot is clean; no irreversible FAIL was needed.
    assert audit(complete)[:2] == (False, None)


@pytest.mark.parametrize("tamper", ["body", "root"])
def test_pending_metadata_does_not_mask_proven_immutable_tampering(tamper):
    s = clean_snapshot()
    event = s["events"][0]
    event["anchor"] = {"status": "PENDING"}
    if tamper == "body":
        event["payload"]["config_hash"] = H("tampered config")
        event["payload_hash"] = Hc(event["payload"])
    else:
        s["anchor_evidence"]["anchors"][0]["merkle_root"] = H("tampered root")
    failed, reason, out, _ = audit(s)
    assert failed and reason == "BATCH_NOT_ANCHORED"
    assert out["anchors_mismatch"] > 0
    if tamper == "body":
        assert out["events_hash_mismatch"] > 0


def test_lagging_gateway_index_catches_up_without_an_anchor_failure():
    s = clean_snapshot()
    record = s["anchor_evidence"]["anchors"].pop(0)
    key = H(record["anchor_id"])
    first = audit(s)
    assert first[:2] == (False, "BATCH_NOT_ANCHORED")
    assert first[2]["anchors_mismatch"] == 0
    assert first[2]["chain_anchor_ids_missing_from_gateway"] == [key]
    s["anchor_evidence"]["anchors"].insert(0, record)
    assert gateway_anchor_omissions(s["chain"], s["anchor_evidence"]) == []
    assert audit(s)[:2] == (False, None)


def test_omission_confirmation_only_counts_currently_missing_matching_ids():
    s = clean_snapshot()
    record = s["anchor_evidence"]["anchors"].pop(0)
    key = H(record["anchor_id"])
    s["confirmed_gateway_anchor_omissions"] = [H("different anchor")]
    assert audit(s)[:2] == (False, "BATCH_NOT_ANCHORED")
    s["confirmed_gateway_anchor_omissions"] = [key, H("different anchor")]
    failed, _, out, _ = audit(s)
    assert failed and out["anchors_mismatch"] == 1
    # A stale timer confirmation cannot override a now-complete Gateway index.
    s["anchor_evidence"]["anchors"].insert(0, record)
    assert audit(s)[:2] == (False, None)


@pytest.mark.parametrize("unavailable", ["node", "index", "index_incomplete", "records", "duplicate_record"])
def test_omission_candidates_require_available_node_and_complete_valid_index(unavailable):
    s = clean_snapshot()
    if unavailable == "node":
        s["chain"]["available"] = False
    elif unavailable == "index":
        s["anchor_evidence"] = None
    elif unavailable == "index_incomplete":
        s["anchor_evidence"]["complete"] = False
    elif unavailable == "records":
        s["anchor_evidence"]["anchors"] = None
    else:
        s["anchor_evidence"]["anchors"].append(deepcopy(s["anchor_evidence"]["anchors"][0]))
    assert gateway_anchor_omissions(s["chain"], s["anchor_evidence"]) is None


def test_a_proven_failure_remains_visible_when_other_evidence_is_missing():
    s = clean_snapshot()
    s["chain"]["checkpoint_state"]["status"] = "OPEN"
    s["anchor_evidence"]["complete"] = False
    failed, reason, out, _ = audit(s)
    assert failed and out["chain_state_mismatch"] == 1 and reason == "BATCH_NOT_ANCHORED"


def _hidden_voter_anchor(s):
    """An extra accepted voter event omitted from the cumulative Gateway list."""
    event = {"schema_version": "1.0", "event_id": "evr_" + "5" * 26, "election_id": s["election_id"],
             "source": "core", "checkpoint": 0, "type": "TOKEN_USED", "occurred_at": "2026-11-17T00:00:00Z",
             "payload": {"token_hash": H("hidden token")}}
    event["payload_hash"] = Hc(event["payload"])
    aid, tx = "anc_" + "5" * 26, "0x" + "5" * 64
    digest = leaf_hash(event)
    event["anchor"] = {"status": "ANCHORED", "anchor_id": aid, "mode": "batched", "merkle_root": digest,
                       "block_number": 2, "tx_hash": tx}
    record = {"anchor_id": aid, "election_id": s["election_id"], "mode": "batched", "merkle_root": digest,
              "rejections_root": H(""), "event_count": 1, "block_number": 2, "tx_hash": tx,
              "anchored_at": event["occurred_at"], "bucket_range": {"min": event["occurred_at"], "max": event["occurred_at"]}}
    s["anchor_evidence"]["anchors"].append(record)
    s["anchor_evidence"]["events"][event["event_id"]] = event
    s["anchor_evidence"]["leaves"][aid] = {"anchor_id": aid, "leaves": [{"event_id": event["event_id"], "leaf_hash": digest}],
                                          "rejection_ids": [], "rejections_root": H("")}
    s["chain"]["anchors"][H(aid)] = {"merkle_root": digest, "rejections_root": H(""), "mode": "batched", "event_count": 1,
                                     "bucket_min": 1794873600, "bucket_max": 1794873600}
    s["chain"]["anchor_logs"][H(aid)] = {key: record[key] for key in ("mode", "merkle_root", "rejections_root", "event_count", "block_number", "tx_hash")}
    return event


def test_cumulative_list_omission_cannot_hide_behind_an_unreferenced_historical_anchor():
    s = clean_snapshot()
    removed = _hidden_voter_anchor(s)
    # Voter events do not change the saved contract state; only opening every
    # historical anchor exposes this extra accepted event.
    failed, reason, out, _ = audit(s)
    assert reason == "OTHER"
    assert out["chain_state_mismatch"] == 0
    assert out["cumulative_anchor_events_missing"] == [removed["event_id"]]


def test_unopened_historical_anchor_prevents_pass_even_if_no_event_points_to_it():
    s = clean_snapshot()
    removed = _hidden_voter_anchor(s)
    s["anchor_evidence"]["leaves"].pop(removed["anchor"]["anchor_id"])
    assert audit(s)[:2] == (False, "BATCH_NOT_ANCHORED")


def test_late_batch_hiding_an_old_vote_is_opened_after_the_checkpoint_anchor():
    s = clean_snapshot()
    hidden = _hidden_voter_anchor(s)
    aid = hidden["anchor"]["anchor_id"]
    hidden["anchor"]["block_number"] = 4  # selected checkpoint anchor was block 3
    s["anchor_evidence"]["anchors"][-1]["block_number"] = 4
    s["chain"]["anchor_logs"][H(aid)]["block_number"] = 4
    failed, reason, out, _ = audit(s)
    assert not failed and reason == "OTHER"
    assert out["cumulative_anchor_events_missing"] == [hidden["event_id"]]
