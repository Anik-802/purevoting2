# Basis of the T05 and T08 labels (written before any run, from the contract text only)

Source: contracts/README.md "Audit rules" (R1 to R5 and their `observed` keys), contracts/hashing.md (`snapshot_hash`),
core/app/views.py (roster rows are "as of k": `added_checkpoint <= k`), tests/scenarios/README.md and T01/setup.py (the
judge compares the set of non-zero numeric `observed` keys of each decided finding with `observed_nonzero`, exactly).
The labels were NOT derived from rules/ (Anik's code) or from any run. Every variant runs the T01 voting flow first
(3 voters vote), so the R3 count keys are the same as in T01.

## Conditions the setup (Sumin) must meet, otherwise the label does not apply

| Variant | Fault | Conditions |
|---|---|---|
| T05-api | roster PUT after freeze | Core answers 409; nothing is changed. |
| T05-db-add | one ACTIVE roster row inserted directly in the database after freeze | `added_checkpoint` = Core's open checkpoint at the time of the insert (the natural value). The insert happens before the judged checkpoint is committed. |
| T08a | one ballot_receipts row changed | the row was already part of a committed checkpoint; the changed field is `receipt_hash` (or `received_bucket`), NOT `ballot_hash` and NOT `sealed_ballot` |
| T08b | `sealed_ballot` bytes changed, `ballot_hash` kept | the row was already part of a committed checkpoint |
| T08c | `ballot_hash` and `sealed_ballot` changed together, so H(sealed_ballot) = ballot_hash still holds | the row was already part of a committed checkpoint |

If a tampering happens before the checkpoint that contains the row is committed, the snapshot hash covers the tampered
data and `tables_mismatched` is 0: that would be a different scenario.

## Reasoning per variant

- **T05-api:** the API refuses, so nothing reaches views or ledger. All rules PASS, `refused_by` core, no cause.
- **T05-db-add:** a silent insert emits no ROSTER_AMENDED event, so `unapproved_changes` stays 0 (it is event based).
  The roster no longer matches the ROSTER_FROZEN roster_hash: R4 "roster_hash chain broken" -> `roster_chain_breaks`,
  and R5 "recomputed roster_hash != latest ROSTER_* value" -> `root_mismatches`. The row is as of the open checkpoint,
  so no committed snapshot changes and `tables_mismatched` is 0. R3 stays PASS (`tokens_active > roster_active` is the
  only roster comparison and it does not hold when a voter is added).
- **T08a:** the committed snapshot of ballot_receipts no longer matches (`tables_mismatched`), and the row differs
  from its BALLOT_RECEIVED (`row_ledger_mismatches`). `ballot_hash` is untouched, so `sealed_hash_mismatches` is 0.
- **T08b:** the snapshot includes `sealed_ballot` (hashing.md), so `tables_mismatched` is non-zero; H(sealed) != ballot_hash
  gives `sealed_hash_mismatches`. The row tuple compared with the ledger does not contain `sealed_ballot`, so
  `row_ledger_mismatches` is 0. This differs from the docs/test-plan.md T08 row, which lists only
  `sealed_hash_mismatches`: the contract text gives both keys.
- **T08c:** like T08a (the row tuple contains `ballot_hash`); H(sealed) = ballot_hash holds, so `sealed_hash_mismatches` is 0.
- R1, R2 and R3 stay PASS everywhere: counts and ledger events are unchanged by a row edit or a roster insert.

## Cause
All tampering variants: DATA_TAMPERING, from the injection (direct database edit). For T05-db-add a rules-only
reading (R4 before R5, `roster_chain_breaks` -> UNAUTHORIZED_ADMIN_ACTION in the B1 table draft) would name another
cause; that is a legitimate B1 error and stays.

## Not labelled yet
- T05 with a voter removed by a direct database edit: the R4 key for "non-revoked tokens > ACTIVE roster rows" is not
  named in the contract, so it is labelled only after that is settled.
- If the setup of T05-db-add sets `added_checkpoint` to an already committed value, `tables_mismatched` becomes non-zero
  and that is a different variant (T05-db-add-old).

## Known gaps in the current rule engine (B1 will miss these; the labels do not change)
`rules/purevoting_rules/engine.py` sets `roster_chain_breaks` and `root_mismatches` to None (not implemented), so the
T05-db-add run is NOT JUDGED or fails until Anik implements them. This is read from the code only, not from a run.
