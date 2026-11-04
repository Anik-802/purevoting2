# Role design — Rule Engine and AI Audit Agent (Anik)

> Part of the PureVoting 2.0 design. Overall picture: `docs/system-design.md`. Source of truth for every rule below:
> `contracts/` v1.0 — if this page and a contract disagree, the contract wins. Folder: `rules/`.

## 1. Mission and boundaries

You own the audit:

- the **Rule Engine**, which evaluates rules R1–R5 at every committed checkpoint and records each result (finding)
  on the ledger, signed with its own key;
- the **AI Audit Agent** (optional feature), which collects evidence with read-only tools and writes a report for
  FAIL / INCONCLUSIVE findings.

You do **not** own: changing anything in the election, approving actions or reviewing findings (organizations do that),
or accessing the database (views only). Neither the Rule Engine nor the AI can delete ballots, stop an election or change
results.

**You decide:** which free local model and prompt (D-15 v1.2: zero cost — no paid API, no cloud tier; fallback a template report). Constraints from the contracts:
10 s per tool call, at most 20 steps, then `ABORTED`; the agent never receives the election private key.

## 2. Read first

| File | Sections |
|---|---|
| `contracts/README.md` | **Audit rules** (the R1–R5 table is the specification), Checkpoints, Evidence references, Permission matrix |
| `contracts/finding.schema.json` | FindingBody, FindingSubmission, Finding (review, evidence_check) |
| `contracts/hashing.md` | all values used by R5 (snapshot_hash, roster_hash, token_set_root, merkle_root, rejections_root, config_hash, result_hash, ballot_set_root) and the finding record digest |
| `contracts/chain-interface.md` | Pinned chain configuration, Contract state derived from ledger events, How R5 uses it |
| `contracts/core-api.openapi.yaml` | `/internal/v1/*` (elections, checkpoints, views, findings, audit-alerts, audit-run-requests, audit-reports) |
| `contracts/ledger-gateway.openapi.yaml` | `GET /v1/events` (`checkpoint_max`), `/v1/rejections`, `/v1/anchors/{id}/leaves`, `POST /v1/finding-records` |
| `contracts/audit-report.schema.json` | (AI) report format and the complete agent tool list |

## 3. Interfaces

**You consume**

| Interface | Key | Use |
|---|---|---|
| Core `GET /internal/v1/elections`, `/internal/v1/checkpoints` | `ruleKey` | discover elections and newly committed checkpoints (poll, e.g. every 60 s) |
| Core `GET /internal/v1/views/{view}?checkpoint=k` (+ `/rows/{row_key}`) | `ruleKey` | the seven views, as of k |
| Gateway `GET /v1/events?checkpoint_max=k`, `/v1/rejections`, `/v1/anchors*`, `/v1/finding-records` | `ruleKey` | ledger side of every rule |
| Pinned validator node (chain-interface getters) | — | anchors (`anchorAt`), registry, `electionState`, `electionGovernance`, `electionStateAtCheckpoint` |
| Gateway `POST /v1/finding-records` | `ruleKey` | register a finding (hash + signature) |
| Core `POST /internal/v1/findings` | `ruleKey` | store the finding body |
| Core `POST /internal/v1/audit-alerts` | `ruleKey` | AUDIT_DELAYED, LEDGER_UNAVAILABLE, CHAIN_CONFIG_MISMATCH |
| Core `/internal/v1/audit-run-requests*` | `ruleKey` | manual re-runs requested by staff (optional) |
| (AI) Core `/internal/v1/*` read + `POST /internal/v1/audit-reports` | `agentKey` | read-only tools + submit report |

**You provide:** nothing that others call. The Rule Engine's signer key is registered on chain as `ruleEngineSigner` by
the observer org (`org_observer`, D-25 v1.1; the registry setup script's default).

## 4. Mandatory features (Rule Engine)

1. **Discovery.** Poll the election list and the checkpoint list. A run is one rule × one election × one committed checkpoint k.
2. **When to run.** For checkpoint k, run once both of these hold:
   - CHECKPOINT_COMMITTED k is ACCEPTED at the Gateway;
   - every event with checkpoint ≤ k is anchored.

   At the latest, run 2 × batch interval + 60 s after the commit was accepted (with a 60 s batch, that is 180 s). Unanchored events then give INCONCLUSIVE `BATCH_NOT_ANCHORED`. That concerns only the checks that need anchors (R5); the other rules evaluate the stored events.

   Before that point, or if the Gateway is unreachable before you have read your inputs: produce **no finding**, raise an alert instead (AUDIT_DELAYED / LEDGER_UNAVAILABLE).
3. **Inputs for k.** Every rule at k evaluates the **cumulative** data of all checkpoints ≤ k. Read:
   - views with `?checkpoint=k`; R5 also reads every committed checkpoint ≤ k;
   - ledger events with `checkpoint_max=k`;
   - rejections with checkpoint ≤ k;
   - chain values from the pinned node — counted only when the node's latest block ≥ the item's block + `confirmation_depth` D (D-27); otherwise treat the item as not anchored / not available yet, never as a mismatch.
4. **Rules R1–R5** exactly as in the README table: FAIL conditions, "not a FAIL" cases and the `observed` keys.
   - R1: duplicates and state.
   - R2: time — effective end = last accepted WINDOW_CHANGED, capped by an earlier close.
   - R3: counts — including the ledger-vs-view counts and the TALLY checks.
   - R4: admin actions — approval signatures from participating orgs, set comparison of effects, roster hash chain, `missing_effects`.
   - R5: integrity:
     - snapshot hashes of every checkpoint ≤ k;
     - row-level match with the ledger;
     - sealed-ballot hash;
     - roots;
     - anchors read from the pinned node;
     - at TALLY: `ballot_set_root`, re-decryption with the revealed key, `result_hash`;
     - `electionStateAtCheckpoint`;
     - `governance_mismatch`;
     - checkpoint cadence.
5. **Missing evidence is never PASS.** INCONCLUSIVE reasons: `BATCH_NOT_ANCHORED`, `NODE_UNAVAILABLE`, `VIEW_UNAVAILABLE`, `KEY_NOT_REVEALED`.
6. **Recording a finding.** Follow these steps in this order:
   1. Build the FindingSubmission and validate it against the schema locally.
   2. Store it durably. Keep it until the election is finalized or voided.
   3. Compute `finding_hash = Hc(FindingSubmission)`.
   4. Sign the finding record digest (hashing.md) with the Rule Engine key.
   5. Register it with `POST /v1/finding-records`.
   6. Store the body with Core `POST /internal/v1/findings`. On 503, retry the same body.

   (Optional, with T10) If the Gateway goes down **after** you read the inputs, submit `submitFindingRecord` directly on chain instead of step 5. Core answers 503 until it has indexed the record.
7. **Re-runs and supersession.** When an INCONCLUSIVE cause clears, re-run the same checkpoint; the later PASS supersedes it. At the TALLY checkpoint this is the only way.
   - A manual re-run with an identical result is not recorded again.
   - A persisting FAIL produces a new FAIL at each later checkpoint. Set `previous_finding_id` to the earlier FAIL of the same rule.
8. **Chain configuration.** Read only the pinned chain configuration from `.env` (chain id, contract address, pinned node URLs, `CONFIRMATION_DEPTH`). `PINNED_NODE_URL` is a comma-separated list (observer first, commission second): try the URLs in order. If Gateway `/v1/chain` differs (including `confirmation_depth`), raise CHAIN_CONFIG_MISMATCH. A direct `submitFindingRecord` counts as recorded only once its block is confirmed.

## 5. Optional features
- **AI Audit Agent.**
  - It polls `GET /internal/v1/findings?has_report=false` for FAIL / INCONCLUSIVE findings and investigates with the tools in `audit-report.schema.json#/$defs/AgentTool` only.
  - It submits an `AuditReportSubmission` without `human_review`. Every entry in `evidence_refs` and `counter_evidence_refs` must resolve, otherwise Core answers 422 EVIDENCE_NOT_FOUND.
  - Staff review the report. It is advisory: finalization never depends on it.
  - **Zero API cost (D-15 v1.2):** a free open-weight model run locally (e.g. Ollama); no paid API, no cloud tier; nothing
    leaves the machine. Go / no-go on 2026-11-20 (`docs/schedule.md`). Reports are written as files (Core report storage
    is future work, D-28); evidence refs are checked by a read-only script.
  - **Template report (no-go fallback, not AI):** must validate against `audit-report.schema.json`:
    - `model = {"provider": "none", "name": "template", "prompt_version": "1.0.0"}` (x.y.z pattern);
    - hypotheses from the B1 rule-ID → cause table, whose causes are values of `common.schema.json#/$defs/CauseCode`
      only; `status` COMPLETED needs at least one hypothesis; a finding whose cause cannot be named (e.g. most
      INCONCLUSIVE) gets `status` ESCALATED;
    - each hypothesis has at least one `evidence_refs` entry (the finding itself and the finding's own evidence);
    - every other required field filled; `limitations` says "generated without AI from the rule-to-cause table".
    It is shown as an explanation aid (`provider none` = not AI) and gets no AI metrics.
  - B2 runs record the model digest, quantization, temperature, seed and hardware.
- **Manual re-run requests** (`audit-run-requests`).

## 6. Pitfalls
- **Hashing must match bit for bit.**
  - Port `H`, `canonical` (RFC 8785), `merkle_root` and `set_root` from `tools/validate_contracts.py`.
  - Reproduce every vector in `tests/vectors/hash-vectors.json` before the first integration (R5 recomputes most of these values).
  - `set_root` sorts and removes duplicates. `ballot_set_root` sorts but **keeps** duplicates.
- **Delivery order.** When CHECKPOINT_COMMITTED k is accepted, every voter event ≤ k already has a final outcome (accepted or rejected), so counts need no grace period.
- **Read the README literally.** Rule conditions use exact definitions:
  - the TALLY checkpoint is reason TALLY;
  - the final checkpoint is the CLOSE_FINAL one;
  - only participating orgs count, and the operator org is excluded from the review quorum.
- **Reproducibility.** A run at k is reproducible: same inputs as of k → same `result`, `observed`, `expected` and evidence refs (`finding_id`, `checked_at` and `data_as_of` differ). Deduplicate manual re-runs on (rule, checkpoint, result, observed), not on the body hash.
- **What you never handle.** No PII ever reaches you. Never log hashes of tokens, nullifiers or receipts, nor `evr_` IDs (README "PII").
- **Private key.** It appears in the `election_config` view only from the TALLY checkpoint on, and only for `ruleKey`. Never pass it to the AI.

## 7. Tests you own

| Test | Expected Rule Engine result |
|---|---|
| **T01** (mandatory) | R1–R5 PASS at the next checkpoint |
| **T02** (mandatory) | R1 PASS with `refused_duplicates ≥ 1` |
| **T04** (mandatory) | R2 PASS with `refused_not_open`; injected late event → R2 FAIL `ledger_rejected_window ≥ 1` and R3 FAIL |
| **T05** (mandatory) | unapproved roster change by DB edit → R4 and/or R5 FAIL |
| **T08** (mandatory) | (a)(c) R5 FAIL `tables_mismatched` / `row_ledger_mismatches`; (b) R5 FAIL `sealed_hash_mismatches` |
| **T15** (mandatory) | (a) R3 + R5 FAIL; (b) R5 FAIL (re-decryption); (c) R5 FAIL (`ballot_set_root`); (d) R3/R5 FAIL if injected |
| T03, T06, T07, T09–T12, T16 | optional (see `docs/test-plan.md`) |

**Experiment fairness.**
- Ground-truth labels for each scenario (`tests/scenarios/<id>/expected.json`) are written and committed by Harits **before** the first run, and never edited after seeing results.
- Baselines: B1 rules only, B2 rules + local AI agent (only if it passed the 11-20 check).
- Metrics: FAIL precision / recall, root-cause accuracy, evidence validity, AI tokens and time to report; API cost is 0 (`docs/test-plan.md`).

Self-test without Core / Gateway: prism mocks of both OpenAPI files plus the fixtures in `contracts/examples/`.

## 8. Milestones

Dates for every item below and the milestones M1–M4: **`docs/schedule.md`** (D-28, replaces the former week table).

A partly implemented rule lists its unimplemented checks in `summary` with the words "not implemented"; a T01 run
with such a finding is reported as "T01 (partial R4/R5)", not as a T01 pass.

## 9. First issues to open
1. D-15 (v1.2, zero cost): name the local model in `docs/decisions.md` after the 11-20 check.
2. Hashing library + `tests/vectors/hash-vectors.json`.
3. Finding builder (FindingSubmission, local schema validation, durable store, signing).
4. Discovery + run scheduler (run condition, timeout, alerts).
5. R1, R2, R3.
6. R4 (signature verification with registry keys, set comparison, roster chain).
7. R5 (snapshots, row match, roots, anchors from the pinned node, TALLY checks, chain state).
8. Re-runs, supersession, `previous_finding_id`.
9. (Optional) AI Audit Agent.

If a contract rule blocks you, open an issue and change the contract by PR (@mention the affected owners, one line in
`contracts/CHANGELOG.md`). Do not agree changes verbally.
