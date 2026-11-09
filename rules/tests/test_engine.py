from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
import yaml

from purevoting_rules.engine import OBSERVED_KEYS, evaluate_all, evaluate_rule
from purevoting_rules.findings import build_finding, validate_submission
from purevoting_rules.fixtures import _Factory, _rehash, development_cases
from purevoting_rules.hashing import H, Hc, target_hash
from purevoting_rules.paths import reference_root


class RuleEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = development_cases(2026)
        cls.by_id = {case["case_id"]: case for case in cls.cases}
        contract_dir = reference_root() / "contracts"
        registry = Registry()
        for path in sorted(contract_dir.iterdir()):
            if path.suffix not in {"json", ".json", ".yaml"}:
                continue
            source = path.read_text(encoding="utf-8")
            document = json.loads(source) if path.suffix == ".json" else yaml.safe_load(source)
            resource = Resource.from_contents(document, default_specification=DRAFT202012)
            registry = registry.with_resource(path.resolve().as_uri(), resource)
            registry = registry.with_resource("https://purevoting2.local/contracts/" + path.name, resource)
            if document.get("$id"):
                registry = registry.with_resource(document["$id"], resource)
        cls.registry = registry
        cls.contract_dir = contract_dir

    def snapshot(self, case_id="clean_cumulative"):
        return deepcopy(self.by_id[case_id]["snapshot"])

    def validator(self, file, fragment=""):
        return Draft202012Validator({"$ref": (self.contract_dir / file).resolve().as_uri() + fragment}, registry=self.registry, format_checker=FormatChecker())

    def test_all_hand_labelled_development_cases(self):
        self.assertGreaterEqual(len(self.cases), 10)
        for case in self.cases:
            with self.subTest(case=case["case_id"]):
                actual = {outcome.rule_id: outcome.result for outcome in evaluate_all(case["snapshot"])}
                self.assertEqual(case["expected"], actual)

    def test_fixture_schema_validation(self):
        """Validate real frozen shapes, not a home-made lenient fixture schema."""
        event_validator = self.validator("ledger-events.schema.json")
        rejection_validator = self.validator("ledger-gateway.openapi.yaml", "#/components/schemas/Rejection")
        row_schemas = {"election_config": "RowElectionConfig", "roster": "RowRoster", "token_status": "RowTokenStatus",
                       "credential_issuance": "RowCredentialIssuance", "ballot_receipts": "RowBallotReceipt",
                       "admin_actions": "RowAdminAction", "rejected_attempts": "RowRejectedAttempt"}
        row_validators = {name: self.validator("core-api.openapi.yaml", f"#/components/schemas/{definition}") for name, definition in row_schemas.items()}
        for case in self.cases:
            snapshot = case["snapshot"]
            with self.subTest(case=case["case_id"]):
                for event in snapshot["events"]:
                    event_validator.validate(event)
                for rejection in snapshot["rejections"]:
                    rejection_validator.validate(rejection)
                all_views = [snapshot["views"]] + [h["views"] for h in snapshot["history"].values()]
                for views in all_views:
                    for name, rows in views.items():
                        for row in rows:
                            row_validators[name].validate(row)

    def test_outcomes_use_required_observed_keys_and_frozen_evidence_refs(self):
        evidence_validator = self.validator("common.schema.json", "#/$defs/EvidenceRef")
        value_validator = self.validator("common.schema.json", "#/$defs/NoFloat")
        for case in self.cases:
            for outcome in evaluate_all(case["snapshot"]):
                with self.subTest(case=case["case_id"], rule=outcome.rule_id):
                    self.assertTrue(set(OBSERVED_KEYS[outcome.rule_id]) <= outcome.observed.keys())
                    value_validator.validate(outcome.observed)
                    self.assertTrue(outcome.evidence_refs)
                    for ref in outcome.evidence_refs:
                        evidence_validator.validate(ref)
                    if outcome.rule_id in {"R4", "R5"}:
                        self.assertEqual(outcome.coverage, "partial")
                        self.assertTrue(outcome.unimplemented_checks)
                        self.assertNotEqual(outcome.result, "PASS")

    def test_repeatability_and_input_immutability(self):
        snapshot = self.snapshot()
        before = deepcopy(snapshot)
        self.assertEqual(evaluate_all(snapshot), evaluate_all(snapshot))
        self.assertEqual(snapshot, before)
        self.assertEqual(development_cases(2026), development_cases(2026))

    def test_missing_required_evidence_never_passes(self):
        dependencies = {"R1": ["token_status", "credential_issuance", "rejected_attempts"],
                        "R2": ["election_config", "admin_actions", "rejected_attempts"],
                        "R3": ["roster", "token_status", "credential_issuance", "ballot_receipts", "admin_actions", "rejected_attempts"],
                        "R4": ["admin_actions", "election_config"],
                        "R5": ["roster", "token_status", "credential_issuance", "ballot_receipts", "admin_actions", "rejected_attempts", "election_config"]}
        for rule, tables in dependencies.items():
            for table in tables:
                with self.subTest(rule=rule, table=table):
                    snapshot = self.snapshot()
                    del snapshot["views"][table]
                    result = evaluate_rule(rule, snapshot)
                    self.assertEqual(result.result, "INCONCLUSIVE")
                    self.assertEqual(result.inconclusive_reason, "VIEW_UNAVAILABLE")
        for key in ("events", "rejections"):
            snapshot = self.snapshot()
            del snapshot[key]
            self.assertTrue(all(result.result == "INCONCLUSIVE" for result in evaluate_all(snapshot)))

    def test_duplicate_transport_copy_is_not_second_accepted_event(self):
        snapshot = self.snapshot()
        snapshot["events"].append(deepcopy(next(e for e in snapshot["events"] if e["type"] == "TOKEN_USED")))
        self.assertEqual(evaluate_rule("R1", snapshot).result, "PASS")
        self.assertEqual(evaluate_rule("R3", snapshot).result, "PASS")
        snapshot["events"][-1]["payload"]["token_hash"] = H("conflicting-copy")
        self.assertEqual(evaluate_rule("R1", snapshot).result, "INCONCLUSIVE")

    def test_refused_gateway_duplicate_is_not_an_accepted_duplicate(self):
        snapshot = self.snapshot()
        factory = _Factory(81)
        factory.election_id = snapshot["election_id"]
        token = snapshot["views"]["token_status"][0]["row_key"]
        snapshot["rejections"].append(factory.rejection("TOKEN_USED", 2, "TOKEN_ALREADY_USED", token, {"token_hash": token}))
        outcome = evaluate_rule("R1", snapshot)
        self.assertEqual(outcome.result, "PASS")
        self.assertEqual(outcome.observed["refused_duplicates"], 1)
        self.assertEqual(outcome.observed["accepted_duplicate_tokens"], 0)

    def test_unknown_ballot_credential_detected_even_when_totals_match(self):
        snapshot = self.snapshot()
        event = next(e for e in snapshot["events"] if e["type"] == "BALLOT_RECEIVED")
        event["payload"]["nullifier_hash"] = H("unknown-credential")
        _rehash(event)
        self.assertEqual(evaluate_rule("R1", snapshot).observed["unknown_nullifiers"], 1)
        self.assertEqual(evaluate_rule("R1", snapshot).result, "FAIL")

    def test_revocation_of_used_token_fails_in_either_list_order(self):
        snapshot = self.snapshot()
        used = next(e for e in snapshot["events"] if e["type"] == "TOKEN_USED")
        factory = _Factory(83)
        factory.election_id = snapshot["election_id"]
        revoke = factory.event("TOKEN_REVOKED", 2, "2026-11-03T09:05:00Z", {"token_hash": used["payload"]["token_hash"]}, 8)
        for position in (0, len(snapshot["events"])):
            candidate = deepcopy(snapshot)
            candidate["events"].insert(position, revoke)
            self.assertEqual(evaluate_rule("R1", candidate).result, "FAIL")

    def test_window_checks_whole_bucket_and_inclusive_end(self):
        for at, expected in (("2026-11-03T09:00:00Z", "PASS"), ("2026-11-03T09:15:00Z", "PASS"),
                             ("2026-11-03T09:30:00Z", "FAIL"), ("2026-11-03T08:45:00Z", "FAIL")):
            with self.subTest(at=at):
                snapshot = self.snapshot()
                event = next(e for e in snapshot["events"] if e["type"] == "BALLOT_RECEIVED")
                event["occurred_at"] = at
                _rehash(event)
                self.assertEqual(evaluate_rule("R2", snapshot).result, expected)

    def test_last_accepted_window_change_and_rejected_extension(self):
        snapshot = self.snapshot("late_accepted_ballot")
        factory = _Factory(84)
        factory.election_id = snapshot["election_id"]
        payload = {"action_id": factory.identity("act"), "proposal_id": factory.identity("prp"),
                   "old_window_end": "2026-11-03T09:30:00Z", "new_window_end": "2026-11-03T10:00:00Z"}
        snapshot["rejections"].append(factory.rejection("WINDOW_CHANGED", 2, "ELECTION_NOT_OPEN", None, payload))
        self.assertEqual(evaluate_rule("R2", snapshot).result, "FAIL")
        snapshot["events"].append(factory.event("WINDOW_CHANGED", 2, "2026-11-03T09:05:00Z", payload, 8))
        self.assertEqual(evaluate_rule("R2", snapshot).result, "PASS")
        payload = {**payload, "old_window_end": "2026-11-03T10:00:00Z", "new_window_end": "2026-11-03T09:30:00Z"}
        snapshot["events"].append(factory.event("WINDOW_CHANGED", 2, "2026-11-03T09:10:00Z", payload, 9))
        self.assertEqual(evaluate_rule("R2", snapshot).result, "FAIL")

    def test_executed_early_close_caps_window_before_scheduler_event(self):
        snapshot = self.snapshot()
        factory = _Factory(85)
        factory.election_id = snapshot["election_id"]
        action_id, proposal_id = factory.identity("act"), factory.identity("prp")
        row = {"view": "admin_actions", "row_key": action_id, "action_type": "ELECTION_CLOSE_EARLY", "status": "EXECUTED",
               "requested_by_org": "org_commission", "proposal_id": proposal_id, "target": {"closed_at": "2026-11-03T09:00:00Z"},
               "requested_checkpoint": 2, "status_checkpoint": 2}
        row["target_hash"] = target_hash({"election_id": snapshot["election_id"], "action_id": action_id, "action_type": row["action_type"], "target": row["target"]})
        snapshot["views"]["admin_actions"].append(row)
        snapshot["events"].append(factory.event("ADMIN_ACTION_EXECUTED", 2, "2026-11-03T09:01:00Z", {"action_id": action_id,
                                  "proposal_id": proposal_id, "action_type": row["action_type"], "target_hash": row["target_hash"]}, 8))
        # ceil(09:01) = 09:15. The initial 09:00 bucket is still permissible.
        self.assertEqual(evaluate_rule("R2", snapshot).result, "PASS")
        voter = next(e for e in snapshot["events"] if e["type"] == "TOKEN_USED")
        voter["occurred_at"] = "2026-11-03T09:15:00Z"
        self.assertEqual(evaluate_rule("R2", snapshot).result, "FAIL")
        snapshot["views"]["admin_actions"].clear()
        self.assertEqual(evaluate_rule("R2", snapshot).result, "INCONCLUSIVE")

    def test_rejected_voter_event_after_close_final_is_failure(self):
        snapshot = self.snapshot("accepted_after_close_final")
        snapshot["events"] = [e for e in snapshot["events"] if not (e["type"] == "TOKEN_USED" and e["checkpoint"] == 4)]
        factory = _Factory(86)
        factory.election_id = snapshot["election_id"]
        snapshot["rejections"].append(factory.rejection("TOKEN_USED", 4, "TOKEN_ALREADY_USED", H("test-token"), {"token_hash": H("test-token")}))
        outcome = evaluate_rule("R2", snapshot)
        self.assertEqual(outcome.result, "FAIL")
        self.assertEqual(outcome.observed["events_after_close_final"], 1)

    def test_count_benign_gap_and_live_token_definition(self):
        outcome = evaluate_rule("R3", self.snapshot())
        self.assertEqual(outcome.observed["redeemed_not_voted"], 1)
        self.assertEqual(outcome.observed["tokens_active"], 20)  # includes USED
        self.assertEqual(outcome.result, "PASS")

    def test_count_checks_historical_voter_counts_not_only_current_commit(self):
        snapshot = self.snapshot()
        commit = next(e for e in snapshot["events"] if e["type"] == "CHECKPOINT_COMMITTED" and e["checkpoint"] == 0)
        commit["payload"]["voter_events"]["TOKEN_USED"] = 1
        _rehash(commit)
        outcome = evaluate_rule("R3", snapshot)
        self.assertEqual(outcome.result, "FAIL")
        self.assertEqual(outcome.observed["checkpoint_voter_event_mismatches"], 1)

    def test_omitted_known_historical_commit_cannot_hide_a_violation_with_pass(self):
        snapshot = self.snapshot()
        commit = next(e for e in snapshot["events"] if e["type"] == "CHECKPOINT_COMMITTED" and e["checkpoint"] == 0)
        commit["payload"]["voter_events"]["TOKEN_USED"] = 1
        _rehash(commit)
        self.assertEqual(evaluate_rule("R3", snapshot).result, "FAIL")
        snapshot["events"].remove(commit)
        outcome = evaluate_rule("R3", snapshot)
        self.assertEqual(outcome.result, "INCONCLUSIVE")
        self.assertEqual(outcome.inconclusive_reason, "OTHER")
        self.assertIn("historical CHECKPOINT_COMMITTED", outcome.observed["input_boundary_issues"][0])

    def test_rejected_historical_checkpoint_does_not_require_an_accepted_copy(self):
        snapshot = self.snapshot()
        commit = next(e for e in snapshot["events"] if e["type"] == "CHECKPOINT_COMMITTED" and e["checkpoint"] == 0)
        snapshot["events"].remove(commit)
        factory = _Factory(89)
        factory.election_id = snapshot["election_id"]
        rejected = factory.rejection("CHECKPOINT_COMMITTED", 0, "INVALID_STATE_TRANSITION", None, commit["payload"])
        rejected["event_id"] = commit["event_id"]
        snapshot["rejections"].append(rejected)
        # R5 reports the rejected checkpoint; no generic contiguous-index rule
        # changes R1's independent duplicate/state predicates.
        self.assertEqual(evaluate_rule("R1", snapshot).result, "PASS")
        self.assertEqual(evaluate_rule("R5", snapshot).result, "FAIL")

    def test_future_use_cannot_be_counted_as_clean_current_checkpoint_evidence(self):
        snapshot = self.snapshot()
        snapshot["views"]["token_status"][0]["used_checkpoint"] = snapshot["checkpoint"] + 1
        commit = next(e for e in snapshot["events"] if e["event_id"] == snapshot["checkpoint_event_id"])
        commit["payload"]["voter_events"]["TOKEN_USED"] = 2
        _rehash(commit)
        outcome = evaluate_rule("R3", snapshot)
        self.assertEqual(outcome.result, "INCONCLUSIVE")
        self.assertEqual(outcome.inconclusive_reason, "VIEW_UNAVAILABLE")
        self.assertIn("token_status.used_checkpoint", outcome.observed["input_boundary_issues"][0])

    def test_future_credential_fields_reject_only_rules_that_read_that_view(self):
        snapshot = self.snapshot()
        snapshot["views"]["credential_issuance"][0]["issued_checkpoint"] = snapshot["checkpoint"] + 1
        for rule in ("R1", "R3"):
            with self.subTest(rule=rule):
                outcome = evaluate_rule(rule, snapshot)
                self.assertEqual(outcome.result, "INCONCLUSIVE")
                self.assertEqual(outcome.inconclusive_reason, "VIEW_UNAVAILABLE")
        self.assertEqual(evaluate_rule("R2", snapshot).result, "PASS")

    def test_input_boundary_issue_does_not_hide_a_proven_accepted_duplicate(self):
        snapshot = self.snapshot("accepted_duplicate_token")
        snapshot["views"]["credential_issuance"][0]["issued_checkpoint"] = snapshot["checkpoint"] + 1
        outcome = evaluate_rule("R1", snapshot)
        self.assertEqual(outcome.result, "FAIL")
        self.assertEqual(outcome.observed["accepted_duplicate_tokens"], 1)
        self.assertTrue(outcome.observed["input_boundary_issues"])

    def test_future_cached_history_is_outside_the_selected_run(self):
        snapshot = self.snapshot()
        later = deepcopy(snapshot["history"]["2"])
        later["checkpoint_event"]["checkpoint"] = 3
        later["checkpoint_event"]["event_id"] = _Factory(90).identity("evt")
        snapshot["history"]["3"] = later
        self.assertEqual(evaluate_rule("R1", snapshot).result, "PASS")

    def test_multiple_input_issues_build_bounded_contract_valid_findings(self):
        snapshot = self.snapshot()
        snapshot["views"]["token_status"][0]["used_checkpoint"] = 3
        snapshot["views"]["credential_issuance"][0]["issued_checkpoint"] = 3
        factory = _Factory(91)
        row = {"view": "admin_actions", "row_key": factory.identity("act"), "action_type": "WINDOW_CHANGE",
               "status": "EXECUTED", "requested_by_org": "org_commission", "proposal_id": factory.identity("prp"),
               "target": {"new_window_end": "2026-11-03T09:45:00Z"}, "requested_checkpoint": 3, "status_checkpoint": 3}
        row["target_hash"] = target_hash({"election_id": snapshot["election_id"], "action_id": row["row_key"],
                                          "action_type": row["action_type"], "target": row["target"]})
        snapshot["views"]["admin_actions"].append(row)
        for outcome in evaluate_all(snapshot):
            with self.subTest(rule=outcome.rule_id):
                issues = outcome.observed["input_boundary_issues"]
                self.assertGreaterEqual(len(issues), 2)
                # Issue strings identify fields once, never individual voters or
                # their raw keys. Their number is bounded by the schema fields.
                self.assertEqual(len(issues), len(set(issues)))
                self.assertLessEqual(len(issues), 14)
                issue_text = json.dumps(issues)
                for rows in snapshot["views"].values():
                    for view_row in rows:
                        if len(view_row["row_key"]) > 20:
                            self.assertNotIn(view_row["row_key"], issue_text)
                body = build_finding(outcome, snapshot, checked_at="2026-11-03T09:20:00Z")
                validate_submission(body)
                self.assertLessEqual(len(body["summary"]), 1000)
                self.assertNotEqual(body["result"], "PASS")
                if outcome.coverage == "partial":
                    for check in outcome.unimplemented_checks:
                        self.assertIn(check, body["summary"])

    def test_count_includes_gateway_rejections_per_voter_event_type(self):
        for kind in ("TOKEN_USED", "BALLOT_RECEIVED"):
            with self.subTest(kind=kind):
                snapshot = self.snapshot()
                event = next(e for e in snapshot["events"] if e["type"] == kind)
                snapshot["events"].remove(event)
                factory = _Factory(87)
                factory.election_id = snapshot["election_id"]
                subject = event["payload"].get("token_hash", event["payload"].get("nullifier_hash"))
                snapshot["rejections"].append(factory.rejection(kind, 2, "ELECTION_NOT_OPEN", subject, event["payload"]))
                self.assertEqual(evaluate_rule("R3", snapshot).result, "PASS")
                snapshot["rejections"].clear()
                self.assertEqual(evaluate_rule("R3", snapshot).result, "FAIL")

    def test_count_constraints_roster_and_credential_limits(self):
        snapshot = self.snapshot()
        snapshot["views"]["roster"].pop()
        outcome = evaluate_rule("R3", snapshot)
        self.assertIn("tokens_within_roster", outcome.observed["count_relationship_violations"])
        snapshot["views"]["roster"] = snapshot["views"]["roster"][:2]
        self.assertIn("credentials_within_roster", evaluate_rule("R3", snapshot).observed["count_relationship_violations"])
        snapshot = self.snapshot()
        snapshot["views"]["credential_issuance"] = []
        self.assertIn("ballots_within_credentials", evaluate_rule("R3", snapshot).observed["count_relationship_violations"])

    def test_tally_final_checkpoint_and_counts(self):
        snapshot = self.snapshot("tally_count_mismatch")
        tally = next(e for e in snapshot["events"] if e["type"] == "TALLY_PUBLISHED")
        tally["payload"]["ballot_count"] = 2
        _rehash(tally)
        self.assertEqual(evaluate_rule("R3", snapshot).result, "PASS")
        self.assertEqual(evaluate_rule("R5", snapshot).inconclusive_reason, "KEY_NOT_REVEALED")
        tally["payload"]["excluded_ballots"] = 1
        self.assertEqual(evaluate_rule("R3", snapshot).result, "FAIL")
        tally["payload"]["excluded_ballots"] = 0
        tally["payload"]["final_checkpoint"] = 2
        self.assertEqual(evaluate_rule("R3", snapshot).result, "FAIL")

    def test_r4_duplicate_execution_and_target_absence_are_demonstrated(self):
        snapshot = self.snapshot()
        factory = _Factory(88)
        factory.election_id = snapshot["election_id"]
        payload = {"action_id": factory.identity("act"), "proposal_id": factory.identity("prp"), "action_type": "WINDOW_CHANGE", "target_hash": H("missing-target")}
        snapshot["events"].extend([factory.event("ADMIN_ACTION_EXECUTED", 2, "2026-11-03T09:05:00Z", payload, 8),
                                    factory.event("ADMIN_ACTION_EXECUTED", 2, "2026-11-03T09:06:00Z", payload, 9)])
        outcome = evaluate_rule("R4", snapshot)
        self.assertEqual(outcome.result, "FAIL")
        self.assertEqual(outcome.observed["duplicate_executions"], 1)
        self.assertEqual(outcome.observed["target_mismatches"], 2)
        self.assertIsNone(outcome.observed["invalid_signatures"])

    def test_historical_tampering_detected_despite_clean_current_view(self):
        snapshot = self.snapshot()
        snapshot["history"]["0"]["views"]["roster"][0]["status"] = "REMOVED"
        outcome = evaluate_rule("R5", snapshot)
        self.assertEqual(outcome.result, "FAIL")
        self.assertEqual(outcome.observed["tables_mismatched"], 1)
        self.assertIn({"kind": "snapshot", "ref": "roster@0"}, outcome.evidence_refs)

    def test_r5_matches_ballot_receipt_hash_and_reveals_unknown_node_checks(self):
        snapshot = self.snapshot()
        snapshot["views"]["ballot_receipts"][0]["receipt_hash"] = H("changed-receipt")
        outcome = evaluate_rule("R5", snapshot)
        self.assertEqual(outcome.result, "FAIL")
        self.assertEqual(outcome.observed["row_ledger_mismatches"], 1)
        self.assertIsNone(outcome.observed["anchors_mismatch"])
        self.assertIsNone(outcome.observed["governance_mismatch"])

    def test_known_local_failure_is_not_hidden_by_node_outage(self):
        snapshot = self.snapshot("tampered_sealed_ballot")
        snapshot["node"]["available"] = False
        outcome = evaluate_rule("R5", snapshot)
        self.assertEqual(outcome.result, "FAIL")
        self.assertGreater(outcome.observed["sealed_hash_mismatches"], 0)

    def test_r5_unavailable_and_pending_reasons(self):
        expected = {"node_unavailable": "NODE_UNAVAILABLE", "missing_historical_view": "VIEW_UNAVAILABLE", "pending_anchor": "BATCH_NOT_ANCHORED", "clean_cumulative": "OTHER"}
        for case, reason in expected.items():
            with self.subTest(case=case):
                result = evaluate_rule("R5", self.snapshot(case))
                self.assertEqual(result.result, "INCONCLUSIVE")
                self.assertEqual(result.inconclusive_reason, reason)


if __name__ == "__main__":
    unittest.main()
