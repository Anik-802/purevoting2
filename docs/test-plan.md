# Test plan (v1.0, with contracts v1.0)

**Scope (D-01, system-design section 9):** mandatory T01, T02, T04, T05, T08, T15; the others are optional. The
experiment protocol below is the target; the MVP minimum is the mandatory scenarios × seeds 1–3 for B1 (and B2 if the
agent exists). Ground-truth labels: Harits (`tests/scenarios/README.md`). Shared hash vectors: `tests/vectors/hash-vectors.json`.

Status of every test: **not implemented / not run**. This file fixes *how* each required defect test (supervisor
list T01–T14, plus T15–T16 added for tally and review governance) will be executed and judged with the contracts, so that results can be reproduced and compared.

## Common setup

- Synthetic data only: 100 voters `voter-001..100`, 3 candidates, 3 organizations, voting window 2 h (09:00–11:00), `time_bucket_seconds = 900` (D-10 rule: 100 × 900 ÷ 7200 = 12.5 ≥ 10 expected voters per bucket),
  checkpoint every 900 s (= one bucket), anchor batch 60 s / 50 events. Random seed recorded per run.
- Run with `docker compose up` from a fresh clone; record commit hash, image versions, host hardware.
- Ground-truth labels for each injected fault are written **before** running the Rule Engine / AI, in
  `tests/scenarios/<id>/expected.json` (expected rule results and cause code).
- Baselines for the experiment: (B0) original PureVoting behaviour, (B1) rules only, (B2) rules + AI (a free local model,
  D-15 v1.2; run only if it go was confirmed on 2026-11-17; a template report without AI is not B2).
  Metrics: precision / recall / false-positive rate of FAIL, root-cause accuracy vs label (for B1 a fixed rule-ID → cause
  mapping table, written with the labels, gives its 'cause'), evidence validity rate,
  AI tokens and time to report (API cost is 0: no paid AI, D-15 v1.2). B2 evidence validity is measured offline by a
  read-only script (Core report storage and EVIDENCE_NOT_FOUND are future work, D-28).

## Defect tests

| ID | Scenario / injection | Contract elements used | Pass criterion |
|---|---|---|---|
| T01 | Normal vote: redeem → ballot → receipt → anchored | `credentials:redeem`, `ballots`, `public/receipts`, BALLOT_RECEIVED | 201 + receipt; receipt status becomes ANCHORED; R1–R5 PASS at next checkpoint; ELECTION_OPENED sent a few seconds early by the chain clock gets RETRY_LATER and is applied, never REJECTED |
| T02 | Reuse the same link | redeem 409 TOKEN_ALREADY_USED, `rejected_attempts` | refused; R1 PASS with `refused_duplicates ≥ 1` |
| T03 | Same credential sent twice concurrently and repeatedly (same and different ballot) | ballots 201/200/409, nullifier uniqueness | exactly one ballot stored and one BALLOT_RECEIVED sent; identical retry gets the same receipt; no Gateway rejection occurs (a Gateway DUPLICATE_CONTENT would mean Core sent a duplicate and must make R3 FAIL); R1 PASS, R3 PASS |
| T04 | Redeem/ballot before start and after end; ballot in the last bucket; a late voter event injected directly into the outbox (bypassing Core checks) | ELECTION_NOT_OPEN at Core, Gateway reject_code ELECTION_NOT_OPEN, R2 formula | Core refuses (rejected_attempts, R2 PASS with `refused_not_open`); the injected event is REJECTED by the Gateway, R2 FAIL with `ledger_rejected_window ≥ 1` and R3 FAIL (rejection without a matching view row); last-bucket ballot accepted and R2 PASS |
| T05 | Add/remove voter after freeze without approval (direct API and direct DB edit) | roster PUT 409, contract refusal of ROSTER_AMENDED, R4 roster chain, R5 snapshot | API refused; DB edit → R4 and/or R5 FAIL at next checkpoint |
| T06 | Legitimate correction: ROSTER_AMEND with 2 valid org signatures (requester excluded); variant (a) Core executes it with different voter_keys than approved (ghost voters); variant (b) executes the same proposal twice; variant (c) a TOKEN_REISSUE races with a redeem of the same token | admin-actions create/submit/decisions, organization registry, R4 set comparison, ALREADY_EXECUTED, execution-time re-check | legitimate: executed, R4 PASS; (a) R4 FAIL `target_mismatches`; (b) second execution REJECTED (ALREADY_EXECUTED) and, if injected, R4 FAIL `duplicate_executions`; (c) never both redeem and reissue, R1 PASS; every executed action's effect events directly follow its ADMIN_ACTION_EXECUTED in seq order |
| T07 | Forged / missing / self approval signature; replayed signature for another action; approval or review signed by a registered organization that does not take part in the election | SIGNATURE_INVALID, SELF_APPROVAL_NOT_ALLOWED, ORG_NOT_REGISTERED, digest includes action/target, participation check | refused; if injected into the ledger directly → R4 FAIL `invalid_signatures ≥ 1` (non-participating org counted as invalid); variant: the Gateway calls createElection with other orgIds or a lower threshold than ELECTION_CREATED while its own /state API reports the expected values → Core GOVERNANCE_MISMATCH alert from its direct node read, no ROSTER_FROZEN is delivered, freeze / tokens:issue / resume are refused (409) and the election can only be deleted at Core (delete 204); a freeze attempted while creation_check is still PENDING is refused (409); a node whose latest block is below block_number + D (D-27) causes a retry, not a mismatch; the Gateway reports ACCEPTED but the election is not on chain after the pinned node reached block_number + D → GOVERNANCE_MISMATCH (not found) and delete 204; if the check is bypassed, R5 FAIL `governance_mismatch` at the first checkpoint |
| T08 | (a) change one ballot_receipts row after its checkpoint was committed **and audited**; (b) change only the sealed_ballot bytes, keeping ballot_hash; (c) change ballot_hash and sealed_ballot together | R5 all-checkpoint snapshot re-check, row-to-ledger match, sealed-hash check, tally ledger set | (a)(c) R5 FAIL at the next run (`tables_mismatched` for the old checkpoint and row ≠ BALLOT_RECEIVED → `row_ledger_mismatches`); (b) R5 FAIL `sealed_hash_mismatches`; the tally counts only ledger-matched ballots and marks the changed one invalid |
| T09 | Delete one BALLOT_RECEIVED from the outbox before delivery (ledger short by one) | voter_events defined from views, delivery-order rule, R3 direct ledger-vs-view comparison, audit alerts | R3 FAIL and R5 FAIL (`row_ledger_mismatches`: row without its BALLOT_RECEIVED) at the first checkpoint whose CHECKPOINT_COMMITTED is accepted (`ledger_ballots` = view count − 1); before that the Rule Engine records an AUDIT_DELAYED alert and no finding |
| T10 | Stop the Gateway during voting for longer than several checkpoints, restart, then compare | outbox FIFO + retry, RETRY_LATER, seq, audit alerts, R3/R5, tally precondition | no ballot lost or duplicated; during the outage only LEDGER_UNAVAILABLE / AUDIT_DELAYED alerts (no findings); after catch-up R1–R5 PASS for the delayed checkpoints (R5 compares each with electionStateAtCheckpoint, so later chain progress does not matter); tally refused (OUTBOX_NOT_DRAINED) until drained; variant: the Gateway goes down after the Rule Engine read its inputs, so it submits the finding record directly on chain; after recovery Core answers 503 until the record is indexed, then stores the body, and finalization is possible; variant (v1.1, optional): while a system event's transaction is not yet confirmed the Gateway answers RETRY_LATER (Core retries every 1 s), and a transaction dropped by a fork is re-broadcast unchanged — exactly one effect on chain, no REJECTED |
| T11 | Make evidence unavailable while the checkpoint is in the ledger: (a) leave one voter batch unanchored, (b) make every validator node unreachable for the Rule Engine | inconclusive_reason, supersession | (a) R5 INCONCLUSIVE BATCH_NOT_ANCHORED with evidence, R1–R4 evaluated normally (PASS); (b) R4 and R5 INCONCLUSIVE NODE_UNAVAILABLE; after the nodes are back, a re-run of the same checkpoint PASSes and supersedes them; never PASS; finalize refused (FINDINGS_UNREVIEWED) until RESOLVED by signed reviews or SUPERSEDED by a later PASS |
| T12 | AI report citing a non-existent event; agentKey calling an internal write endpoint, a Gateway write endpoint and a staff endpoint; report with human_review | EVIDENCE_NOT_FOUND, permission matrix, AuditReportSubmission | 422; 403 (internal and Gateway writes); 401 (staff endpoint, no staff JWT); 400 |
| T13 | Try to link voter to ballot using only views, ledger and logs | threat model S1–S3, view and log schemas | no join key exists; time link limited to bucket/checkpoint; documented k per bucket |
| T14 | Fresh clone on another PC → full demo (normal vote + 2 fault scenarios) | README, docker-compose, CI | demo completes; steps and logs recorded |
| T15 | Tally manipulation: (a) omit one accepted ballot from the count, (b) change one candidate's total, (c) count a ballot with checkpoint > final, (d) set final_checkpoint one checkpoint earlier than CLOSE_FINAL to drop the last checkpoint's ballots consistently | TALLY_PUBLISHED fields, CLOSE_FINAL checkpoint, ballot_set_root, revealed private key, R3/R5 at TALLY | (a) R3 FAIL and R5 FAIL; (b) R5 FAIL (re-decryption); (c) R5 FAIL (ballot_set_root); (d) publishTally rejected by the contract and, if injected, R3/R5 FAIL (final_checkpoint ≠ CLOSE_FINAL); finalize refused until reviewed |
| T16 | Commission tries to clear a FAIL alone: (a) reviews DISMISSED as operator_org only, then finalizes; (b) creates the election with a staff account of another org to change operator_org; (c) does not relay another org's CONFIRMED_ISSUE | FINDING_REVIEWED, quorum excluding operator_org, registry operatorOrg, direct submitFindingReview | (a) finalize refused by Core and contract; (b) no effect — Core takes operator_org from the registry, not from the login; if Core is modified to send a different operator_org, the contract rejects ELECTION_CREATED; (c) the org submits directly on chain, the finding becomes CONFIRMED_ISSUE; finalize is refused by Core, or — if Core had not yet indexed the review — ELECTION_FINALIZED is rejected by the contract and Core stays TALLIED (FINALIZED is applied only after ACCEPTED) |

## Contract-level checks available now

`python tools/validate_contracts.py` validates schemas, OpenAPI files and examples (valid examples pass,
invalid examples — e.g. voter event with precise time, report with human_review, finding FAIL without evidence —
are rejected). This proves only the contracts, not any implementation.

## Demo timing

System events are released at the end of their time bucket, so with 900 s buckets each step of a live demo (freeze,
tally, finalize) can take up to 15 minutes to reach the ledger. Demo runs use a separate configuration with a short
bucket (e.g. 60 s) and a small synthetic roster sized to the D-10 rule, stated as a demo setting, not the evaluated one.

## Baselines and mocks

- **B0 (original PureVoting):** run the original system with the same synthetic roster for the scenarios it supports
  (normal vote, link/QR reuse, late submission); scenarios it cannot express are marked "not applicable" rather than failed.
- **Mocks for independent development:** each team runs `prism mock` (or equivalent) on `contracts/core-api.openapi.yaml`
  and `contracts/ledger-gateway.openapi.yaml`, and the Rule Engine uses `contracts/examples/` as schema fixtures (judged scenarios with expected labels live in `tests/scenarios/`), so no module
  waits for another before the first integration.

## Experiment protocol

- Scenarios (D-28): the 6 mandatory scenarios (T01, T02, T04, T05, T08, T15), seeds 1–3 first and 4–5 if time allows
  (fault timing and voter order vary by seed), in the demo configuration, scripted, 2026-11-27 – 11-30. Baseline B1;
  B2 only if the AI Audit Agent is built; B0 described qualitatively. The report states that the evaluation
  configuration and seeds 6–10 were not run. (Before D-28: T01–T16 plus 10 random fault mixes, seeds 1–10.)
- Ground-truth labels for a scenario are written by a team member who does not implement the Rule Engine or the Audit
  Agent (Seungmin or Harits), committed before the first run, and never edited after seeing results.
- Report per baseline: number of runs, failures and their causes, and all metrics with min / median / max over seeds.
