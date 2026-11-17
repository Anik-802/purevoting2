"""R4 complete-evidence fixtures and adversarial changes (no Core imports)."""
from copy import deepcopy

import pytest

from purevoting_rules.admin_audit import audit_admin
from purevoting_rules.fixtures import _Factory, _base, _commit_payload
from purevoting_rules.hashing import H, proposal_digest, review_digest, roster_hash, target_hash
from purevoting_rules.signing import EIP191Signer


def clean():
    factory = _Factory(1728)
    s = _base(factory)
    signers = {org: EIP191Signer((i + 1).to_bytes(32, "big")) for i, org in enumerate(("org_commission", "org_neutral", "org_observer"))}
    organizations = [{"org_id": org, "signer": signer.address} for org, signer in signers.items()]
    config = s["views"]["election_config"][0]
    config["organizations"] = organizations
    created = next(e for e in s["events"] if e["type"] == "ELECTION_CREATED")
    created["payload"]["organizations"] = deepcopy(organizations)
    for past in s["history"].values():
        past["views"]["election_config"][0]["organizations"] = deepcopy(organizations)
    s["chain"] = {"available": True,
                  "registry": {org: {"signer": signer.address, "registered_by": "0x" + "aa" * 20} for org, signer in signers.items()},
                  "operator": {"org_id": H("org_commission"), "confirmed_by_all": True},
                  "governance": {"org_ids": [H(o) for o in signers], "operator_org_id": H("org_commission"), "approval_threshold": 2},
                  "proposals": {}}
    return s, factory, signers


def outcome(s):
    out, refs = {}, []
    failed, reason = audit_admin(s, s["events"], s["rejections"], out, refs)
    return ("FAIL" if failed else "INCONCLUSIVE" if reason else "PASS"), reason, out, refs


def commit_next(s, factory, checkpoint=3, reason="PERIODIC", at="2026-11-03T09:30:00Z"):
    seq = max(e.get("seq", 0) for e in s["events"] if e.get("source") == "core") + 1
    event = factory.event("CHECKPOINT_COMMITTED", checkpoint, at, _commit_payload(s["views"], checkpoint, reason), seq)
    s["events"].append(event)
    s["checkpoint"] = checkpoint
    s["checkpoint_event_id"] = event["event_id"]
    s["history"][str(checkpoint)] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(event)}
    return event


def action(s, factory, signers, typ, target, checkpoint=3, execute=True, failed=False):
    aid, pid = factory.identity("act"), factory.identity("prp")
    org = "org_commission"
    th = target_hash({"election_id": s["election_id"], "action_id": aid, "action_type": typ, "target": target})
    row = {"view": "admin_actions", "row_key": aid, "action_type": typ, "status": "FAILED" if failed else "EXECUTED" if execute else "APPROVED",
           "requested_by_org": org, "proposal_id": pid, "target": target, "target_hash": th,
           "requested_checkpoint": checkpoint, "status_checkpoint": checkpoint}
    s["views"]["admin_actions"].append(row)
    gwseq = max((e.get("seq", 0) for e in s["events"] if e.get("source") == "gateway"), default=0)
    t = "2026-11-03T09:15:00Z"
    rp = {"action_id": aid, "proposal_id": pid, "action_type": typ, "target_hash": th,
          "requested_by_org": org, "threshold": 2, "expires_at": "2026-11-04T09:15:00Z",
          "signature": signers[org].sign_digest(proposal_digest(pid, aid, s["election_id"], typ, th, org, "REQUEST"))}
    request = factory.event("ADMIN_ACTION_REQUESTED", checkpoint, t, rp, gwseq + 1, "gateway")
    s["events"].append(request)
    for i, org in enumerate(("org_neutral", "org_observer"), start=1):
        dp = {"action_id": aid, "proposal_id": pid, "org_id": org, "decision": "APPROVE", "approvals_so_far": i,
              "threshold": 2, "submitted_via": "gateway", "signature": signers[org].sign_digest(
                  proposal_digest(pid, aid, s["election_id"], typ, th, org, "APPROVE"))}
        s["events"].append(factory.event("APPROVAL_RECORDED", checkpoint, t, dp, gwseq + i + 1, "gateway"))
    s["events"].append(factory.event("PROPOSAL_CLOSED", checkpoint, t,
                                    {"action_id": aid, "proposal_id": pid, "outcome": "APPROVED"}, gwseq + 4, "gateway"))
    s["chain"]["proposals"][pid] = {"status": "APPROVED", "target_hash": th, "approvals": 2, "executed": execute}
    ex = None
    if execute or failed:
        seq = max(e.get("seq", 0) for e in s["events"] if e.get("source") == "core") + 1
        ep = {"action_id": aid, "proposal_id": pid, "action_type": typ}
        ep.update({"reason_code": "INVALID_STATE_TRANSITION"} if failed else {"target_hash": th})
        ex = factory.event("ADMIN_ACTION_FAILED" if failed else "ADMIN_ACTION_EXECUTED", checkpoint, t, ep, seq)
        s["events"].append(ex)
    return row, ex, request


def effect(s, factory, execution, kind, payload):
    ep = {"action_id": execution["payload"]["action_id"], "proposal_id": execution["payload"]["proposal_id"], **payload}
    seq = max(e.get("seq", 0) for e in s["events"] if e.get("source") == "core") + 1
    ev = factory.event(kind, execution["checkpoint"], execution["occurred_at"], ep, seq)
    s["events"].append(ev)
    return ev


def test_clean_complete_evidence_can_pass_and_is_immutable():
    s, _, _ = clean()
    before = deepcopy(s)
    assert outcome(s)[:2] == ("PASS", None)
    assert s == before


@pytest.mark.parametrize("known_registry_violation", [False, True])
def test_missing_selected_checkpoint_is_unavailable_and_preserves_known_violations(known_registry_violation):
    s, _, _ = clean()
    s["events"] = [e for e in s["events"] if e["event_id"] != s["checkpoint_event_id"]]
    if known_registry_violation:
        s["chain"]["registry"]["org_observer"]["signer"] = EIP191Signer((23).to_bytes(32, "big")).address
    before = deepcopy(s)
    result, reason, observed, _ = outcome(s)
    assert (result, reason) == ("FAIL" if known_registry_violation else "INCONCLUSIVE", "OTHER")
    assert observed["invalid_signatures"] == int(known_registry_violation)
    assert "The selected CHECKPOINT_COMMITTED evidence is unavailable for R4." in observed["evidence_unavailable"]
    assert s == before


@pytest.mark.parametrize("gap", ["chain", "registry", "operator", "governance", "history"])
def test_missing_evidence_never_passes(gap):
    s, _, _ = clean()
    if gap == "chain":
        del s["chain"]
    elif gap == "history":
        del s["history"]["0"]
    else:
        del s["chain"][gap]
    assert outcome(s)[0] == "INCONCLUSIVE"


def test_unapproved_database_added_voter_is_detected_even_when_counts_recommitted():
    s, f, _ = clean()
    s["views"]["roster"].append({"view": "roster", "row_key": f.identity("vtr"), "status": "ACTIVE", "added_checkpoint": 3, "removed_checkpoint": None})
    commit_next(s, f)
    result = outcome(s)
    assert result[0] == "FAIL"
    assert result[2]["roster_chain_breaks"] > 0


def test_token_deleted_from_later_view_is_detected():
    s, f, _ = clean()
    s["views"]["token_status"].pop()
    commit_next(s, f)
    assert outcome(s)[0] == "FAIL"


def test_registry_mismatch_and_operator_mismatch_fail():
    s, _, _ = clean()
    s["chain"]["registry"]["org_observer"]["signer"] = EIP191Signer((23).to_bytes(32, "big")).address
    assert outcome(s)[2]["invalid_signatures"] == 1
    s, _, _ = clean()
    s["chain"]["operator"]["org_id"] = H("org_neutral")
    assert outcome(s)[0] == "FAIL"


def window_case():
    s, f, signers = clean()
    row, ex, req = action(s, f, signers, "WINDOW_CHANGE", {"new_window_end": "2026-11-03T10:00:00Z"})
    eff = effect(s, f, ex, "WINDOW_CHANGED", {"old_window_end": "2026-11-03T09:30:00Z", "new_window_end": row["target"]["new_window_end"]})
    s["views"]["election_config"][0]["window_end"] = row["target"]["new_window_end"]
    commit_next(s, f)
    return s, f, signers, row, ex, req, eff


def test_valid_window_action_signatures_and_effect_pass():
    assert outcome(window_case()[0])[0] == "PASS"


@pytest.mark.parametrize("mutation", ["request_signature", "approval_signature", "self_approval", "outsider", "wrong_target", "chain_unexecuted", "old_window", "window_outside_effect_window", "duplicate_execution"])
def test_administrative_attacks_are_detected(mutation):
    s, f, signers, row, ex, req, eff = window_case()
    approval = next(e for e in s["events"] if e["type"] == "APPROVAL_RECORDED")
    if mutation == "request_signature":
        req["payload"]["signature"] = signers["org_neutral"].sign_digest(H("wrong"))
    elif mutation == "approval_signature":
        approval["payload"]["signature"] = signers["org_neutral"].sign_digest(H("wrong"))
    elif mutation == "self_approval":
        approval["payload"]["org_id"] = "org_commission"
    elif mutation == "outsider":
        approval["payload"]["org_id"] = "org_outsider"
    elif mutation == "wrong_target":
        row["target"]["new_window_end"] = "2026-11-03T10:15:00Z"
    elif mutation == "chain_unexecuted":
        s["chain"]["proposals"][row["proposal_id"]]["executed"] = False
    elif mutation == "old_window":
        eff["payload"]["old_window_end"] = "2026-11-03T09:45:00Z"
    elif mutation == "window_outside_effect_window":
        eff["seq"] += 2
    elif mutation == "duplicate_execution":
        other = deepcopy(ex)
        other["event_id"] = f.identity("ev")
        other["seq"] = eff["seq"] + 2
        s["events"].append(other)
    assert outcome(s)[0] == "FAIL"


def test_missing_confirmed_proposal_is_unknown_not_a_valid_approval():
    s, _, _, row, *_ = window_case()
    del s["chain"]["proposals"][row["proposal_id"]]
    assert outcome(s)[:2] == ("INCONCLUSIVE", "NODE_UNAVAILABLE")


def test_action_failed_without_effect_is_not_a_violation():
    s, f, signers = clean()
    action(s, f, signers, "WINDOW_CHANGE", {"new_window_end": "2026-11-03T10:00:00Z"}, execute=False, failed=True)
    commit_next(s, f)
    assert outcome(s)[0] == "PASS"


def test_missing_window_effect_fails():
    s, f, signers = clean()
    action(s, f, signers, "WINDOW_CHANGE", {"new_window_end": "2026-11-03T10:00:00Z"})
    commit_next(s, f)
    assert outcome(s)[2]["missing_effects"] == 1


def test_add_only_amendment_needs_no_revocation_and_chains_roots():
    s, f, signers = clean()
    active = {r["row_key"] for r in s["views"]["roster"]}
    for _ in range(2):
        key = f.identity("vtr")
        row, ex, req = action(s, f, signers, "ROSTER_AMEND", {"add": [key], "remove": []})
        before = roster_hash(active)
        active.add(key)
        effect(s, f, ex, "ROSTER_AMENDED", {"roster_hash_before": before, "roster_hash_after": roster_hash(active), "voter_count": len(active), "added": 1, "removed": 0})
        s["views"]["roster"].append({"view": "roster", "row_key": key, "status": "ACTIVE", "added_checkpoint": 3, "removed_checkpoint": None})
    commit_next(s, f)
    assert outcome(s)[0] == "PASS"
    amends = [e for e in s["events"] if e["type"] == "ROSTER_AMENDED"]
    amends[1]["payload"]["roster_hash_before"] = amends[0]["payload"]["roster_hash_before"]
    assert outcome(s)[2]["roster_chain_breaks"] > 0


def test_exact_remove_and_revocation_sets_are_checked():
    s, f, signers = clean()
    victim = s["views"]["roster"][-1]
    token = s["views"]["token_status"][-1]
    active = {r["row_key"] for r in s["views"]["roster"]}
    _, ex, _ = action(s, f, signers, "ROSTER_AMEND", {"add": [], "remove": [victim["row_key"]]})
    effect(s, f, ex, "ROSTER_AMENDED", {"roster_hash_before": roster_hash(active), "roster_hash_after": roster_hash(active - {victim["row_key"]}), "voter_count": len(active) - 1, "added": 0, "removed": 1})
    rv = effect(s, f, ex, "TOKEN_REVOKED", {"voter_key": victim["row_key"], "token_hash": token["row_key"], "reason": "VOTER_REMOVED", "token_set_root": H("root")})
    victim.update(status="REMOVED", removed_checkpoint=3)
    token.update(status="REVOKED", revoked_checkpoint=3)
    commit_next(s, f)
    assert outcome(s)[0] == "PASS"
    rv["payload"]["voter_key"] = s["views"]["roster"][0]["row_key"]
    assert outcome(s)[2]["target_mismatches"] > 0


def test_reissue_revokes_exact_approved_voter_set():
    s, f, signers = clean()
    voter, token = s["views"]["roster"][-1], s["views"]["token_status"][-1]
    _, ex, _ = action(s, f, signers, "TOKEN_REISSUE", {"voter_keys": [voter["row_key"]]})
    effect(s, f, ex, "TOKEN_REVOKED", {"voter_key": voter["row_key"], "token_hash": token["row_key"], "reason": "REISSUE", "token_set_root": H("root")})
    token.update(status="REVOKED", revoked_checkpoint=3)
    commit_next(s, f)
    assert outcome(s)[0] == "PASS"


def test_early_close_only_becomes_missing_after_effective_deadline():
    s, f, signers = clean()
    action(s, f, signers, "ELECTION_CLOSE_EARLY", {"closed_at": "2026-11-03T09:30:00Z"})
    cp = commit_next(s, f, at="2026-11-03T09:30:00Z")
    assert outcome(s)[0] == "PASS"
    cp["occurred_at"] = "2026-11-03T09:45:00Z"
    assert outcome(s)[2]["missing_effects"] == 1


def test_all_voted_close_requires_turnout_counts_and_strict_earlier_boundary():
    s, f, _ = clean()
    # Original fixture has 20 active tokens, only three used: ALL_VOTED is false.
    close = f.event("ELECTION_CLOSED", 3, "2026-11-03T09:10:00Z", {"reason": "ALL_VOTED", "closed_at": "2026-11-03T09:15:00Z"}, 8)
    s["events"].append(close)
    commit_next(s, f, reason="CLOSE_FINAL", at="2026-11-03T09:15:00Z")
    assert outcome(s)[2]["unapproved_changes"] > 0


def test_review_signature_and_matching_finding_hash_are_checked():
    s, f, signers = clean()
    fid, fh = f.identity("fnd"), H("finding")
    record = f.event("FINDING_RECORDED", 2, "2026-11-03T09:15:00Z", {"finding_id": fid, "finding_hash": fh}, 1, "gateway")
    p = {"finding_id": fid, "finding_hash": fh, "org_id": "org_observer", "decision": "DISMISSED", "note_hash": H("checked")}
    p["signature"] = signers[p["org_id"]].sign_digest(review_digest(fid, s["election_id"], fh, p["org_id"], p["decision"], p["note_hash"]))
    review = f.event("FINDING_REVIEWED", 2, "2026-11-03T09:15:00Z", p, 2, "gateway")
    s["events"].extend([record, review])
    assert outcome(s)[0] == "PASS"
    review["payload"]["finding_hash"] = H("forged finding")
    assert outcome(s)[2]["invalid_signatures"] > 0


@pytest.mark.parametrize("target", [{}, {"new_window_end": []}, {"new_window_end": "bad date"}, {"new_window_end": "2026-11-03T10:00:00Z", "extra": "not signed"}])
def test_malformed_admin_target_is_a_violation_without_losing_existing_fail(target):
    s, _, _, row, *_ = window_case()
    row["target"] = target
    assert outcome(s)[0] == "FAIL"


def test_early_close_matches_requested_and_rounded_execution_time():
    s, f, signers = clean()
    row, ex, _ = action(s, f, signers, "ELECTION_CLOSE_EARLY", {"closed_at": "2026-11-03T09:15:00Z"})
    close = effect(s, f, ex, "ELECTION_CLOSED", {"reason": "EARLY", "requested_closed_at": "2026-11-03T09:15:00Z", "closed_at": "2026-11-03T09:15:00Z"})
    commit_next(s, f, reason="CLOSE_FINAL")
    assert outcome(s)[0] == "PASS"
    close["payload"]["closed_at"] = "2026-11-03T09:30:00Z"
    assert outcome(s)[2]["target_mismatches"] > 0


def test_earlier_scheduled_close_excuses_later_early_close_effect():
    s, f, signers = clean()
    action(s, f, signers, "ELECTION_CLOSE_EARLY", {"closed_at": "2026-11-03T09:45:00Z"})
    seq = max(e.get("seq", 0) for e in s["events"] if e.get("source") == "core") + 1
    s["events"].append(f.event("ELECTION_CLOSED", 3, "2026-11-03T09:30:00Z", {"reason": "SCHEDULED", "closed_at": "2026-11-03T09:30:00Z"}, seq))
    commit_next(s, f, reason="CLOSE_FINAL", at="2026-11-03T10:00:00Z")
    assert outcome(s)[0] == "PASS"


def test_election_void_effect_must_match_finding_target_set():
    s, f, signers = clean()
    fid = f.identity("fnd")
    row, ex, _ = action(s, f, signers, "ELECTION_VOID", {"finding_ids": [fid]})
    eff = effect(s, f, ex, "ELECTION_VOIDED", {"finding_ids": [fid]})
    commit_next(s, f)
    assert outcome(s)[0] == "PASS"
    eff["payload"]["finding_ids"] = []
    assert outcome(s)[2]["target_mismatches"] > 0


def test_registered_nonparticipant_signature_never_counts_toward_approval():
    s, _, _, row, _, req, _ = window_case()
    outsider = EIP191Signer((42).to_bytes(32, "big"))
    s["chain"]["registry"]["org_outsider"] = {"signer": outsider.address, "registered_by": "0x" + "bb" * 20}
    approval = next(e for e in s["events"] if e["type"] == "APPROVAL_RECORDED")
    p = approval["payload"]
    p["org_id"] = "org_outsider"
    p["signature"] = outsider.sign_digest(proposal_digest(row["proposal_id"], row["row_key"], s["election_id"], row["action_type"], row["target_hash"], "org_outsider", "APPROVE"))
    assert outcome(s)[2]["invalid_signatures"] > 0


def test_approval_only_after_proposal_closes_does_not_authorize_execution():
    s, _, _, _, _, _, _ = window_case()
    approvals = [e for e in s["events"] if e["type"] == "APPROVAL_RECORDED"]
    close = next(e for e in s["events"] if e["type"] == "PROPOSAL_CLOSED")
    approvals[1]["seq"] = close["seq"] + 1
    assert outcome(s)[0] == "FAIL"


def test_rejected_proposal_without_execution_is_not_a_violation():
    s, f, signers = clean()
    row, _, req = action(s, f, signers, "WINDOW_CHANGE", {"new_window_end": "2026-11-03T10:00:00Z"}, execute=False)
    s["events"] = [e for e in s["events"] if e["type"] != "APPROVAL_RECORDED"]
    close = next(e for e in s["events"] if e["type"] == "PROPOSAL_CLOSED")
    close["payload"]["outcome"] = "REJECTED"
    row["status"] = "REJECTED"
    s["chain"]["proposals"][row["proposal_id"]].update(status="REJECTED", approvals=0)
    commit_next(s, f)
    assert outcome(s)[0] == "PASS"
