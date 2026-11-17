"""R5 computations over checkpoint views and accepted ledger events.

This module does not contact Core, import Core, read the database, or trust a
Gateway verification flag. Independent node commitments are checked separately
by chain_audit; agreement here alone is insufficient for a complete R5 PASS.
"""
from __future__ import annotations

import base64
from collections import Counter
from datetime import datetime
import json

from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox

from .hashing import H, Hc, ballot_set_root, config_hash, result_hash, roster_hash, snapshot_hash, token_set_root
from .tally_elgamal import audit_elgamal

TABLES = ("roster", "token_status", "credential_issuance", "ballot_receipts", "admin_actions", "rejected_attempts")
TALLY_KEYS = ("tally_root_mismatches", "tally_final_checkpoint_mismatches", "tally_result_hash_mismatches",
              "tally_invalid_set_mismatches", "tally_share_failures", "tally_result_mismatches")
LOCAL_KEYS = ("rejected_checkpoints", "checkpoint_cadence_violations", "row_ledger_mismatches", "key_mismatch",
              "tables_mismatched", "sealed_hash_mismatches", "root_mismatches", "config_mismatch", "events_hash_mismatch")


def _event_ref(event):
    return {"kind": "ledger_event", "ref": event["event_id"]}


def _row_ref(row, checkpoint):
    return {"kind": "view_row", "ref": f"{row['view']}:{row['row_key']}@{checkpoint}"}


def _raw(value):
    raw = base64.b64decode(value, validate=True)
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("noncanonical base64")
    return raw


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("missing timezone")
    return parsed


def _valid_plaintext(plaintext, candidates):
    """D-30 legacy ballots: parse UTF-8 JSON, reject duplicate or extra keys."""
    def pairs(items):
        obj = dict(items)
        if len(obj) != len(items):
            raise ValueError("duplicate JSON key")
        return obj

    def constant(_):
        raise ValueError("non-JSON number")

    try:
        obj = json.loads(plaintext.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
        if isinstance(obj, dict) and set(obj) == {"candidate_id"}:
            choice = obj["candidate_id"]
            if isinstance(choice, str) and choice in candidates:
                return choice
    except (UnicodeError, ValueError, RecursionError):
        pass
    return None


def _result_mismatches(expected, reported):
    counts = Counter(r["candidate_id"] for r in reported)
    values = {r["candidate_id"]: r["votes"] for r in reported}
    return sum(values.get(cid, 0) != votes for cid, votes in expected.items()) + sum(
        r["candidate_id"] not in expected or counts[r["candidate_id"]] > 1 for r in reported)


def _sealed_tally(config, eid, created, events, rows, tally, out, refs):
    """Verify the retained SEALED_BOX_V1 scheme without logging the disclosed key."""
    raw_public = None
    try:
        raw_public = _raw(config["public_key"])
        if len(raw_public) != 32 or H(raw_public) != created["payload"]["public_key_hash"]:
            out["key_mismatch"] += 1
    except (ValueError, TypeError):
        out["key_mismatch"] += 1
    if tally is None:
        return None
    if config["private_key"] is None:
        return "KEY_NOT_REVEALED"
    try:
        key = PrivateKey(_raw(config["private_key"]))
        if bytes(key.public_key) != raw_public:
            out["key_mismatch"] += 1
    except (ValueError, TypeError):
        out["key_mismatch"] += 1
    if out["key_mismatch"]:
        return None
    box = SealedBox(key)
    candidates = {c["candidate_id"] for c in config["candidates"]}
    votes = dict.fromkeys(candidates, 0)
    by_id = {}
    for row in rows:
        by_id.setdefault(row["event_id"], []).append(row)
    invalid = []
    for event in events:
        if event["type"] != "BALLOT_RECEIVED" or event["checkpoint"] > tally["final_checkpoint"]:
            continue
        p = event["payload"]
        matches = by_id.get(event["event_id"], [])
        choice = None
        if len(matches) == 1:
            row = matches[0]
            if (row["row_key"], row["ballot_hash"], row["receipt_hash"]) == (
                    p["nullifier_hash"], p["ballot_hash"], p["receipt_hash"]):
                try:
                    sealed = _raw(row["sealed_ballot"])
                    if H(sealed) == p["ballot_hash"]:
                        choice = _valid_plaintext(box.decrypt(sealed), candidates)
                except (ValueError, TypeError, CryptoError):
                    pass
        if choice is None:
            invalid.append(p["ballot_hash"])
        else:
            votes[choice] += 1
    expected, published = Counter(invalid), Counter(tally["invalid_ballot_hashes"])
    out["tally_invalid_set_mismatches"] = sum((expected - published).values()) + sum((published - expected).values())
    out["tally_result_mismatches"] = _result_mismatches(votes, tally["results"])
    return None


def audit_local(s, events, rejections, out, refs):
    """Return (failed, inconclusive_reason, detail) for the non-RPC R5 checks.

    The engine validates the required current views before calling this function.
    Historical view loss leaves a positive evidence gap, never an empty snapshot.
    """
    out.update(dict.fromkeys(LOCAL_KEYS, 0))
    out.update(dict.fromkeys(TALLY_KEYS))
    out["events_unanchored"] = sum(e.get("anchor", {}).get("status") != "ANCHORED" for e in events)
    out["rejected_checkpoints"] = sum(r["type"] == "CHECKPOINT_COMMITTED" for r in rejections)
    refs.extend({"kind": "ledger_rejection", "ref": r["rejection_id"]}
                for r in rejections if r["type"] == "CHECKPOINT_COMMITTED")
    views, k = s["views"], s["checkpoint"]
    config = views["election_config"][0]
    checkpoints = sorted((e for e in events if e["type"] == "CHECKPOINT_COMMITTED"), key=lambda e: e["seq"])
    created_events = [e for e in events if e["type"] == "ELECTION_CREATED"]
    current = next(e for e in checkpoints if e["event_id"] == s["checkpoint_event_id"])
    missing_history = False
    for commit in checkpoints:
        j = commit["checkpoint"]
        past = views if j == k else s.get("history", {}).get(str(j), {}).get("views")
        if not isinstance(past, dict):
            missing_history = True
            continue
        committed = {v["table"]: v for v in commit["payload"]["tables"]}
        for table in TABLES:
            rows = past.get(table)
            if not isinstance(rows, list):
                missing_history = True
                continue
            entry = committed.get(table)
            if entry is None or entry["row_count"] != len(rows) or entry["snapshot_hash"] != snapshot_hash(rows):
                out["tables_mismatched"] += 1
                refs.extend([_event_ref(commit), {"kind": "snapshot", "ref": f"{table}@{j}"}])
        # Roots describe sets after the latest accepted operation, not counts.
        # Validate every checkpoint: a forged earlier row cannot be hidden by
        # restoring the current view or publishing a matching new snapshot hash.
        prefix = sorted((e for e in events if e.get("source") == "core" and "seq" in e
                         and e["seq"] <= commit["seq"]), key=lambda e: e["seq"])
        roots = (("roster", {"ROSTER_FROZEN", "ROSTER_AMENDED"}),
                 ("token_status", {"TOKEN_BATCH_ISSUED", "TOKEN_REVOKED"}))
        for name, kinds in roots:
            rows = past.get(name)
            if not isinstance(rows, list):
                continue
            latest = next((e for e in reversed(prefix) if e["type"] in kinds), None)
            if latest is None:
                # Roster data can exist in DRAFT before any frozen-root event.
                # Tokens cannot exist before the first issuance.
                if name == "token_status" and rows:
                    out["root_mismatches"] += 1
                    refs.append({"kind": "snapshot", "ref": f"{name}@{j}"})
                continue
            if name == "roster":
                actual = roster_hash(r["row_key"] for r in rows if r["status"] == "ACTIVE")
                expected = latest["payload"]["roster_hash_after" if latest["type"] == "ROSTER_AMENDED" else "roster_hash"]
            else:
                actual = token_set_root(r["row_key"] for r in rows if r["status"] != "REVOKED")
                expected = latest["payload"]["token_set_root"]
            if actual != expected:
                out["root_mismatches"] += 1
                refs.extend([_event_ref(latest), {"kind": "snapshot", "ref": f"{name}@{j}"}])
    ballots = {e["event_id"]: e for e in events if e["type"] == "BALLOT_RECEIVED"}
    seen = Counter()
    for row in views["ballot_receipts"]:
        try:
            sealed_ok = H(_raw(row["sealed_ballot"])) == row["ballot_hash"]
        except (ValueError, TypeError):
            sealed_ok = False
        if not sealed_ok:
            out["sealed_hash_mismatches"] += 1
            refs.append(_row_ref(row, k))
        event = ballots.get(row["event_id"])
        seen[row["event_id"]] += 1
        if event is None or (row["row_key"], row["ballot_hash"], row["receipt_hash"], row["received_bucket"], row["received_checkpoint"]) != (
                event["payload"]["nullifier_hash"], event["payload"]["ballot_hash"], event["payload"]["receipt_hash"],
                event["occurred_at"], event["checkpoint"]):
            out["row_ledger_mismatches"] += 1
            refs.append(_row_ref(row, k))
    for identity, event in ballots.items():
        if seen[identity] != 1:
            out["row_ledger_mismatches"] += 1
            refs.append(_event_ref(event))
    used_rows = {r["row_key"]: r for r in views["token_status"] if r["used_checkpoint"] is not None and r["used_checkpoint"] <= k}
    used_events = {e["payload"]["token_hash"]: e for e in events if e["type"] == "TOKEN_USED"}
    for identity in sorted(used_rows.keys() ^ used_events.keys()):
        out["row_ledger_mismatches"] += 1
        refs.append(_row_ref(used_rows[identity], k) if identity in used_rows else _event_ref(used_events[identity]))
    for event in events:
        if Hc(event["payload"]) != event["payload_hash"]:
            out["events_hash_mismatch"] += 1
            refs.append(_event_ref(event))
    openings = sorted((e for e in events if e["type"] == "ELECTION_OPENED"), key=lambda e: e["seq"])
    if openings:
        interval, counts = config["checkpoint_interval_seconds"], Counter()
        for commit in checkpoints:
            if commit["payload"]["reason"] != "PERIODIC":
                continue
            offset = int((_time(commit["occurred_at"]) - _time(openings[0]["occurred_at"])).total_seconds())
            counts[offset // interval] += 1
            if offset <= 0 or offset % interval or counts[offset // interval] > 1:
                out["checkpoint_cadence_violations"] += 1
                refs.append(_event_ref(commit))
    reason, detail = ("VIEW_UNAVAILABLE", "Historical checkpoint views are unavailable.") if missing_history else (None, None)
    if len(created_events) != 1:
        return any(out[x] for x in LOCAL_KEYS), "OTHER", "Exactly one ELECTION_CREATED is required."
    created = created_events[0]
    try:
        public = _raw(config["public_key"])
        digest = config_hash({**config, "public_key_hash": H(public)})
        if digest != created["payload"]["config_hash"] or config["config_hash"] != digest:
            out["config_mismatch"] += 1
            refs.extend([_event_ref(created), _row_ref(config, k)])
    except (ValueError, TypeError):
        out["config_mismatch"] += 1
    is_tally = current["payload"]["reason"] == "TALLY"
    tallies = [e for e in events if e["type"] == "TALLY_PUBLISHED" and e["checkpoint"] == k] if is_tally else []
    tally = tallies[0]["payload"] if len(tallies) == 1 else None
    if is_tally and len(tallies) != 1:
        reason = "OTHER"
        detail = "TALLY_PUBLISHED missing at the TALLY checkpoint" if not tallies else "TALLY checkpoint contains more than one TALLY_PUBLISHED"
    if tally is not None:
        refs.append(_event_ref(tallies[0]))
        finals = [e["checkpoint"] for e in checkpoints if e["payload"]["reason"] == "CLOSE_FINAL"]
        out["tally_final_checkpoint_mismatches"] = int(len(finals) != 1 or tally["final_checkpoint"] not in finals)
        ledger_hashes = [e["payload"]["ballot_hash"] for e in events
                         if e["type"] == "BALLOT_RECEIVED" and e["checkpoint"] <= tally["final_checkpoint"]]
        row_hashes = [r["ballot_hash"] for r in views["ballot_receipts"] if r["received_checkpoint"] <= tally["final_checkpoint"]]
        out["tally_root_mismatches"] = int(ballot_set_root(ledger_hashes) != tally["ballot_set_root"]
                                            or ballot_set_root(row_hashes) != tally["ballot_set_root"])
        # hashing.md includes every candidate, even a missing zero-vote entry.
        published = {r["candidate_id"]: r["votes"] for r in tally["results"]}
        all_results = [{"candidate_id": c["candidate_id"], "votes": published.get(c["candidate_id"], 0)} for c in config["candidates"]]
        out["tally_result_hash_mismatches"] = int(result_hash({**tally, "election_id": s["election_id"],
                                                             "results": all_results}) != tally["result_hash"])
    scheme = config.get("ballot_scheme", "SEALED_BOX_V1")
    if scheme == "ELGAMAL_ED25519_V1":
        extra, why, evidence, explanation = audit_elgamal(config, s["election_id"], events, views["ballot_receipts"], k, is_tally)
        out.update(extra)
        refs.extend(evidence)
        if why:
            reason, detail = why, explanation
    elif scheme == "SEALED_BOX_V1":
        why = _sealed_tally(config, s["election_id"], created, events, views["ballot_receipts"], tally, out, refs)
        if why:
            reason, detail = why, "The sealed-box private key is not revealed at the TALLY checkpoint."
    else:
        reason, detail = "OTHER", "The ballot scheme is unsupported; verification cannot be completed."
    failed = any(out.get(key) for key in (*LOCAL_KEYS, *TALLY_KEYS))
    return failed, reason, detail
