# Role design — Ledger, smart contract and Gateway (Harits)

> Part of the PureVoting 2.0 design. Overall picture: `docs/system-design.md`. Source of truth for every rule below:
> `contracts/` v1.0 — if this page and a contract disagree, the contract wins. Folder: `ledger/`.

## 1. Mission and boundaries

You own the only path between the system and the blockchain:

- the **smart contract** (`IPureVotingLedger` in `contracts/chain-interface.md`) and the validator nodes;
- the **Ledger Gateway** service (`contracts/ledger-gateway.openapi.yaml`, port 8100), which stores ledger events, puts
  them on chain, runs approval proposals and answers queries.

You do **not** own: deciding what an event means for the election (Core), judging anomalies (Rule Engine), or showing
anything to staff (Core / web). You never see names, raw tokens or raw credentials.

**You decide:** the blockchain platform (D-02) and the node setup (D-03). **Hard requirement (D-11):** the chain itself
(contract or chaincode) must verify the signatures of approvals, reviews and finding records against the on-chain
registry. If the Gateway verified them instead, the claim "the Gateway operator cannot forge approvals" would be false.
- Besu / any EVM chain: use the Solidity interface as written, `eip191-secp256k1` signatures, `ecrecover`.
- Fabric: same function names, arguments and return fields as JSON; signatures `ed25519` (hashing.md).
Tell the team which one you chose in week 1 (one line in `docs/decisions.md` D-02 by PR).

## 2. Read first

| File | Sections |
|---|---|
| `contracts/chain-interface.md` | all of it (normative interface, encodings, execution order, time preconditions, derived state, indexing) |
| `contracts/ledger-gateway.openapi.yaml` | `info.description` (responsibilities, who enforces what), `POST /v1/events` (outcomes, delivery order), `/v1/proposals*`, `/v1/finding-records*`, `/v1/anchors*`, `/v1/rejections` |
| `contracts/hashing.md` | `leaf_hash`, `merkle_root`, `set_root`, `rejection_leaf`, `rejections_root`, Signatures (digest layout, registry) |
| `contracts/ledger-events.schema.json` | event envelope and payload per type |
| `contracts/README.md` | Election state machine, Checkpoints, Consistency model, Ledger event types |

## 3. Interfaces

**You provide**

| Interface | Callers (key) | Notes |
|---|---|---|
| `POST /v1/events` | Core (`coreKey`) only | batch ≤ 500, idempotent by `event_id` |
| `GET /v1/events*`, `/v1/rejections*`, `/v1/anchors*`, `/v1/verify`, `/v1/elections/{id}/state`, `/v1/organizations`, `/v1/nodes`, `/v1/chain` | Core, Rule Engine, Audit Agent, `auditorKey` | read only |
| `POST /v1/proposals`, `POST /v1/proposals/{id}/decisions` | Core (relays signatures it cannot create) | |
| `POST /v1/finding-records` | Rule Engine (`ruleKey`) only | |
| `POST /v1/finding-records/{id}/reviews` | Core (relays signed reviews) | |
| Contract (`chain-interface.md`) | Gateway account (writes), anyone (direct submissions, reads) | |

**You consume:** Core `POST /internal/v1/audit-alerts` with `gatewayKey`, only to raise `ANCHOR_REJECTED`; Core
`POST /internal/v1/service-logs:append` for your own logs (optional).

## 4. Mandatory features

### 4.1 Contract
1. **Registry** set up at network start.
   - Each organization's node account registers its own signer (`orgSigner`).
   - `operatorOrg`, `ruleEngineSigner` and `gatewayAccount` become effective only after every registered organization has confirmed them.
   - A setup script is fine; demo orgs are `org_commission`, `org_observer` and `org_neutral`.
2. **Election state machine** exactly as in README "Election state machine". Any violation reverts, and the Gateway reports it as REJECTED with the matching `reject_code`.
   - `createElection`:
     - orgs and operator must equal the registry;
     - 2 ≤ threshold ≤ orgs − 1;
     - `configHash != 0`;
     - orgIds = `idh(org_id)` in ascending org_id order, no duplicates.
   - `openElection` / scheduled `closeElection` before their block time revert with `TIME_NOT_REACHED`. The Gateway answers **RETRY_LATER** for these, never REJECTED.
   - `publishTally` is allowed once, and `finalCheckpoint` must be the CLOSE_FINAL checkpoint.
   - `finalizeElection` checks:
     - the result hash equals the tally;
     - the finding quorum holds. R1–R5 records exist at the TALLY checkpoint. Every FAIL / INCONCLUSIVE is RESOLVED or SUPERSEDED. There is no CONFIRMED_ISSUE.
     - Quorum = signed reviews from ≥ threshold participating orgs **other than the operator**.
   - `voidElection` is allowed from DRAFT, ROSTER_FROZEN, OPEN, CLOSED and TALLIED.
3. **Checkpoints**:
   - `commitCheckpoint` requires strictly increasing numbers per election.
   - It stores the current state under (election, checkpoint) for `electionStateAtCheckpoint`.
   - `reason` is 0 PERIODIC, 1 STATE_CHANGE, 2 CLOSE_FINAL or 3 TALLY.
4. **Anchors:** `commitAnchor` with mode 0 immediate / 1 batched. A batched anchor is refused if `bucketMax + bucket > effective end`.
5. **Proposals:**
   - `openProposal` stores `expiresAt = block time + proposalTtlHours`.
   - Decisions are relayed or submitted directly; the requester cannot approve its own action.
   - At the threshold the proposal is APPROVED. When approval becomes impossible it is REJECTED. After `expiresAt` it is EXPIRED.
   - **Execution order** (chain-interface "Execution order"):
     - `executeAction` comes first and opens the effect window.
     - Then only that proposal's effects follow, for example exactly one `amendRoster` plus `setTokenSetRoot` calls.
     - A second execution → `ALREADY_EXECUTED`.
6. **Signature verification on chain** for request / approval / finding-record / review digests.
   - The contract rebuilds each digest from its `bytes32` arguments (hashing.md "Signatures") and checks it against the registry key.
   - The signing org must take part in that election.
   - Each org may decide or review only once.
7. **Finding records and reviews:**
   - `findingCheckpoint` ≤ the last committed checkpoint.
   - A second record with the same hash is a no-op; with a different hash it reverts.
   - The status follows the encodings 0 NOT_REQUIRED … 4 CONFIRMED_ISSUE.
   - SUPERSEDED: an INCONCLUSIVE is superseded by a PASS of the same rule at a higher checkpoint, or at the same checkpoint if that PASS is recorded later. A FAIL is never superseded.

### 4.2 Gateway
1. **Event intake** (`POST /v1/events`). Outcomes:
   - **ACCEPTED** — for system events this means the transaction is included in a **confirmed** block (latest block ≥ its block + `confirmation_depth` D, D-27, contracts v1.1). Return `tx_hash` and `block_number` for ACCEPTED and DUPLICATE **system events** (voter events have no transaction yet).
   - **DUPLICATE** — same `event_id`, same `payload_hash`.
   - **CONFLICT** — same `event_id`, different `payload_hash`.
   - **REJECTED** — see the reject codes in the OpenAPI description.
   - **RETRY_LATER** — a seq gap, ELECTION_OPENED not applied yet, a pending or not yet confirmed transaction, or `TIME_NOT_REACHED`.
   - **Confirmations (v1.1, chain-interface.md "Confirmations"):** expose chain-derived data (outcomes incl. REJECTED, finding records, anchors and proofs, indexed chain events, proposal status, registry) only from confirmed blocks; while a transaction is unconfirmed re-broadcast only the same signed transaction; "already applied" reverts → ACCEPTED / DUPLICATE only on an exact match; follow the revert → outcome table there.
2. **Ordering:**
   - Apply core system events strictly by `seq` per election. A rejected event still consumes its seq.
   - Voter events have no seq.
3. **Voter-side events** (TOKEN_USED, BALLOT_RECEIVED):
   - Check each one in software: the window, using the bucket formula of README R2, and duplicate `token_hash` / `nullifier_hash` (DUPLICATE_CONTENT). Also reject TOKEN_USED of a revoked token (TOKEN_REVOKED) and TOKEN_REVOKED of a used token (TOKEN_ALREADY_USED). The chain cannot see individual voter events (D-22).
   - **Never record their arrival time or arrival order.**
   - Collect them into a batch. Close the batch every **60 s or at 50 events**, shuffle the leaves randomly at batch close and anchor the Merkle root.
4. **Rejections:** every refused submission becomes a rejection record (`rejection_leaf`). Their `rejections_root` goes into the next anchor of that election, and `anchor_id` is set once that anchor exists.
5. **Proposals:** create and relay them, and emit ADMIN_ACTION_REQUESTED, APPROVAL_RECORDED and PROPOSAL_CLOSED. Report APPROVED only after every APPROVAL_RECORDED that made it so is stored.
6. **Finding records:** `POST /v1/finding-records` takes the Rule Engine's signed record. It is idempotent, and returns 422 CHECKPOINT_NOT_FOUND if the checkpoint is not committed yet. Review relay emits FINDING_REVIEWED.
7. **Indexing chain events** (the relayed path is mandatory; indexing of direct submissions is optional, T10 / T16):
   - Turn `ProposalDecided`, `FindingRecorded` and `FindingReviewed` into ledger events. Use `submitted_via: chain` when the transaction did not come from you.
   - This makes direct submissions visible to Core and to R4.
8. **Queries:** every GET in the OpenAPI.
   - `GET /v1/anchors/{id}/leaves` returns the leaves in the anchor's order, plus `rejection_ids`.
   - `/v1/elections/{id}/state` reads the contract getters.
9. **Anchor refused by the contract:** keep the events PENDING and raise `ANCHOR_REJECTED` at Core. The recovery is manual: fix the cause, then re-anchor the same leaves.

## 5. Optional features
- Hardening the indexing of direct submissions (`submitDecision`, `submitFindingRecord`, `submitFindingReview`) for T10 / T16.
- WINDOW_CHANGE, ELECTION_CLOSE_EARLY and TOKEN_REISSUE effects. They are already in the contract interface; test them when Core implements them.

## 6. Pitfalls (things reviewers will check)
- **Hashing must match bit for bit.**
  - Port `H`, `canonical` (RFC 8785), `merkle_root` and `set_root` from `tools/validate_contracts.py`.
  - Reproduce every vector in `tests/vectors/hash-vectors.json` (leaf_hash, rejection_leaf, rejections_root, roots, the three digests) before anything else.
  - Odd Merkle levels carry the last node up unchanged.
  - An empty list hashes to `H("")`.
- `bytes32` IDs are `sha256(utf8(id string))`, not the string itself.
- **System events and buckets:** Core releases system events at the end of their time bucket. Do not assume real-time arrival.
- **Signer keys:** use fresh signer keys per deployment (test vs demo). Digests carry no chain id.
- **Dishonest-Gateway checks:** Core and R5 compare your contract state with the ledger events (creation check, `electionStateAtCheckpoint`). Calling the contract with arguments that differ from the event is detected — so never "fix up" values.
- **Pinned node:** Core and the Rule Engine read a pinned node run by an org other than the Gateway operator (demo: the observer). Your node setup must provide that RPC URL.

## 7. Tests you own

| Test | Your part |
|---|---|
| **T01** (mandatory) | events anchored, receipt status ANCHORED; ELECTION_OPENED a few seconds early → RETRY_LATER, never REJECTED |
| **T04** (mandatory) | a late voter event injected into the outbox is REJECTED with `ELECTION_NOT_OPEN` and appears in `/v1/rejections` |
| **T05** (mandatory) | the contract refuses ROSTER_AMENDED without an APPROVED proposal |
| **T15(d)** (mandatory) | `publishTally` with a final checkpoint ≠ CLOSE_FINAL is rejected |
| T06(b), T07, T10, T11(a), T16 | optional: ALREADY_EXECUTED; signature / participation checks and the creation-check variant; outage and catch-up; unanchored batch; direct submissions |

Self-test without Core: replay the events in `contracts/examples/valid/ledger-events/` in order. Only seq, checkpoints,
payload_hash, receipt_hash and result_hash are consistent in the examples; anchor roots, snapshot hashes and set roots are
placeholders — check your Merkle code against `tests/vectors/hash-vectors.json`, not against example anchors.

**Ground-truth labels:** you write the labels (`tests/scenarios/<id>/expected.json`) for T01, T02, T04, T05, T08 and T15,
and the B1 rule-ID → cause mapping table — dates in `docs/schedule.md`, always before the first run (see `tests/scenarios/README.md`).
Seungmin writes the setup scripts; Anik never writes labels.

## 8. Milestones

Dates for every item below and the milestones M1–M3: **`docs/schedule.md`** (D-28, replaces the former week table).

## 9. First issues to open
1. D-02 decision + PR to `docs/decisions.md`.
2. Hashing / digest library + `tests/vectors/hash-vectors.json`.
3. Node setup (pinned node on the observer) + registry setup script + `.env` entries (chain id, contract address, pinned node URL).
4. Contract: registry + election state + checkpoints (with `electionStateAtCheckpoint`).
5. Gateway: `POST /v1/events` (seq, outcomes, voter checks, batching, rejections).
6. Contract + Gateway: finding records (`relayFindingRecord`, `POST /v1/finding-records`) (needed for T01).
7. Gateway: query endpoints + indexing of relayed chain events.
8. Contract: anchors refusal rule, proposals + execution order.

### v1.1 additions (2026-11-02, decisions D-02 / D-03 / D-27)
- Gateway: confirmed-blocks rule, same-signed-transaction re-broadcast, exact-match handling of "already applied"
  reverts, revert → outcome table (NotGateway = RETRY_LATER + LEDGER_UNAVAILABLE alert), `eth_call` pre-check of time
  preconditions, `GET /v1/chain.confirmation_depth`.
- Contract: commitCheckpoint with reason CLOSE_FINAL only while status == CLOSED; `submitFindingRecord` /
  `relayFindingRecord` use `hasCheckpoint` for "≤ last committed checkpoint"; optional bucket-alignment checks in
  createElection; optional `CheckpointCommitted` event.
- Network: a mode that seals blocks continuously (empty blocks) with period P ≤ 5 s (provisional).
- **Measurements due 2026-11-13 (D-28; D = 2 until then):** mode name and P; one- and two-validator stop tests; deepest reorg observed in
  stop/restart tests (final D = max(2, that + 1)). Record them in D-02 / D-03 / D-27.
9. Contract: reviews, finalize quorum.
10. (Optional) indexing of direct submissions.

If a contract rule blocks you, open an issue and change the contract by PR (@mention the affected owners, one line in
`contracts/CHANGELOG.md`). Do not agree changes verbally.
