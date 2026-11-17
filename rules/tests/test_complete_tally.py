"""Synthetic complete-evidence TALLY integration of all five Rule Engine checks.

The chain records and proof responses below are deterministic test inputs, not a
Docker, deployed PureChain, finalization transaction or official T01 run. Real
proof arithmetic and independent local event/state replay still execute.
"""
import base64
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from purevoting_rules.engine import evaluate_all, evaluate_rule
from purevoting_rules.findings import build_finding, validate_submission
from purevoting_rules.fixtures import _Factory, _base, _commit_payload, _rehash
from purevoting_rules.hashing import H, ballot_set_root, config_hash, leaf_hash, receipt_hash, result_hash, snapshot_hash
from purevoting_rules.live_inputs import _validators
from purevoting_rules.paths import reference_root


VECTORS = json.loads((Path(__file__).resolve().parents[2] / "tests/vectors/elgamal-vectors.json").read_text(encoding="utf-8"))
ZERO = "0" * 64


def _unix(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _refresh_commit(s, event):
    j = event["checkpoint"]
    views = s["views"] if j == s["checkpoint"] else s["history"][str(j)]["views"]
    event["payload"] = _commit_payload(views, j, event["payload"]["reason"])
    _rehash(event)
    s["history"][str(j)]["checkpoint_event"] = deepcopy(event)


def _synthetic_chain(s):
    """Build internally consistent synthetic node + Gateway proof responses."""
    config = s["views"]["election_config"][0]
    core = sorted((e for e in s["events"] if e["source"] == "core" and "seq" in e), key=lambda e: e["seq"])
    created = next(e for e in core if e["type"] == "ELECTION_CREATED")["payload"]
    commit = next(e for e in core if e["event_id"] == s["checkpoint_event_id"])
    cp_state = {"status": "DRAFT", "window_end": _unix(created["window_end"]), "roster_hash": ZERO,
                "token_set_root": ZERO, "result_hash": ZERO, "last_core_seq": commit["seq"]}
    statuses = {"ROSTER_FROZEN": "ROSTER_FROZEN", "ELECTION_OPENED": "OPEN", "ELECTION_CLOSED": "CLOSED",
                "TALLY_PUBLISHED": "TALLIED", "ELECTION_FINALIZED": "FINALIZED", "ELECTION_VOIDED": "VOIDED"}
    for e in core:
        if e["seq"] > commit["seq"]:
            continue
        p, kind = e["payload"], e["type"]
        if kind in statuses:
            cp_state["status"] = statuses[kind]
        if kind == "WINDOW_CHANGED":
            cp_state["window_end"] = _unix(p["new_window_end"])
        elif kind == "ELECTION_CLOSED":
            cp_state["window_end"] = min(cp_state["window_end"], _unix(p["closed_at"]))
        elif kind == "ROSTER_FROZEN":
            cp_state["roster_hash"] = p["roster_hash"]
        elif kind == "ROSTER_AMENDED":
            cp_state["roster_hash"] = p["roster_hash_after"]
        elif kind in {"TOKEN_BATCH_ISSUED", "TOKEN_REVOKED"}:
            cp_state["token_set_root"] = p["token_set_root"]
        elif kind == "TALLY_PUBLISHED":
            cp_state["result_hash"] = p["result_hash"]
    chain = {"available": True, "reason": None, "confirmed_block": 1000, "chain_id": 1337,
             "contract_address": "0x" + "ab" * 20,
             "registry": {o["org_id"]: {"signer": o["signer"], "registered_by": "0x" + "cd" * 20} for o in config["organizations"]},
             "operator": {"org_id": H(config["operator_org"]), "confirmed_by_all": True},
             "governance": {"org_ids": [H(o["org_id"]) for o in config["organizations"]],
                            "approval_threshold": config["approval_threshold"], "operator_org_id": H(config["operator_org"]),
                            "proposal_ttl_hours": config["proposal_ttl_hours"],
                            "checkpoint_interval_seconds": config["checkpoint_interval_seconds"]},
             "checkpoint_state": cp_state,
             "state": {**cp_state, "window_start": _unix(created["window_start"]),
                       "time_bucket_seconds": created["time_bucket_seconds"], "config_hash": created["config_hash"],
                       "last_checkpoint": s["checkpoint"], "operator_org_id": H(config["operator_org"])},
             "proposals": {}, "anchors": {}, "anchor_logs": {}}
    evidence = {"complete": True, "anchors": [], "leaves": {}, "events": {}, "rejections": {}, "proofs": {}}
    for event in s["events"]:
        _rehash(event)
        a = event["anchor"]
        root = leaf_hash(event)
        node = {"merkle_root": root, "rejections_root": H(""), "mode": a["mode"], "event_count": 1,
                "bucket_min": _unix(event["occurred_at"]), "bucket_max": _unix(event["occurred_at"])}
        chain["anchors"][H(a["anchor_id"])] = node
        chain["anchor_logs"][H(a["anchor_id"])] = {k: node[k] for k in ("merkle_root", "rejections_root", "mode", "event_count")}
        chain["anchor_logs"][H(a["anchor_id"])].update(block_number=a["block_number"], tx_hash=a["tx_hash"])
        gateway = {"anchor_id": a["anchor_id"], "election_id": s["election_id"], "mode": a["mode"],
                   "merkle_root": root, "rejections_root": H(""), "event_count": 1,
                   "tx_hash": a["tx_hash"], "block_number": a["block_number"], "anchored_at": event["occurred_at"]}
        if a["mode"] == "batched":
            gateway["bucket_range"] = {"min": event["occurred_at"], "max": event["occurred_at"]}
        evidence["anchors"].append(gateway)
        evidence["leaves"][a["anchor_id"]] = {"anchor_id": a["anchor_id"], "leaves": [{"event_id": event["event_id"], "leaf_hash": root}],
                                             "rejection_ids": [], "rejections_root": H("")}
        evidence["events"][event["event_id"]] = deepcopy(event)
        evidence["proofs"][event["event_id"]] = {"event_id": event["event_id"], "leaf_hash": root, "merkle_root": root,
                                                "path": [], "anchor_id": a["anchor_id"], "tx_hash": a["tx_hash"], "block_number": a["block_number"]}
    s["chain"], s["anchor_evidence"] = chain, evidence


def complete_tally():
    """Twenty rostered voters, four valid ElGamal votes, scheduled close, tally."""
    f = _Factory(1717)
    f.election_id = VECTORS["election_id"]
    s = _base(f)
    config = s["views"]["election_config"][0]
    candidates = sorted([{"candidate_id": f.identity("cand"), "name": f"Candidate {i}"} for i in range(3)], key=lambda x: x["candidate_id"])
    config.update(candidates=candidates, ballot_scheme="ELGAMAL_ED25519_V1", public_key=base64.b64encode(bytes.fromhex(VECTORS["joint_key"])).decode(),
                  trustee_keys=deepcopy([t["record"] for t in VECTORS["trustees"]]), decryption_shares=None)
    config["config_hash"] = config_hash({**config, "public_key_hash": H(bytes.fromhex(VECTORS["joint_key"]))})
    created = next(e for e in s["events"] if e["type"] == "ELECTION_CREATED")
    created["payload"].update(candidate_count=3, public_key_hash=H(bytes.fromhex(VECTORS["joint_key"])), config_hash=config["config_hash"])
    # Replace earlier placeholder ciphertexts with the four fixed shared vectors.
    s["events"] = [e for e in s["events"] if e["type"] not in {"TOKEN_USED", "BALLOT_RECEIVED"}]
    for t in s["views"]["token_status"]:
        t["status"], t["used_checkpoint"] = "ISSUED", None
    s["views"]["credential_issuance"], s["views"]["ballot_receipts"] = [], []
    for i, ballot in enumerate(VECTORS["ballots"]):
        token = s["views"]["token_status"][i]
        token["status"], token["used_checkpoint"] = "USED", 2
        credential = {"view": "credential_issuance", "row_key": ballot["nullifier_hash"], "issued_checkpoint": 2, "used_checkpoint": 2}
        s["views"]["credential_issuance"].append(credential)
        s["events"].insert(-1, f.event("TOKEN_USED", 2, "2026-11-03T09:00:00Z", {"token_hash": token["row_key"]}))
        raw = bytes.fromhex(ballot["sealed_ballot"])
        p = {"nullifier_hash": credential["row_key"], "ballot_hash": H(raw), "receipt_hash": receipt_hash(s["election_id"], credential["row_key"], H(raw))}
        event = f.event("BALLOT_RECEIVED", 2, "2026-11-03T09:00:00Z", p)
        s["events"].insert(-1, event)
        s["views"]["ballot_receipts"].append({"view": "ballot_receipts", "row_key": credential["row_key"], "ballot_hash": H(raw),
                                             "sealed_ballot": base64.b64encode(raw).decode(), "receipt_hash": p["receipt_hash"],
                                             "received_checkpoint": 2, "received_bucket": event["occurred_at"], "event_id": event["event_id"]})
    s["history"]["2"]["views"] = deepcopy(s["views"])
    for past in s["history"].values():
        status = past["views"]["election_config"][0]["status"]
        past["views"]["election_config"] = [deepcopy(config)]
        past["views"]["election_config"][0]["status"] = status
    for event in s["events"]:
        if event["type"] == "CHECKPOINT_COMMITTED":
            _refresh_commit(s, event)
    config["status"] = "CLOSED"
    s["events"].append(f.event("ELECTION_CLOSED", 3, "2026-11-03T09:30:00Z", {"reason": "SCHEDULED", "closed_at": "2026-11-03T09:30:00Z"}, 8))
    close = f.event("CHECKPOINT_COMMITTED", 3, "2026-11-03T09:45:00Z", _commit_payload(s["views"], 3, "CLOSE_FINAL"), 9)
    s["events"].append(close)
    s["history"]["3"] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(close)}
    config["status"] = "TALLIED"
    config["decryption_shares"] = deepcopy([share["share"] for share in VECTORS["shares"]])
    payload = {"final_checkpoint": 3, "ballot_count": 4, "invalid_ballots": 0, "excluded_ballots": 0, "invalid_ballot_hashes": [],
               "results": [{"candidate_id": c["candidate_id"], "votes": v} for c, v in zip(candidates, VECTORS["votes"])],
               "ballot_set_root": ballot_set_root(r["ballot_hash"] for r in s["views"]["ballot_receipts"])}
    payload["result_hash"] = result_hash({**payload, "election_id": s["election_id"]})
    s["events"].append(f.event("TALLY_PUBLISHED", 4, "2026-11-03T09:46:00Z", payload, 10))
    tally_commit = f.event("CHECKPOINT_COMMITTED", 4, "2026-11-03T09:46:00Z", _commit_payload(s["views"], 4, "TALLY"), 11)
    s["events"].append(tally_commit)
    s.update(checkpoint=4, checkpoint_event_id=tally_commit["event_id"], ledger_last_seq=11)
    s["history"]["4"] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(tally_commit)}
    _synthetic_chain(s)
    return s


def test_complete_synthetic_elgamal_tally_all_five_rules_pass():
    snapshot = complete_tally()
    before = deepcopy(snapshot)
    outcomes = evaluate_all(snapshot)
    assert {o.rule_id: o.result for o in outcomes} == dict.fromkeys(("R1", "R2", "R3", "R4", "R5"), "PASS"), [(o.rule_id, o.summary, o.observed) for o in outcomes]
    assert all(o.coverage == "complete" and not o.unimplemented_checks for o in outcomes)
    for outcome in outcomes:
        body = build_finding(outcome, snapshot)
        validate_submission(body)
        assert body["result"] == "PASS" and "inconclusive_reason" not in body
    assert snapshot == before


def test_complete_tally_fixture_uses_current_contract_wire_shapes():
    snapshot = complete_tally()
    validators = _validators(reference_root())
    for event in snapshot["events"]:
        validators["event"].validate(event)
    for checkpoint, past in snapshot["history"].items():
        for name, rows in past["views"].items():
            validators["view"].validate({"view": name, "election_id": snapshot["election_id"], "checkpoint": int(checkpoint),
                                         "total_rows": len(rows), "snapshot_hash": None if name == "election_config" else snapshot_hash(rows),
                                         "rows": rows, "next_cursor": None})
    validators["anchors"].validate({"items": snapshot["anchor_evidence"]["anchors"], "next_cursor": None})
    for leaves in snapshot["anchor_evidence"]["leaves"].values():
        validators["leaves"].validate(leaves)
    for proof in snapshot["anchor_evidence"]["proofs"].values():
        validators["proof"].validate(proof)


@pytest.mark.parametrize("kind,counter", [("share", "tally_share_failures"), ("results", "tally_result_mismatches"),
                                         ("invalid_set", "tally_invalid_set_mismatches"), ("result_hash", "tally_result_hash_mismatches"),
                                         ("final_checkpoint", "tally_final_checkpoint_mismatches"), ("roster", "root_mismatches"),
                                         ("key", "key_mismatch"), ("ballot_root", "tally_root_mismatches")])
def test_complete_tally_tampering_fails_even_with_matching_snapshot_and_event_hashes(kind, counter):
    snapshot = complete_tally()
    config = snapshot["views"]["election_config"][0]
    tally = next(e for e in snapshot["events"] if e["type"] == "TALLY_PUBLISHED")
    if kind == "share":
        config["decryption_shares"][0]["shares"][0]["z"] = "00" * 32
    elif kind == "results":
        tally["payload"]["results"][0]["votes"] += 1
        tally["payload"]["result_hash"] = result_hash({**tally["payload"], "election_id": snapshot["election_id"]})
    elif kind == "invalid_set":
        tally["payload"]["invalid_ballot_hashes"] = ["aa" * 32]
        tally["payload"]["result_hash"] = result_hash({**tally["payload"], "election_id": snapshot["election_id"]})
    elif kind == "result_hash":
        tally["payload"]["result_hash"] = "aa" * 32
    elif kind == "final_checkpoint":
        tally["payload"]["final_checkpoint"] = 2
        tally["payload"]["result_hash"] = result_hash({**tally["payload"], "election_id": snapshot["election_id"]})
    elif kind == "key":
        config["trustee_keys"][0]["proof"]["z"] = "00" * 32
    elif kind == "ballot_root":
        tally["payload"]["ballot_set_root"] = "aa" * 32
        tally["payload"]["result_hash"] = result_hash({**tally["payload"], "election_id": snapshot["election_id"]})
    else:
        snapshot["views"]["roster"][0]["row_key"] = "vtr_" + "0" * 26
    current = next(e for e in snapshot["events"] if e["event_id"] == snapshot["checkpoint_event_id"])
    _refresh_commit(snapshot, current)
    _synthetic_chain(snapshot)
    outcome = evaluate_rule("R5", snapshot)
    assert outcome.result == "FAIL", (outcome.summary, outcome.observed)
    assert outcome.observed[counter] > 0
    body = build_finding(outcome, snapshot)
    validate_submission(body)
    assert body["result"] == "FAIL" and body["evidence_refs"]


@pytest.mark.parametrize("kind,counter", [("checkpoint_state", "chain_state_mismatch"), ("governance", "governance_mismatch"),
                                         ("anchor_root", "anchors_mismatch"), ("proof_leaf", "events_hash_mismatch")])
def test_complete_tally_checks_independent_node_state_roots_and_proofs(kind, counter):
    snapshot = complete_tally()
    if kind == "checkpoint_state":
        snapshot["chain"]["checkpoint_state"]["result_hash"] = "ab" * 32
    elif kind == "governance":
        snapshot["chain"]["governance"]["approval_threshold"] = 1
    elif kind == "anchor_root":
        next(iter(snapshot["chain"]["anchors"].values()))["merkle_root"] = "ab" * 32
    else:
        next(iter(snapshot["anchor_evidence"]["proofs"].values()))["leaf_hash"] = "ab" * 32
    outcome = evaluate_rule("R5", snapshot)
    assert outcome.result == "FAIL", (outcome.summary, outcome.observed)
    assert outcome.observed[counter] > 0


@pytest.mark.parametrize("missing", ["chain", "anchor_evidence", "history", "decryption_shares"])
def test_complete_tally_never_passes_with_required_evidence_missing(missing):
    snapshot = complete_tally()
    if missing == "decryption_shares":
        snapshot["views"]["election_config"][0]["decryption_shares"] = None
    elif missing == "history":
        del snapshot["history"]["0"]
    else:
        del snapshot[missing]
    result = evaluate_rule("R5", snapshot)
    assert result.result == "INCONCLUSIVE", (result.summary, result.observed)
