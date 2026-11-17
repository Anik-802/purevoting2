"""Synthetic development cases, not Harits's independent scenario labels.

Rows/events/rejections retain their frozen v1.0 shapes. The outer snapshot envelope
is the local adapter's normalized input: cumulative accepted events/rejections plus
as-of views and historical view copies. Ciphertext bytes and anchor metadata are
synthetic; no encryption, deployed node, or full chain-verification success is claimed.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import random

from .engine import TABLES
from .hashing import H, Hc, ballot_set_root, config_hash, leaf_hash, receipt_hash, result_hash, roster_hash, snapshot_hash, token_set_root

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_GOOD = {"R1": "PASS", "R2": "PASS", "R3": "PASS", "R4": "INCONCLUSIVE", "R5": "INCONCLUSIVE"}


class _Factory:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.election_id = self.identity("elc")
        self.block = 100

    def identity(self, prefix: str) -> str:
        return prefix + "_" + "".join(self.rng.choice(_ALPHABET) for _ in range(26))

    def event(self, kind: str, k: int, at: str, payload: dict, seq: int | None = None, source: str = "core") -> dict:
        voter = kind in {"TOKEN_USED", "BALLOT_RECEIVED"}
        event = {"event_id": self.identity("evr" if voter else "evt"), "schema_version": "1", "election_id": self.election_id,
                 "type": kind, "source": source, "checkpoint": k, "occurred_at": at,
                 "time_precision": "bucketed" if voter else "exact", "actor": {"kind": "voter_anonymous" if voter else "system"},
                 "payload": deepcopy(payload), "payload_hash": Hc(payload)}
        if voter:
            event["time_bucket_seconds"] = 900
        else:
            event["seq"] = seq or 1
        root = leaf_hash(event)
        self.block += 1
        event["anchor"] = {"status": "ANCHORED", "anchor_id": self.identity("anc"), "mode": "batched" if voter else "immediate",
                           "merkle_root": root, "tx_hash": "0x" + H(f"fixture-block-{self.block}"), "block_number": self.block}
        if not voter:
            event["received_at"] = at
        return event

    def rejection(self, kind: str, k: int, code: str, subject: str | None, payload: dict, at: str = "2026-11-03T09:00:00Z") -> dict:
        return {"rejection_id": self.identity("rej"), "election_id": self.election_id,
                "event_id": self.identity("evr" if kind in {"TOKEN_USED", "BALLOT_RECEIVED"} else "evt"), "type": kind,
                "checkpoint": k, "reject_code": code, "recorded_at": at, "payload_hash": Hc(payload), "subject_hash": subject}


def _rehash(event: dict) -> None:
    event["payload_hash"] = Hc(event["payload"])
    submission = {key: value for key, value in event.items() if key not in {"anchor", "received_at"}}
    if event.get("anchor", {}).get("status") == "ANCHORED":
        event["anchor"]["merkle_root"] = leaf_hash(submission)


def _counts(views: dict) -> dict:
    return {"roster_active": sum(r["status"] == "ACTIVE" for r in views["roster"]),
            "tokens_active": sum(r["status"] in {"ISSUED", "USED"} for r in views["token_status"]),
            "token_used": sum(r["used_checkpoint"] is not None for r in views["token_status"]),
            "credentials_issued": len(views["credential_issuance"]), "ballots_received": len(views["ballot_receipts"]),
            "rejected_attempts": len(views["rejected_attempts"])}


def _commit_payload(views: dict, k: int, reason: str) -> dict:
    return {"checkpoint": k, "reason": reason,
            "tables": [{"table": name, "row_count": len(views[name]), "snapshot_hash": snapshot_hash(views[name])} for name in TABLES],
            "counts": _counts(views),
            "voter_events": {"TOKEN_USED": sum(r["used_checkpoint"] == k for r in views["token_status"]),
                             "BALLOT_RECEIVED": sum(r["received_checkpoint"] == k for r in views["ballot_receipts"])}}


def _sync_current_commit(snapshot: dict) -> None:
    event = next(e for e in snapshot["events"] if e["event_id"] == snapshot["checkpoint_event_id"])
    event["payload"] = _commit_payload(snapshot["views"], snapshot["checkpoint"], event["payload"]["reason"])
    _rehash(event)
    snapshot["history"][str(snapshot["checkpoint"])] = {"views": deepcopy(snapshot["views"]), "checkpoint_event": deepcopy(event)}


def _base(factory: _Factory) -> dict:
    public_bytes = bytes.fromhex(H("PureVoting-local-fixture-public-key-NOT-A-DEPLOYED-KEY"))
    config = {"view": "election_config", "row_key": "config", "status": "OPEN", "title": "Synthetic rules development election",
              "candidates": [{"candidate_id": factory.identity("cand"), "name": "Candidate A"}, {"candidate_id": factory.identity("cand"), "name": "Candidate B"}],
              "config_window_start": "2026-11-03T09:00:00Z", "config_window_end": "2026-11-03T09:30:00Z",
              "window_start": "2026-11-03T09:00:00Z", "window_end": "2026-11-03T09:30:00Z", "time_bucket_seconds": 900,
              "checkpoint_interval_seconds": 900, "public_key": base64.b64encode(public_bytes).decode(), "private_key": None,
              "organizations": [{"org_id": "org_commission", "signer": "0x" + "11" * 20},
                                {"org_id": "org_neutral", "signer": "0x" + "22" * 20},
                                {"org_id": "org_observer", "signer": "0x" + "33" * 20}],
              "approval_threshold": 2, "proposal_ttl_hours": 24, "operator_org": "org_commission"}
    config["config_hash"] = config_hash({**config, "public_key_hash": H(public_bytes)})
    roster = [{"view": "roster", "row_key": factory.identity("vtr"), "status": "ACTIVE", "added_checkpoint": 0, "removed_checkpoint": None} for _ in range(20)]
    tokens = [{"view": "token_status", "row_key": H(f"synthetic-token-{i}"), "status": "ISSUED", "issued_checkpoint": 1,
               "used_checkpoint": None, "revoked_checkpoint": None} for i in range(20)]
    views = {"election_config": [config], "roster": roster, "token_status": [], "credential_issuance": [], "ballot_receipts": [], "admin_actions": [], "rejected_attempts": []}
    created = {"config_hash": config["config_hash"], "window_start": config["window_start"], "window_end": config["window_end"],
               "time_bucket_seconds": 900, "checkpoint_interval_seconds": 900, "candidate_count": 2,
               "organizations": config["organizations"], "approval_threshold": 2, "proposal_ttl_hours": 24,
               "operator_org": "org_commission", "public_key_hash": H(public_bytes)}
    events = [factory.event("ELECTION_CREATED", 0, "2026-11-03T08:00:00Z", created, 1),
              factory.event("ROSTER_FROZEN", 0, "2026-11-03T08:30:00Z", {"roster_hash": roster_hash(r["row_key"] for r in roster), "voter_count": 20}, 2)]
    frozen_views = deepcopy(views)
    frozen_views["election_config"][0]["status"] = "ROSTER_FROZEN"
    commit0 = factory.event("CHECKPOINT_COMMITTED", 0, "2026-11-03T08:30:00Z", _commit_payload(frozen_views, 0, "STATE_CHANGE"), 3)
    events.append(commit0)
    history = {"0": {"views": frozen_views, "checkpoint_event": deepcopy(commit0)}}
    views["token_status"] = tokens
    events.append(factory.event("TOKEN_BATCH_ISSUED", 1, "2026-11-03T08:31:00Z", {"batch_no": 1, "token_count": 20, "token_set_root": token_set_root(r["row_key"] for r in tokens)}, 4))
    events.append(factory.event("ELECTION_OPENED", 1, "2026-11-03T09:00:00Z", {"window_start": config["window_start"], "window_end": config["window_end"]}, 5))
    commit1 = factory.event("CHECKPOINT_COMMITTED", 1, "2026-11-03T09:00:00Z", _commit_payload(views, 1, "STATE_CHANGE"), 6)
    events.append(commit1)
    history["1"] = {"views": deepcopy(views), "checkpoint_event": deepcopy(commit1)}
    for i in range(3):
        tokens[i]["status"] = "USED"
        tokens[i]["used_checkpoint"] = 2
        credential = {"view": "credential_issuance", "row_key": H(f"synthetic-nullifier-{i}"), "issued_checkpoint": 2, "used_checkpoint": 2 if i < 2 else None}
        views["credential_issuance"].append(credential)
        events.append(factory.event("TOKEN_USED", 2, "2026-11-03T09:00:00Z", {"token_hash": tokens[i]["row_key"]}))
        if i < 2:
            # These bytes are fixture bytes, not encrypted voting material.
            sealed = f"synthetic-sealed-ballot-{i}".encode()
            ballot = H(sealed)
            payload = {"nullifier_hash": credential["row_key"], "ballot_hash": ballot,
                       "receipt_hash": receipt_hash(factory.election_id, credential["row_key"], ballot)}
            event = factory.event("BALLOT_RECEIVED", 2, "2026-11-03T09:00:00Z", payload)
            events.append(event)
            views["ballot_receipts"].append({"view": "ballot_receipts", "row_key": credential["row_key"], "ballot_hash": ballot,
                                             "sealed_ballot": base64.b64encode(sealed).decode(), "receipt_hash": payload["receipt_hash"],
                                             "received_checkpoint": 2, "received_bucket": event["occurred_at"], "event_id": event["event_id"]})
    commit2 = factory.event("CHECKPOINT_COMMITTED", 2, "2026-11-03T09:15:00Z", _commit_payload(views, 2, "PERIODIC"), 7)
    events.append(commit2)
    history["2"] = {"views": deepcopy(views), "checkpoint_event": deepcopy(commit2)}
    return {"election_id": factory.election_id, "checkpoint": 2, "checkpoint_event_id": commit2["event_id"],
            "ledger_last_seq": 7, "views": views, "events": events, "rejections": [], "history": history, "node": {"available": True}}


def development_cases(seed: int = 2026) -> list[dict]:
    """Return reproducible, hand-labelled local assertions; no formal Txx claims."""
    factory = _Factory(seed)
    original = _base(factory)
    cases = []

    def add(case_id: str, description: str, snapshot: dict, **changes: str) -> None:
        cases.append({"case_id": case_id, "description": description, "snapshot": snapshot, "expected": {**_GOOD, **changes}})

    add("clean_cumulative", "Twenty synthetic voters; three credentials redeemed, two cast; independent node evidence is deliberately absent.", deepcopy(original))
    s = deepcopy(original)
    for kind in ("TOKEN_ALREADY_USED", "CREDENTIAL_ALREADY_USED", "ELECTION_NOT_OPEN"):
        s["views"]["rejected_attempts"].append({"view": "rejected_attempts", "row_key": factory.identity("att"), "kind": kind, "subject_hash": None, "checkpoint": 2})
    _sync_current_commit(s)
    add("core_refused_retry", "Core refuses two duplicate retries and an out-of-window request; none becomes an accepted vote.", s)

    for event_type, case_id in (("TOKEN_USED", "accepted_duplicate_token"), ("BALLOT_RECEIVED", "accepted_duplicate_nullifier")):
        s = deepcopy(original)
        event = deepcopy(next(e for e in s["events"] if e["type"] == event_type))
        event["event_id"] = factory.identity("evr")
        _rehash(event)
        s["events"].insert(-1, event)
        add(case_id, "A second distinct event is accepted with an already-used subject; accepted duplicates are violations.", s,
            R1="FAIL", R3="FAIL", **({"R5": "FAIL"} if event_type == "BALLOT_RECEIVED" else {}))

    s = deepcopy(original)
    for e in s["events"]:
        if e["type"] not in {"TOKEN_USED", "BALLOT_RECEIVED"}:
            e["received_at"] = "2026-11-03T12:00:00Z"
    add("delayed_system_delivery", "Late system received_at timestamps do not change official voter acceptance buckets; voter records contain no precise receive time.", s)

    s = deepcopy(original)
    event = next(e for e in s["events"] if e["type"] == "BALLOT_RECEIVED")
    event["occurred_at"] = "2026-11-03T09:30:00Z"
    _rehash(event)
    add("late_accepted_ballot", "An accepted bucket starts at window_end, so its full 900-second bucket lies outside the window.", s, R2="FAIL", R5="FAIL")

    s = deepcopy(original)
    event = next(e for e in s["events"] if e["type"] == "BALLOT_RECEIVED")
    s["events"].remove(event)
    rejection = factory.rejection("BALLOT_RECEIVED", 2, "ELECTION_NOT_OPEN", event["payload"]["nullifier_hash"], event["payload"], "2026-11-03T09:30:00Z")
    rejection["event_id"] = event["event_id"]
    s["rejections"].append(rejection)
    add("gateway_window_rejection", "Core stored a ballot but Gateway rejected it outside the window; ledger acceptance plus rejection still reconciles its count.", s, R2="FAIL", R5="FAIL")

    s = deepcopy(original)
    s["views"]["credential_issuance"].pop()
    _sync_current_commit(s)
    add("atomic_redemption_mismatch", "Three used tokens but only two issued credentials violates atomic redemption, even when the committed count matches the bad view.", s, R3="FAIL")

    s = deepcopy(original)
    event = next(e for e in s["events"] if e["event_id"] == s["checkpoint_event_id"])
    event["payload"]["counts"]["ballots_received"] += 1
    _rehash(event)
    add("committed_count_mismatch", "The committed ballot count disagrees with the complete as-of view.", s, R3="FAIL")

    s = deepcopy(original)
    del s["views"]["token_status"]
    add("missing_token_view", "An unavailable token view is unknown evidence, never an empty clean table.", s, R1="INCONCLUSIVE", R3="INCONCLUSIVE")

    s = deepcopy(original)
    s["views"]["ballot_receipts"][0]["sealed_ballot"] = base64.b64encode(b"altered-after-commit").decode()
    add("tampered_sealed_ballot", "Stored sealed bytes change after commit; both sealed-byte hashing and immutable snapshot checks detect it.", s, R5="FAIL")

    s = deepcopy(original)
    s["node"]["available"] = False
    add("node_unavailable", "Local R1–R3 remain evaluable; R4/R5 cannot claim success without an independent node.", s)

    s = deepcopy(original)
    action_id, proposal_id = factory.identity("act"), factory.identity("prp")
    event = factory.event("WINDOW_CHANGED", 2, "2026-11-03T09:05:00Z", {"action_id": action_id, "proposal_id": proposal_id,
                          "old_window_end": "2026-11-03T09:30:00Z", "new_window_end": "2026-11-03T09:45:00Z"}, 7)
    s["events"].insert(-1, event)
    s["events"][-1]["seq"] = 8
    s["ledger_last_seq"] = 8
    _rehash(s["events"][-1])
    s["views"]["election_config"][0]["window_end"] = "2026-11-03T09:45:00Z"
    add("unapproved_window_change", "A window effect has no executed, approved proposal; the implemented R4 subset demonstrates the violation.", s, R4="FAIL")

    s = deepcopy(original)
    event = next(e for e in s["events"] if e["type"] == "TOKEN_USED")
    event["payload"]["token_hash"] = H("unknown-synthetic-token")
    _rehash(event)
    add("unknown_accepted_token", "An accepted token does not exist in token_status; R4 also detects the unmatched token-state change.", s, R1="FAIL", R4="FAIL", R5="FAIL")

    s = deepcopy(original)
    row = s["views"]["token_status"][0]
    row["revoked_checkpoint"] = 2
    row["status"] = "REVOKED"
    _sync_current_commit(s)
    add("used_and_revoked_token", "A used token is revoked without an authorized event; its live-token root also differs from the ledger.", s, R1="FAIL", R4="FAIL", R5="FAIL")

    s = deepcopy(original)
    del s["history"]["0"]
    add("missing_historical_view", "Current snapshots are clean but checkpoint zero cannot be rechecked; R5 returns VIEW_UNAVAILABLE.", s)

    s = deepcopy(original)
    event = next(e for e in s["events"] if e["type"] == "BALLOT_RECEIVED")
    event["anchor"] = {"status": "PENDING"}
    add("pending_anchor", "After the adapter's eligibility timeout, a pending voter anchor leaves R5 BATCH_NOT_ANCHORED.", s)

    s = deepcopy(original)
    # Closing at the next boundary, then accepting an event at a later checkpoint
    # must fail even when its declared bucket is deceptively inside the old window.
    s["events"].append(factory.event("ELECTION_CLOSED", 3, "2026-11-03T09:30:00Z", {"reason": "SCHEDULED", "closed_at": "2026-11-03T09:30:00Z"}, 8))
    s["views"]["election_config"][0]["status"] = "CLOSED"
    close = factory.event("CHECKPOINT_COMMITTED", 3, "2026-11-03T09:30:00Z", _commit_payload(s["views"], 3, "CLOSE_FINAL"), 9)
    s["events"].append(close)
    s["history"]["3"] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(close)}
    s["events"].append(factory.event("TOKEN_USED", 4, "2026-11-03T09:00:00Z", {"token_hash": H("unrecorded-post-close-token")}))
    commit = factory.event("CHECKPOINT_COMMITTED", 4, "2026-11-03T09:45:00Z", _commit_payload(s["views"], 4, "STATE_CHANGE"), 10)
    s["events"].append(commit)
    s.update(checkpoint=4, checkpoint_event_id=commit["event_id"], ledger_last_seq=10)
    s["history"]["4"] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(commit)}
    add("accepted_after_close_final", "A voter event beyond CLOSE_FINAL is a violation even with an in-window bucket.", s, R1="FAIL", R2="FAIL", R3="FAIL", R5="FAIL")

    # Tally-count fixture uses the clean prefix above, without its bad post-close
    # event. This older synthetic fixture intentionally supplies no private key.
    s = deepcopy(s)
    s["events"] = [e for e in s["events"] if e["checkpoint"] <= 3]
    s["history"].pop("4", None)
    config = s["views"]["election_config"][0]
    config["status"] = "TALLIED"
    payload = {"final_checkpoint": 3, "ballot_count": 3, "invalid_ballots": 0, "excluded_ballots": 0,
               "invalid_ballot_hashes": [], "results": [{"candidate_id": config["candidates"][0]["candidate_id"], "votes": 3},
                                                        {"candidate_id": config["candidates"][1]["candidate_id"], "votes": 0}],
               "ballot_set_root": ballot_set_root(r["ballot_hash"] for r in s["views"]["ballot_receipts"])}
    payload["result_hash"] = result_hash({"election_id": s["election_id"], **payload})
    s["events"].append(factory.event("TALLY_PUBLISHED", 4, "2026-11-03T09:45:00Z", payload, 10))
    commit = factory.event("CHECKPOINT_COMMITTED", 4, "2026-11-03T09:45:00Z", _commit_payload(s["views"], 4, "TALLY"), 11)
    s["events"].append(commit)
    s.update(checkpoint=4, checkpoint_event_id=commit["event_id"], ledger_last_seq=11)
    s["history"]["4"] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(commit)}
    add("tally_count_mismatch", "Tally claims three valid ballots while the cumulative accepted ledger has two; no revealed private key is supplied.", s, R3="FAIL")
    return cases
