"""R4: independently check signed administration and replay its visible effects.

Inputs are complete cumulative events, all committed view snapshots, and the
confirmed registry/proposal values collected from locally pinned RPC nodes.
Token views deliberately do not disclose a voter-to-token join: holder *sets*
are reconstructed from the full-coverage TOKEN_BATCH_ISSUED operation, and a
revocation's voter key and token hash are checked against the approved target,
visible token state, and the subsequent snapshots. No hidden mapping is invented.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from .hashing import H, proposal_digest, review_digest, roster_hash, target_hash
from .signing import verify_signature


_COUNTERS = ("unapproved_changes", "invalid_signatures", "duplicate_executions",
             "missing_effects", "target_mismatches", "roster_chain_breaks",
             "executed_actions", "failed_actions")
_FAILURES = _COUNTERS[:-2]
_EFFECTS = {"ROSTER_AMENDED", "TOKEN_REVOKED", "WINDOW_CHANGED", "ELECTION_VOIDED"}
_TARGET_FIELDS = {"ROSTER_AMEND": {"add", "remove"}, "TOKEN_REISSUE": {"voter_keys"},
                  "WINDOW_CHANGE": {"new_window_end"}, "ELECTION_CLOSE_EARLY": {"closed_at"},
                  "ELECTION_VOID": {"finding_ids"}}
_ZERO_ADDRESS = "0x" + "0" * 40


def _seconds(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("R4 timestamps must include a timezone")
    return int(dt.timestamp())


def _checked_target_hash(election_id: str, row: dict) -> str | None:
    """An admin target is an opaque object in OpenAPI; enforce its normative shape."""
    target = row.get("target")
    typ = row.get("action_type")
    if not isinstance(target, dict) or set(target) != _TARGET_FIELDS.get(typ, set()):
        return None
    for value in target.values():
        if isinstance(value, list):
            if any(not isinstance(v, str) for v in value) or value != sorted(set(value)):
                return None
        elif not isinstance(value, str):
            return None
    if typ in {"ROSTER_AMEND", "TOKEN_REISSUE", "ELECTION_VOID"}:
        if any(not isinstance(value, list) for value in target.values()):
            return None
    if typ in {"WINDOW_CHANGE", "ELECTION_CLOSE_EARLY"} and any(not isinstance(v, str) for v in target.values()):
        return None
    if typ == "TOKEN_REISSUE" and not target["voter_keys"]:
        return None
    try:
        if typ in {"WINDOW_CHANGE", "ELECTION_CLOSE_EARLY"}:
            _seconds(next(iter(target.values())))
        return target_hash({"election_id": election_id, "action_id": row["row_key"],
                            "action_type": typ, "target": target})
    except (KeyError, TypeError, ValueError):
        return None


def _event_ref(event: dict) -> dict:
    return {"kind": "ledger_event", "ref": event["event_id"]}


class _Audit:
    def __init__(self, snapshot, events, out, refs):
        self.s, self.events, self.out, self.refs = snapshot, events, out, refs
        self.missing: set[str] = set()
        self.missing_details: set[str] = set()
        self.k = snapshot["checkpoint"]
        self.core = sorted((e for e in events if e.get("source") == "core" and e.get("seq") is not None),
                           key=lambda e: e["seq"])
        self.checkpoints = [e for e in self.core if e["type"] == "CHECKPOINT_COMMITTED"]
        self.histories = {}
        for cp in self.checkpoints:
            j = cp["checkpoint"]
            views = snapshot.get("views") if j == self.k else snapshot.get("history", {}).get(str(j), {}).get("views")
            self.histories[j] = views
        self.chain = snapshot.get("chain", {})
        self.chain_ok = self.chain.get("available") is True
        if not self.chain_ok:
            self.unavailable("NODE_UNAVAILABLE", "Confirmed registry and proposal evidence is unavailable.")

    def bad(self, key, event=None, row=None, checkpoint=None):
        self.out[key] += 1
        if event is not None:
            self.refs.append(_event_ref(event))
        if row is not None:
            self.refs.append({"kind": "view_row", "ref": f"{row['view']}:{row['row_key']}@{self.k if checkpoint is None else checkpoint}"})

    def unavailable(self, reason, detail):
        self.missing.add(reason)
        self.missing_details.add(detail)

    def view(self, name, checkpoint=None):
        views = self.s.get("views", {}) if checkpoint is None else self.histories.get(checkpoint)
        rows = views.get(name) if isinstance(views, dict) else None
        if not isinstance(rows, list):
            self.unavailable("VIEW_UNAVAILABLE", f"Required {name} snapshot is unavailable.")
            return None
        return rows

    def registered_signature(self, org, digest, signature):
        """None = unavailable registry; False = proven signature/registry mismatch."""
        if not self.chain_ok:
            return None
        entry = self.chain.get("registry", {}).get(org)
        if not isinstance(entry, dict) or "signer" not in entry:
            self.unavailable("NODE_UNAVAILABLE", "A required organization registry getter is unavailable.")
            return None
        signer = entry["signer"]
        return signer != _ZERO_ADDRESS and signature.get("signer") == signer and verify_signature(digest, signature)

    def creation(self, config):
        created = [e for e in self.events if e["type"] == "ELECTION_CREATED"]
        if len(created) != 1:
            self.unavailable("OTHER", "Exactly one ELECTION_CREATED is required for R4.")
            return None
        event, p = created[0], created[0]["payload"]
        orgs = p["organizations"]
        org_ids = [o["org_id"] for o in orgs]
        if (org_ids != sorted(set(org_ids)) or p["operator_org"] not in org_ids
                or not 2 <= p["approval_threshold"] <= len(org_ids) - 1):
            self.bad("unapproved_changes", event)
        if (config["organizations"] != orgs or config["operator_org"] != p["operator_org"]
                or config["approval_threshold"] != p["approval_threshold"]):
            self.bad("unapproved_changes", event)
        if self.chain_ok:
            registry = self.chain.get("registry", {})
            for org in orgs:
                item = registry.get(org["org_id"])
                if not isinstance(item, dict) or "signer" not in item:
                    self.unavailable("NODE_UNAVAILABLE", "A required organization registry getter is unavailable.")
                elif item["signer"] == _ZERO_ADDRESS or org["signer"].lower() != item["signer"]:
                    self.bad("invalid_signatures", event)
            op = self.chain.get("operator")
            if not isinstance(op, dict) or not {"org_id", "confirmed_by_all"} <= op.keys():
                self.unavailable("NODE_UNAVAILABLE", "The operator registry getter is unavailable.")
            elif op.get("org_id") != H(p["operator_org"]) or op.get("confirmed_by_all") is not True:
                self.bad("unapproved_changes", event)
            gov = self.chain.get("governance")
            if not isinstance(gov, dict) or not {"org_ids", "operator_org_id", "approval_threshold"} <= gov.keys():
                self.unavailable("NODE_UNAVAILABLE", "Election governance is unavailable.")
            elif (gov.get("org_ids") != [H(x) for x in org_ids]
                  or gov.get("operator_org_id") != H(p["operator_org"])
                  or gov.get("approval_threshold") != p["approval_threshold"]):
                self.bad("unapproved_changes", event)
        return event

    def actions(self, created, rows):
        """Verify every accepted request/decision/review; return approved requests."""
        p = created["payload"]
        orgs = {o["org_id"] for o in p["organizations"]}
        threshold = p["approval_threshold"]
        actions = {a["row_key"]: a for a in rows}
        if len(actions) != len(rows):
            self.bad("target_mismatches", created)
        requests = defaultdict(list)
        decisions = defaultdict(list)
        closures = defaultdict(list)
        for e in self.events:
            ep = e["payload"]
            if e["type"] == "ADMIN_ACTION_REQUESTED":
                requests[ep["proposal_id"]].append(e)
            elif e["type"] == "APPROVAL_RECORDED":
                decisions[ep["proposal_id"]].append(e)
            elif e["type"] == "PROPOSAL_CLOSED":
                closures[ep["proposal_id"]].append(e)
        approved = {}
        for pid in requests.keys() | decisions.keys() | closures.keys():
            reqs = requests[pid]
            if len(reqs) != 1:
                self.bad("unapproved_changes", (reqs + decisions[pid] + closures[pid])[0])
                continue
            request = reqs[0]
            rp = request["payload"]
            row = actions.get(rp["action_id"])
            valid = True
            if row is None:
                self.unavailable("VIEW_UNAVAILABLE", "An administrative request has no target view row.")
                valid = False
            else:
                target = row["target"]
                recomputed = _checked_target_hash(self.s["election_id"], row)
                if (recomputed is None or recomputed != row["target_hash"]
                        or row["target_hash"] != rp["target_hash"] or row["proposal_id"] != pid
                        or row["action_type"] != rp["action_type"] or row["requested_by_org"] != rp["requested_by_org"]):
                    self.bad("target_mismatches", request, row)
                    valid = False
            digest = proposal_digest(pid, rp["action_id"], self.s["election_id"], rp["action_type"],
                                     rp["target_hash"], rp["requested_by_org"], "REQUEST")
            signature_ok = self.registered_signature(rp["requested_by_org"], digest, rp["signature"])
            if rp["requested_by_org"] not in orgs or rp["threshold"] != threshold or signature_ok is False:
                self.bad("invalid_signatures", request)
                valid = False
            if signature_ok is None:
                valid = False
            votes = set()
            seen = set()
            approved_event = None
            valid_approvals = []
            req_order = request["seq"]
            for decision in sorted(decisions[pid], key=lambda e: e["seq"]):
                dp = decision["payload"]
                org = dp["org_id"]
                digest = proposal_digest(pid, rp["action_id"], self.s["election_id"], rp["action_type"],
                                         rp["target_hash"], org, dp["decision"])
                sig = self.registered_signature(org, digest, dp["signature"])
                eligible = (org in orgs and org != rp["requested_by_org"] and org not in seen
                            and dp["action_id"] == rp["action_id"] and dp["threshold"] == threshold
                            and decision["seq"] > req_order
                            and _seconds(decision["occurred_at"]) <= _seconds(rp["expires_at"]))
                if not eligible or sig is False:
                    self.bad("invalid_signatures", decision)
                    valid = False
                elif sig is True and dp["decision"] == "APPROVE":
                    votes.add(org)
                    valid_approvals.append(decision)
                if sig is None:
                    valid = False
                seen.add(org)
                if eligible and sig is not None and dp["approvals_so_far"] != len(votes):
                    self.bad("invalid_signatures", decision)
                    valid = False
            for close in closures[pid]:
                if close["payload"]["action_id"] != rp["action_id"] or close["seq"] <= req_order:
                    self.bad("unapproved_changes", close)
                    valid = False
                if close["payload"]["outcome"] == "APPROVED":
                    qualifying = [d for d in valid_approvals if d["seq"] < close["seq"]]
                    if len(votes) < threshold or len(qualifying) < threshold or approved_event is not None:
                        self.bad("unapproved_changes", close)
                        valid = False
                    approved_event = close
            # A rejected/expired proposal, by itself, is expressly not a violation.
            if valid and approved_event is not None:
                approved[pid] = (request, approved_event)
            if self.chain_ok:
                state = self.chain.get("proposals", {}).get(pid)
                if not isinstance(state, dict) or not {"target_hash", "status", "approvals", "executed"} <= state.keys():
                    self.unavailable("NODE_UNAVAILABLE", "A required confirmed proposal getter is unavailable.")
                else:
                    if state.get("target_hash") != rp["target_hash"]:
                        self.bad("target_mismatches", request)
                    if approved_event is not None and (state.get("status") != "APPROVED" or state.get("approvals", -1) < threshold):
                        self.bad("unapproved_changes", approved_event)
        records = {e["payload"]["finding_id"]: e for e in self.events if e["type"] == "FINDING_RECORDED"}
        seen_reviews = set()
        for e in self.events:
            if e["type"] != "FINDING_REVIEWED":
                continue
            rp = e["payload"]
            identity = (rp["finding_id"], rp["org_id"])
            record = records.get(rp["finding_id"])
            digest = review_digest(rp["finding_id"], self.s["election_id"], rp["finding_hash"], rp["org_id"], rp["decision"], rp["note_hash"])
            sig = self.registered_signature(rp["org_id"], digest, rp["signature"])
            if (rp["org_id"] not in orgs or rp["org_id"] == p["operator_org"] or identity in seen_reviews
                    or sig is False or (record and (record["payload"]["finding_hash"] != rp["finding_hash"]
                        or record["seq"] >= e["seq"] or record["checkpoint"] > e["checkpoint"]))):
                self.bad("invalid_signatures", e)
            if record is None:
                self.unavailable("OTHER", "A reviewed finding's ledger record is unavailable.")
            seen_reviews.add(identity)
        return actions, approved

    def replay(self, config, created, actions, approved):
        executions = [e for e in self.core if e["type"] == "ADMIN_ACTION_EXECUTED"]
        self.out["executed_actions"] = len(executions)
        self.out["failed_actions"] = sum(e["type"] == "ADMIN_ACTION_FAILED" for e in self.core)
        counts = Counter(e["payload"]["proposal_id"] for e in executions)
        for pid, count in counts.items():
            if count > 1:
                self.bad("duplicate_executions", next(e for e in executions if e["payload"]["proposal_id"] == pid))
        active = set()
        expected_roster = {}
        token_holders = set()
        token_count = 0
        issued_counts = Counter()
        revoked = {}
        used = defaultdict(list)
        for event in self.events:
            if event["type"] == "TOKEN_USED":
                used[event["payload"]["token_hash"]].append(event)
        ever_tokens = {}
        frozen = False
        current_root = None
        effective_end = _seconds(created["payload"]["window_end"])
        processed_effects = set()
        early = []
        all_closes = []
        last_commit = next((e for e in self.checkpoints if e["event_id"] == self.s["checkpoint_event_id"]), None)
        if last_commit is None:
            self.unavailable("OTHER", "The selected CHECKPOINT_COMMITTED evidence is unavailable for R4.")
            return
        audit_time = _seconds(last_commit["occurred_at"])
        for index, event in enumerate(self.core):
            kind, ep, j = event["type"], event["payload"], event["checkpoint"]
            if kind == "ROSTER_FROZEN":
                rows = self.view("roster", j)
                if rows is None:
                    continue
                if frozen:
                    self.bad("roster_chain_breaks", event)
                expected_roster = {r["row_key"]: dict(r) for r in rows}
                active = {r["row_key"] for r in rows if r["status"] == "ACTIVE"}
                if len(expected_roster) != len(rows) or any(r["added_checkpoint"] > j for r in rows):
                    self.bad("roster_chain_breaks", event)
                if roster_hash(active) != ep["roster_hash"] or len(active) != ep["voter_count"]:
                    self.bad("roster_chain_breaks", event)
                current_root, frozen = ep["roster_hash"], True
            elif kind == "ADMIN_ACTION_EXECUTED":
                pid, aid, typ = ep["proposal_id"], ep["action_id"], ep["action_type"]
                row = actions.get(aid)
                if row is None:
                    self.unavailable("VIEW_UNAVAILABLE", "An executed action's target is unavailable.")
                    self.bad("target_mismatches", event)
                    continue
                approval = approved.get(pid)
                if approval is None:
                    # Missing node data alone is unknown, not a demonstrated invalid signature.
                    if "NODE_UNAVAILABLE" not in self.missing:
                        self.bad("unapproved_changes", event)
                    elif not any(e["type"] == "PROPOSAL_CLOSED" and e["payload"].get("proposal_id") == pid
                                 and e["payload"].get("outcome") == "APPROVED" for e in self.events):
                        self.bad("unapproved_changes", event)
                else:
                    request, close = approval
                    if (request["payload"]["action_id"] != aid or request["payload"]["target_hash"] != ep["target_hash"]
                            or close["checkpoint"] > j or _seconds(close["occurred_at"]) > _seconds(event["occurred_at"])):
                        self.bad("unapproved_changes", event)
                recomputed = _checked_target_hash(self.s["election_id"], row)
                if (row["proposal_id"] != pid or row["action_type"] != typ or row["target_hash"] != ep["target_hash"]
                        or recomputed != ep["target_hash"]):
                    self.bad("target_mismatches", event, row)
                if recomputed is None:
                    continue  # Proven malformed target; do not replace FAIL with a parser exception.
                if self.chain_ok:
                    state = self.chain.get("proposals", {}).get(pid)
                    if not isinstance(state, dict) or not {"status", "executed", "target_hash"} <= state.keys():
                        self.unavailable("NODE_UNAVAILABLE", "An executed proposal's confirmed state is unavailable.")
                    elif (state.get("status") != "APPROVED" or state.get("executed") is not True
                          or state.get("target_hash") != ep["target_hash"]):
                        self.bad("unapproved_changes", event)
                effects = []
                for nxt in self.core[index + 1:]:
                    np = nxt["payload"]
                    if nxt["type"] not in _EFFECTS or np.get("proposal_id") != pid or np.get("action_id") != aid:
                        break
                    effects.append(nxt)
                target = row["target"]
                if typ == "ELECTION_CLOSE_EARLY":
                    early.append((event, target))
                    if effects:
                        self.bad("target_mismatches", event)
                    continue
                expected_type = {"ROSTER_AMEND": "ROSTER_AMENDED", "WINDOW_CHANGE": "WINDOW_CHANGED",
                                 "ELECTION_VOID": "ELECTION_VOIDED"}.get(typ)
                if expected_type:
                    main = [e for e in effects if e["type"] == expected_type]
                    if len(main) != 1:
                        self.bad("missing_effects", event)
                allowed_types = {expected_type} if expected_type else set()
                expected_revocations = set()
                if typ == "ROSTER_AMEND":
                    adds, removes = set(target["add"]), set(target["remove"])
                    if (frozen and (adds & active or not removes <= active)) or adds & removes:
                        self.bad("target_mismatches", event)
                    expected_revocations = removes & token_holders
                    allowed_types.add("TOKEN_REVOKED")
                elif typ == "TOKEN_REISSUE":
                    expected_revocations = set(target["voter_keys"])
                    allowed_types.add("TOKEN_REVOKED")
                    if frozen and (not expected_revocations <= token_holders or not expected_revocations <= active):
                        self.bad("target_mismatches", event)
                actual_revocations = [e["payload"]["voter_key"] for e in effects if e["type"] == "TOKEN_REVOKED"]
                if (frozen or typ == "TOKEN_REISSUE") and Counter(actual_revocations) != Counter(expected_revocations):
                    self.bad("target_mismatches", event)
                    if expected_revocations - set(actual_revocations):
                        self.bad("missing_effects", event)
                for eff in effects:
                    if eff["type"] not in allowed_types:
                        self.bad("target_mismatches", eff)
                    else:
                        processed_effects.add(eff["event_id"])
            elif kind in _EFFECTS:
                if event["event_id"] not in processed_effects:
                    self.bad("unapproved_changes", event)
                row = actions.get(ep.get("action_id"))
                target = row["target"] if row and _checked_target_hash(self.s["election_id"], row) is not None else None
                if kind == "ROSTER_AMENDED":
                    if not frozen:
                        self.unavailable("VIEW_UNAVAILABLE", "The pre-amendment roster snapshot is unavailable.")
                    if current_root is not None and ep["roster_hash_before"] != current_root:
                        self.bad("roster_chain_breaks", event)
                    if target is None or row["action_type"] != "ROSTER_AMEND":
                        self.unavailable("VIEW_UNAVAILABLE", "A roster amendment's exact target is unavailable.")
                    else:
                        adds, removes = set(target["add"]), set(target["remove"])
                        active = (active - removes) | adds
                        for key in adds:
                            expected_roster[key] = {"view": "roster", "row_key": key, "status": "ACTIVE",
                                                    "added_checkpoint": j, "removed_checkpoint": None}
                        for key in removes:
                            if key in expected_roster:
                                expected_roster[key] = {**expected_roster[key], "status": "REMOVED", "removed_checkpoint": j}
                        if ((frozen and (roster_hash(active) != ep["roster_hash_after"] or len(active) != ep["voter_count"]))
                                or ep["added"] != len(adds) or ep["removed"] != len(removes)):
                            self.bad("roster_chain_breaks", event)
                    current_root = ep["roster_hash_after"]
                elif kind == "TOKEN_REVOKED":
                    key = ep["token_hash"]
                    if key in revoked or (frozen and ep["voter_key"] not in token_holders):
                        self.bad("target_mismatches", event)
                    if any(u["checkpoint"] <= j for u in used[key]):
                        self.bad("target_mismatches", event)
                    token_holders.discard(ep["voter_key"])
                    token_count -= 1
                    revoked[key] = event
                    if row and ep["reason"] != ("REISSUE" if row["action_type"] == "TOKEN_REISSUE" else "VOTER_REMOVED"):
                        self.bad("target_mismatches", event)
                elif kind == "WINDOW_CHANGED":
                    if (not target or row["action_type"] != "WINDOW_CHANGE" or ep["new_window_end"] != target.get("new_window_end")
                            or _seconds(ep["old_window_end"]) != effective_end):
                        self.bad("target_mismatches", event)
                    effective_end = _seconds(ep["new_window_end"])
                elif kind == "ELECTION_VOIDED" and (not target or row["action_type"] != "ELECTION_VOID"
                        or Counter(ep["finding_ids"]) != Counter(target.get("finding_ids", []))):
                    self.bad("target_mismatches", event)
            elif kind == "TOKEN_BATCH_ISSUED":
                issued_counts[j] += ep["token_count"]
                token_count += ep["token_count"]
                if frozen and (token_count != len(active) or ep["token_count"] != len(active - token_holders)):
                    self.bad("unapproved_changes", event)
                token_holders = set(active)
            elif kind == "ELECTION_CLOSED":
                if ep["reason"] == "EARLY":
                    matching = [(e, t) for e, t in early if e["payload"]["proposal_id"] == ep.get("proposal_id")
                                and e["payload"]["action_id"] == ep.get("action_id")]
                    if len(matching) != 1:
                        self.bad("unapproved_changes", event)
                    else:
                        ex, target = matching[0]
                        b = config["time_bucket_seconds"]
                        expected = max(_seconds(target["closed_at"]), ((_seconds(ex["occurred_at"]) + b - 1) // b) * b)
                        if ep["requested_closed_at"] != target["closed_at"] or _seconds(ep["closed_at"]) != expected:
                            self.bad("target_mismatches", event)
                        if any(c["payload"].get("proposal_id") == ep.get("proposal_id") for c in all_closes):
                            self.bad("target_mismatches", event)
                elif ep["reason"] == "ALL_VOTED":
                    b = config["time_bucket_seconds"]
                    if (_seconds(ep["closed_at"]) != (_seconds(event["occurred_at"]) // b) * b + b
                            or _seconds(ep["closed_at"]) >= effective_end):
                        self.bad("unapproved_changes", event)
                    finals = [c for c in self.checkpoints if c["payload"]["reason"] == "CLOSE_FINAL" and c["seq"] > event["seq"]]
                    if len(finals) != 1:
                        self.unavailable("OTHER", "ALL_VOTED requires one subsequent CLOSE_FINAL checkpoint.")
                    else:
                        final = finals[0]
                        counts = final["payload"]["counts"]
                        if (counts["tokens_active"] != counts["roster_active"] or counts["token_used"] != counts["tokens_active"]
                                or counts["ballots_received"] != counts["credentials_issued"]):
                            self.bad("unapproved_changes", event)
                        rv = self.view("roster", final["checkpoint"])
                        tv = self.view("token_status", final["checkpoint"])
                        cv = self.view("credential_issuance", final["checkpoint"])
                        bv = self.view("ballot_receipts", final["checkpoint"])
                        if all(v is not None for v in (rv, tv, cv, bv)):
                            live = [r for r in tv if r["revoked_checkpoint"] is None]
                            if (len(live) != sum(r["status"] == "ACTIVE" for r in rv)
                                    or sum(r["used_checkpoint"] is not None for r in live) != len(live)
                                    or len(bv) != len(cv)):
                                self.bad("unapproved_changes", event)
                all_closes.append(event)
                effective_end = min(effective_end, _seconds(ep["closed_at"]))
            elif kind == "CHECKPOINT_COMMITTED":
                rows, tokens = self.view("roster", j), self.view("token_status", j)
                if rows is not None and frozen:
                    actual = {r["row_key"]: r for r in rows}
                    if actual != expected_roster or len(actual) != len(rows):
                        self.bad("roster_chain_breaks", event)
                        self.refs.append({"kind": "snapshot", "ref": f"roster@{j}"})
                if tokens is not None:
                    actual = {r["row_key"]: r for r in tokens}
                    if len(actual) != len(tokens) or not ever_tokens.keys() <= actual.keys():
                        self.bad("unapproved_changes", event)
                    for key, row in actual.items():
                        previous = ever_tokens.get(key)
                        if previous and row["issued_checkpoint"] != previous["issued_checkpoint"]:
                            self.bad("unapproved_changes", event, row, j)
                        us = [u for u in used[key] if u["checkpoint"] <= j]
                        rv = revoked.get(key)
                        expected_used = min((u["checkpoint"] for u in us), default=None)
                        expected_revoked = rv["checkpoint"] if rv else None
                        status = "REVOKED" if rv else "USED" if us else "ISSUED"
                        if (row["used_checkpoint"] != expected_used or row["revoked_checkpoint"] != expected_revoked
                                or row["status"] != status):
                            self.bad("unapproved_changes", event, row, j)
                    by_issue = Counter(r["issued_checkpoint"] for r in tokens)
                    if by_issue != issued_counts or len([r for r in tokens if r["revoked_checkpoint"] is None]) != token_count:
                        self.bad("unapproved_changes", event)
                    for key, rv in revoked.items():
                        if key not in actual or actual[key]["issued_checkpoint"] > rv["checkpoint"]:
                            self.bad("unapproved_changes", rv)
                    if (frozen and token_count > len(active)) or token_count < 0:
                        self.bad("unapproved_changes", event)
                    ever_tokens = {key: dict(row) for key, row in actual.items()}
        for execution, target in early:
            b = config["time_bucket_seconds"]
            expected = max(_seconds(target["closed_at"]), ((_seconds(execution["occurred_at"]) + b - 1) // b) * b)
            matching = [c for c in all_closes if c["payload"].get("proposal_id") == execution["payload"]["proposal_id"]]
            closed_earlier = any(_seconds(c["payload"]["closed_at"]) < expected for c in all_closes)
            if audit_time >= expected + b and not matching and not closed_earlier:
                self.bad("missing_effects", execution)
        if not frozen:
            self.unavailable("OTHER", "The committed roster's ROSTER_FROZEN evidence is unavailable.")


def audit_admin(snapshot: dict, events: list[dict], rejections: list[dict], out: dict, refs: list) -> tuple[bool, str | None]:
    """Apply the R4 contract; independently demonstrated violations outrank gaps."""
    out.update(dict.fromkeys(_COUNTERS, 0))
    audit = _Audit(snapshot, events, out, refs)
    configs = audit.view("election_config")
    rows = audit.view("admin_actions")
    if not configs or len(configs) != 1 or rows is None:
        return False, "VIEW_UNAVAILABLE"
    created = audit.creation(configs[0])
    if created is not None:
        actions, approved = audit.actions(created, rows)
        audit.replay(configs[0], created, actions, approved)
    failed = any(out[k] for k in _FAILURES)
    if audit.missing_details:
        out["evidence_unavailable"] = sorted(audit.missing_details)
    if not audit.chain_ok and not out["invalid_signatures"]:
        out["invalid_signatures"] = None
    reason = next((r for r in ("NODE_UNAVAILABLE", "VIEW_UNAVAILABLE", "OTHER") if r in audit.missing), None)
    return failed, reason
