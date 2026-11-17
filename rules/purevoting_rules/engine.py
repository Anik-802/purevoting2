"""Deterministic rules over explicit, complete, cumulative local audit inputs.

No network I/O is hidden in this module. The caller supplies contract-shaped rows,
accepted LedgerEvents and Gateway Rejections for one committed checkpoint. The
adapter must fetch every page and select the requested as-of views. Local guards
detect visible contradictions (future row checkpoints or a known historical commit
omitted from events); they cannot prove that an external response is complete.
R4/R5 additionally require independently read, confirmed contract evidence. A
clean local subset is insufficient for PASS when that evidence is unavailable.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import RuleOutcome
from .admin_audit import audit_admin
from .chain_audit import audit_chain
from .integrity_local import audit_local

VOTER_TYPES = {"TOKEN_USED", "BALLOT_RECEIVED"}
TABLES = ("roster", "token_status", "credential_issuance", "ballot_receipts", "admin_actions", "rejected_attempts")
_RULE_VIEWS = {
    "R1": ("token_status", "credential_issuance", "rejected_attempts"),
    "R2": ("election_config", "admin_actions", "rejected_attempts"),
    "R3": TABLES,
    "R4": ("admin_actions", "election_config", "roster", "token_status"),
    "R5": TABLES + ("election_config",),
}
_ROW_CHECKPOINT_FIELDS = {
    "roster": ("added_checkpoint", "removed_checkpoint"),
    "token_status": ("issued_checkpoint", "used_checkpoint", "revoked_checkpoint"),
    "credential_issuance": ("issued_checkpoint", "used_checkpoint"),
    "ballot_receipts": ("received_checkpoint",),
    "admin_actions": ("requested_checkpoint", "status_checkpoint"),
    "rejected_attempts": ("checkpoint",),
}
# Coverage is the implemented predicate set, not a claim that inputs exist or
# that a deployed integration has passed. Each missing input remains an explicit
# INCONCLUSIVE even with complete implementation coverage.
R4_UNIMPLEMENTED = ()
R5_UNIMPLEMENTED = ()
OBSERVED_KEYS = {
    "R1": "accepted_duplicate_tokens accepted_duplicate_nullifiers unknown_tokens unknown_nullifiers used_then_revoked refused_duplicates".split(),
    "R2": "events_before_start events_after_end ledger_rejected_window events_after_close_final refused_not_open".split(),
    "R3": "roster_active tokens_active token_used credentials_issued ballots_received ledger_token_used ledger_ballots ledger_rejected_voter_events redeemed_not_voted".split(),
    "R4": "unapproved_changes invalid_signatures duplicate_executions missing_effects target_mismatches roster_chain_breaks executed_actions failed_actions".split(),
    "R5": "rejected_checkpoints chain_state_mismatch governance_mismatch checkpoint_cadence_violations row_ledger_mismatches key_mismatch tables_mismatched sealed_hash_mismatches root_mismatches config_mismatch events_hash_mismatch anchors_mismatch events_unanchored".split(),
}
EXPECTED = {
    "R1": "Accepted tokens and nullifiers are unique, known, and never both used and revoked; refusals are counted separately.",
    "R2": "Every accepted voter bucket lies wholly in the effective window; no voter activity follows CLOSE_FINAL.",
    "R3": "Atomic redemption, committed view counts, per-checkpoint voter counts, ledger outcomes and tally totals reconcile.",
    "R4": "Every administrative effect has one valid approved execution with exact authorized targets and effects.",
    "R5": "Every historical snapshot, accepted row, sealed ballot, hash, independent chain commitment and tally agrees.",
}


class MissingEvidence(Exception):
    def __init__(self, message: str, reason: str = "VIEW_UNAVAILABLE"):
        super().__init__(message)
        self.reason = reason


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Audit timestamps must carry a timezone")
    return parsed


def _ceil_bucket(value: str, seconds: int) -> datetime:
    stamp = int(_time(value).timestamp())
    return datetime.fromtimestamp(((stamp + seconds - 1) // seconds) * seconds, tz=timezone.utc)


def _rows(s: dict, name: str) -> list[dict]:
    rows = s.get("views", {}).get(name)
    if not isinstance(rows, list):
        raise MissingEvidence(f"Required {name} view is unavailable at the selected checkpoint.")
    return rows


def _records(s: dict, name: str) -> list[dict]:
    records = s.get(name)
    if not isinstance(records, list):
        raise MissingEvidence(f"Complete cumulative {name} evidence was not supplied.", "OTHER")
    # A repeated page/transport replay is the same record, never another accepted
    # use. Conflicting copies are incomplete evidence rather than an arbitrary win.
    unique: dict[str, dict] = {}
    key = "event_id" if name == "events" else "rejection_id"
    for record in records:
        if record["election_id"] != s["election_id"] or record["checkpoint"] > s["checkpoint"]:
            continue
        identity = record[key]
        if identity in unique and unique[identity] != record:
            raise MissingEvidence("Conflicting copies of one ledger evidence record.", "OTHER")
        unique[identity] = record
    return list(unique.values())


def _event_ref(event: dict) -> dict[str, str]:
    return {"kind": "ledger_event", "ref": event["event_id"]}


def _row_ref(row: dict, k: int) -> dict[str, str]:
    return {"kind": "view_row", "ref": f"{row['view']}:{row['row_key']}@{k}"}


def _checkpoint(s: dict, events: list[dict]) -> dict:
    found = [e for e in events if e["type"] == "CHECKPOINT_COMMITTED" and e["checkpoint"] == s["checkpoint"]
             and e["event_id"] == s["checkpoint_event_id"]]
    if len(found) != 1:
        raise MissingEvidence("The selected accepted checkpoint event is unavailable.", "OTHER")
    return found[0]


def _input_boundary_issues(rule_id: str, s: dict, events: list[dict], rejections: list[dict]) -> list[tuple[str, str]]:
    """Detect provable envelope defects without assuming contiguous checkpoints.

    A rejected checkpoint may legitimately have no accepted checkpoint event.
    We only require an event when the caller supplied its accepted historical
    copy, and only inspect view fields that this rule reads. A visible defect
    prevents PASS; an independently demonstrated violation can still be FAIL.
    """
    issues: set[tuple[str, str]] = set()
    k = s["checkpoint"]
    accepted_commits = {(e["event_id"], e["checkpoint"]) for e in events if e["type"] == "CHECKPOINT_COMMITTED"}
    history = s.get("history", {})
    if isinstance(history, dict):
        rejected_commits = {(r["event_id"], r["checkpoint"]) for r in rejections if r["type"] == "CHECKPOINT_COMMITTED"}
        for past in history.values():
            previous = past.get("checkpoint_event") if isinstance(past, dict) else None
            if not isinstance(previous, dict) or previous.get("type") != "CHECKPOINT_COMMITTED":
                continue
            if previous.get("election_id") != s["election_id"] or previous["checkpoint"] > k:
                continue
            identity = (previous["event_id"], previous["checkpoint"])
            if identity not in accepted_commits and identity not in rejected_commits:
                issues.add(("OTHER", "A known historical CHECKPOINT_COMMITTED is missing from cumulative accepted events."))
    views = s.get("views", {})
    if isinstance(views, dict):
        for name in _RULE_VIEWS[rule_id]:
            rows = views.get(name)
            if not isinstance(rows, list):
                continue  # The rule's existing missing-view check supplies the reason.
            for row in rows:
                if not isinstance(row, dict):
                    continue  # The normal shape/type checks reject this input.
                for field in _ROW_CHECKPOINT_FIELDS.get(name, ()):
                    value = row.get(field)
                    if value is not None and (type(value) is not int or value < 0 or value > k):
                        issues.add(("VIEW_UNAVAILABLE", f"{name}.{field} is invalid for the selected as-of checkpoint."))
    return sorted(issues)


def _counts(views: dict[str, list[dict]]) -> dict[str, int]:
    return {
        "roster_active": sum(r["status"] == "ACTIVE" for r in views["roster"]),
        "tokens_active": sum(r["status"] in {"ISSUED", "USED"} for r in views["token_status"]),
        "token_used": sum(r["used_checkpoint"] is not None for r in views["token_status"]),
        "credentials_issued": len(views["credential_issuance"]),
        "ballots_received": len(views["ballot_receipts"]),
        "rejected_attempts": len(views["rejected_attempts"]),
    }


def _r1(s: dict, events: list[dict], rejections: list[dict], out: dict, refs: list) -> tuple[bool, str | None]:
    tokens = {r["row_key"]: r for r in _rows(s, "token_status")}
    credentials = {r["row_key"]: r for r in _rows(s, "credential_issuance")}
    attempts = _rows(s, "rejected_attempts")
    used = [e for e in events if e["type"] == "TOKEN_USED"]
    ballots = [e for e in events if e["type"] == "BALLOT_RECEIVED"]
    token_counts = Counter(e["payload"]["token_hash"] for e in used)
    ballot_counts = Counter(e["payload"]["nullifier_hash"] for e in ballots)
    revoked_events = {e["payload"]["token_hash"] for e in events if e["type"] == "TOKEN_REVOKED"}
    state_conflicts = {key for key, row in tokens.items() if row["used_checkpoint"] is not None and row["revoked_checkpoint"] is not None}
    state_conflicts |= set(token_counts) & revoked_events
    for e in used:
        row = tokens.get(e["payload"]["token_hash"])
        if row and row["revoked_checkpoint"] is not None and row["revoked_checkpoint"] <= e["checkpoint"]:
            state_conflicts.add(row["row_key"])
    out.update(
        accepted_duplicate_tokens=sum(n > 1 for n in token_counts.values()),
        accepted_duplicate_nullifiers=sum(n > 1 for n in ballot_counts.values()),
        unknown_tokens=sum(key not in tokens for key in token_counts),
        unknown_nullifiers=sum(key not in credentials for key in ballot_counts),
        used_then_revoked=len(state_conflicts),
        refused_duplicates=sum(r["reject_code"] in {"DUPLICATE_CONTENT", "TOKEN_REVOKED", "TOKEN_ALREADY_USED"} for r in rejections)
        + sum(r["kind"] in {"TOKEN_ALREADY_USED", "CREDENTIAL_ALREADY_USED"} for r in attempts),
    )
    for e in used + ballots:
        key = e["payload"].get("token_hash", e["payload"].get("nullifier_hash"))
        bad = (token_counts[key] > 1 or key not in tokens or key in state_conflicts) if e["type"] == "TOKEN_USED" else (ballot_counts[key] > 1 or key not in credentials)
        if bad:
            refs.append(_event_ref(e))
    refs.extend(_row_ref(tokens[key], s["checkpoint"]) for key in sorted(state_conflicts & tokens.keys()))
    return any(out[key] for key in OBSERVED_KEYS["R1"] if key != "refused_duplicates"), None


def _r2(s: dict, events: list[dict], rejections: list[dict], out: dict, refs: list) -> tuple[bool, str | None]:
    configs = _rows(s, "election_config")
    if len(configs) != 1:
        raise MissingEvidence("Exactly one election_config row is required.")
    config = configs[0]
    attempts = _rows(s, "rejected_attempts")
    actions = {r["row_key"]: r for r in _rows(s, "admin_actions")}
    start = _time(config["config_window_start"])
    end = _time(config["config_window_end"])
    bucket = config["time_bucket_seconds"]
    system = sorted((e for e in events if e["type"] not in VOTER_TYPES), key=lambda e: e["seq"])
    # Last accepted change wins; rejected extensions do not extend this window.
    for event in system:
        if event["type"] == "WINDOW_CHANGED":
            end = _time(event["payload"]["new_window_end"])
    unavailable_target = False
    for event in system:
        if event["type"] == "ELECTION_CLOSED":
            end = min(end, _time(event["payload"]["closed_at"]))
        elif event["type"] == "ADMIN_ACTION_EXECUTED" and event["payload"]["action_type"] == "ELECTION_CLOSE_EARLY":
            action = actions.get(event["payload"]["action_id"])
            if not action or "closed_at" not in action["target"]:
                unavailable_target = True
                refs.append(_event_ref(event))
                continue
            effective = max(_time(action["target"]["closed_at"]), _ceil_bucket(event["occurred_at"], bucket))
            end = min(end, effective)
    final = [e["checkpoint"] for e in events if e["type"] == "CHECKPOINT_COMMITTED" and e["payload"]["reason"] == "CLOSE_FINAL"]
    close_final = min(final) if final else None
    out.update({key: 0 for key in OBSERVED_KEYS["R2"]})
    out["refused_not_open"] = sum(r["kind"] == "ELECTION_NOT_OPEN" for r in attempts)
    for event in events:
        if event["type"] not in VOTER_TYPES:
            continue
        before = _time(event["occurred_at"]) < start
        after = _time(event["occurred_at"]) + timedelta(seconds=event["time_bucket_seconds"]) > end
        after_final = close_final is not None and event["checkpoint"] > close_final
        out["events_before_start"] += before
        out["events_after_end"] += after
        out["events_after_close_final"] += after_final
        if before or after or after_final:
            refs.append(_event_ref(event))
    for rejection in rejections:
        if rejection["type"] not in VOTER_TYPES:
            continue
        bad_window = rejection["reject_code"] == "ELECTION_NOT_OPEN"
        after_final = close_final is not None and rejection["checkpoint"] > close_final
        out["ledger_rejected_window"] += bad_window
        out["events_after_close_final"] += after_final
        if bad_window or after_final:
            refs.append({"kind": "ledger_rejection", "ref": rejection["rejection_id"]})
    failed = any(out[key] for key in OBSERVED_KEYS["R2"] if key != "refused_not_open")
    return failed, "VIEW_UNAVAILABLE" if unavailable_target else None


def _r3(s: dict, events: list[dict], rejections: list[dict], out: dict, refs: list) -> tuple[bool, str | None]:
    views = {name: _rows(s, name) for name in TABLES}
    counts = _counts(views)
    commit = _checkpoint(s, events)
    accepted = Counter(e["type"] for e in events if e["type"] in VOTER_TYPES)
    rejected = Counter(r["type"] for r in rejections if r["type"] in VOTER_TYPES)
    out.update({key: counts[key] for key in counts if key != "rejected_attempts"})
    out.update(ledger_token_used=accepted["TOKEN_USED"], ledger_ballots=accepted["BALLOT_RECEIVED"],
               ledger_rejected_voter_events=sum(rejected.values()), redeemed_not_voted=counts["credentials_issued"] - counts["ballots_received"])
    violations = []
    comparisons = {
        "atomic_redemption": counts["token_used"] == counts["credentials_issued"],
        "ballots_within_credentials": counts["ballots_received"] <= counts["credentials_issued"],
        "credentials_within_roster": counts["credentials_issued"] <= counts["roster_active"],
        "tokens_within_roster": counts["tokens_active"] <= counts["roster_active"],
        "committed_counts": counts == commit["payload"]["counts"],
        "ledger_token_outcomes": accepted["TOKEN_USED"] + rejected["TOKEN_USED"] == counts["token_used"],
        "ledger_ballot_outcomes": accepted["BALLOT_RECEIVED"] + rejected["BALLOT_RECEIVED"] == counts["ballots_received"],
    }
    violations.extend(key for key, good in comparisons.items() if not good)
    per_checkpoint_mismatch = 0
    for event in events:
        if event["type"] != "CHECKPOINT_COMMITTED":
            continue
        j = event["checkpoint"]
        actual = {
            "TOKEN_USED": sum(r["used_checkpoint"] == j for r in views["token_status"]),
            "BALLOT_RECEIVED": sum(r["received_checkpoint"] == j for r in views["ballot_receipts"]),
        }
        if event["payload"]["voter_events"] != actual:
            per_checkpoint_mismatch += 1
            refs.append(_event_ref(event))
    if per_checkpoint_mismatch:
        violations.append("checkpoint_voter_events")
    out["checkpoint_voter_event_mismatches"] = per_checkpoint_mismatch
    out["count_relationship_violations"] = violations
    missing_tally = False
    if commit["payload"]["reason"] == "TALLY":
        tallies = [e for e in events if e["type"] == "TALLY_PUBLISHED" and e["checkpoint"] == s["checkpoint"]]
        finals = [e["checkpoint"] for e in events if e["type"] == "CHECKPOINT_COMMITTED" and e["payload"]["reason"] == "CLOSE_FINAL"]
        if len(tallies) != 1:
            missing_tally = True
        else:
            tally = tallies[0]["payload"]
            n = sum(e["type"] == "BALLOT_RECEIVED" and e["checkpoint"] <= tally["final_checkpoint"] for e in events)
            r = sum(e["type"] == "BALLOT_RECEIVED" and e["checkpoint"] <= tally["final_checkpoint"] for e in rejections)
            mismatches = int(len(finals) != 1 or tally["final_checkpoint"] not in finals)
            mismatches += int(tally["ballot_count"] + tally["invalid_ballots"] != n)
            mismatches += int(tally["excluded_ballots"] != r)
            out.update(tally_final_checkpoint=tally["final_checkpoint"], tally_ballot_count=tally["ballot_count"],
                       tally_invalid_ballots=tally["invalid_ballots"], tally_excluded_ballots=tally["excluded_ballots"],
                       tally_accepted_ballots=n, tally_rejected_ballots=r, tally_count_mismatches=mismatches)
            if mismatches:
                violations.append("tally_counts")
                refs.append(_event_ref(tallies[0]))
    if violations:
        refs.extend({"kind": "snapshot", "ref": f"{table}@{s['checkpoint']}"} for table in TABLES)
        refs.extend({"kind": "ledger_rejection", "ref": r["rejection_id"]} for r in rejections if r["type"] in VOTER_TYPES)
    return bool(violations), "OTHER" if missing_tally else None


def _r4(s: dict, events: list[dict], rejections: list[dict], out: dict, refs: list) -> tuple[bool, str | None]:
    for name in _RULE_VIEWS["R4"]:
        _rows(s, name)
    if len(_rows(s, "election_config")) != 1:
        raise MissingEvidence("Exactly one election_config row is required.")
    return audit_admin(s, events, rejections, out, refs)


def _r5(s: dict, events: list[dict], rejections: list[dict], out: dict, refs: list) -> tuple[bool, str | None]:
    for name in _RULE_VIEWS["R5"]:
        _rows(s, name)
    if len(_rows(s, "election_config")) != 1:
        raise MissingEvidence("Exactly one election_config row is required.")
    local_failed, local_reason, detail = audit_local(s, events, rejections, out, refs)
    chain_failed, chain_reason = audit_chain(s, events, rejections, out, refs)
    if chain_reason:
        out["chain_evidence_reason"] = chain_reason
    if detail:
        out["evidence_detail"] = detail
    return local_failed or chain_failed, local_reason or chain_reason


def evaluate_rule(rule_id: str, snapshot: dict[str, Any]) -> RuleOutcome:
    """Evaluate one rule without mutating inputs or treating unavailable data as clean."""
    if rule_id not in OBSERVED_KEYS:
        raise ValueError(f"Unknown rule ID: {rule_id}")
    observed: dict[str, Any] = dict.fromkeys(OBSERVED_KEYS[rule_id])
    refs = []
    if snapshot.get("checkpoint_event_id"):
        refs.append({"kind": "ledger_event", "ref": snapshot["checkpoint_event_id"]})
    unimplemented = R4_UNIMPLEMENTED if rule_id == "R4" else R5_UNIMPLEMENTED if rule_id == "R5" else ()
    boundary_issues: list[tuple[str, str]] = []
    try:
        events, rejections = _records(snapshot, "events"), _records(snapshot, "rejections")
        _checkpoint(snapshot, events)
        boundary_issues = _input_boundary_issues(rule_id, snapshot, events, rejections)
        failed, reason = {"R1": _r1, "R2": _r2, "R3": _r3, "R4": _r4, "R5": _r5}[rule_id](snapshot, events, rejections, observed, refs)
        if failed:
            result, reason = "FAIL", None
            summary = f"{rule_id}: supplied cumulative evidence contains a violation; inspect the linked evidence and observed counts."
        elif reason:
            result = "INCONCLUSIVE"
            summary = f"{rule_id}: checked predicates found no demonstrated violation, but required evidence or implementation coverage is incomplete."
        else:
            result = "PASS"
            summary = f"{rule_id}: all required predicates agree on the supplied complete audit evidence."
    except MissingEvidence as exc:
        result, reason, summary = "INCONCLUSIVE", exc.reason, str(exc)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # The fixture importer/schema gate should prevent this; never conceal a
        # malformed or missing required field by substituting an empty clean view.
        result, reason = "INCONCLUSIVE", "OTHER"
        summary = f"{rule_id}: malformed required audit evidence ({type(exc).__name__}); no complete decision is possible."
    if observed.get("evidence_detail"):
        summary += " " + observed["evidence_detail"]
    if boundary_issues:
        observed["input_boundary_issues"] = [message for _, message in boundary_issues]
        if result != "FAIL":
            result = "INCONCLUSIVE"
            reason = "VIEW_UNAVAILABLE" if any(code == "VIEW_UNAVAILABLE" for code, _ in boundary_issues) else "OTHER"
            summary = f"{rule_id}: the supplied cumulative/as-of evidence has visible inconsistencies; no complete decision is possible."
        else:
            summary += " The input also has a visible cumulative/as-of inconsistency, listed in observed."
    deduped = {(r["kind"], r["ref"]): r for r in refs}
    return RuleOutcome(rule_id=rule_id, result=result, observed=observed, expected=EXPECTED[rule_id], summary=summary,
                       evidence_refs=[deduped[key] for key in sorted(deduped)], inconclusive_reason=reason,
                       coverage="partial" if unimplemented else "complete", unimplemented_checks=unimplemented)


def evaluate_all(snapshot: dict[str, Any]) -> list[RuleOutcome]:
    return [evaluate_rule(rule_id, snapshot) for rule_id in OBSERVED_KEYS]
