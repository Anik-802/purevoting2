# On-chain interface (normative, v1.1)

Purpose: let the Rule Engine (R4, R5), organizations and any auditor read anchors, election state and the registry
**directly from a validator node**, and let organizations and the Rule Engine **submit signed decisions, reviews and
finding records directly**, so that neither detection nor participation depends on the Gateway or Core.
**Pinned chain configuration.** chain_id and contract_address are fixed when the network is set up, confirmed by every
registered organization, and written into the deployment configuration of Core, the Rule Engine and each organization's
client, together with the RPC URL of at least one node run by an organization other than the Gateway operator (D-26)
(demo: the observer's node first, the commission's second, D-03) and the **confirmation depth D** (D-27, see
"Confirmations"). Readers use only these pinned values. Gateway `GET /v1/chain` (platform, chain_id, contract_address,
rpc_urls — one URL per node, confirmation_depth) is a convenience: readers compare it with the pinned values and raise a
CHAIN_CONFIG_MISMATCH alert (Core itself, the Rule Engine via POST /internal/v1/audit-alerts) if they differ.

The Solidity interface is normative for EVM platforms: D-02 A (Besu) and D-02 C (PureChain, the platform chosen in
v1.1). For Fabric (D-02 B) the chaincode would expose functions with the same names, arguments and return fields
(JSON). The implementation (Harits) may add functions but must not change these. `tools/validate_contracts.py` checks that the interface below has no duplicate function names.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IPureVotingLedger {
    // ---- registry (written at network setup; see hashing.md "Registry and verification") ----
    function orgSigner(bytes32 orgId) external view returns (address signer, address registeredBy);
    function operatorOrg() external view returns (bytes32 orgId, bool confirmedByAll);
    function ruleEngineSigner() external view returns (address signer, bytes32 operatedByOrg, bool confirmedByAll);
    function gatewayAccount() external view returns (address account, bytes32 operatedByOrg, bool confirmedByAll);
    // gatewayAccount: the only account allowed to call the 'Gateway writes' below; registered by the node account of the
    // organization operating the Gateway (D-26) and effective only after every registered organization confirmed it.

    // ---- election state ----
    function electionState(bytes32 electionId) external view returns (
        uint8 status,              // 0 DRAFT, 1 ROSTER_FROZEN, 2 OPEN, 3 CLOSED, 4 TALLIED, 5 FINALIZED, 6 VOIDED
        uint64 windowStart, uint64 windowEnd, uint32 timeBucketSeconds,
        bytes32 configHash, bytes32 rosterHash, bytes32 tokenSetRoot,
        bytes32 resultHash, uint64 lastCheckpoint, uint64 lastCoreSeq, bytes32 operatorOrgId
    );

    function electionGovernance(bytes32 electionId) external view returns (
        bytes32[] memory orgIds, uint8 approvalThreshold, bytes32 operatorOrgId, uint32 proposalTtlHours,
        uint32 checkpointIntervalSeconds
    );   // orgIds in the order passed to createElection (ascending org_id string order, see below)

    // State as stored by commitCheckpoint(k) at that moment (platform-neutral; no historical-block query needed).
    function electionStateAtCheckpoint(bytes32 electionId, uint64 checkpoint) external view returns (
        uint8 status, uint64 windowEnd, bytes32 rosterHash, bytes32 tokenSetRoot, bytes32 resultHash, uint64 lastCoreSeq
    );

    // ---- anchors ----
    function anchorCount(bytes32 electionId) external view returns (uint256);
    function anchorAt(bytes32 electionId, uint256 index) external view returns (
        bytes32 anchorId, bytes32 merkleRoot, bytes32 rejectionsRoot,
        uint8 mode,                // 0 immediate, 1 batched
        uint32 eventCount, uint64 bucketMin, uint64 bucketMax
    );

    // ---- governance (read) ----
    function proposalState(bytes32 proposalId) external view returns (
        uint8 status,              // 0 PENDING, 1 APPROVED, 2 REJECTED, 3 EXPIRED
        bytes32 targetHash, uint8 approvals, bool executed
    );
    function findingRecord(bytes32 findingId) external view returns (
        bytes32 electionId, bytes32 findingHash, uint8 ruleId, uint8 result, uint64 findingCheckpoint,
        uint8 status               // 0 NOT_REQUIRED, 1 PENDING, 2 RESOLVED, 3 SUPERSEDED, 4 CONFIRMED_ISSUE
    );

    // ---- direct submission (anyone may send; the signature is what counts) ----
    // Every decision / review counts only if orgId is a participating organization of that election.
    // The contract rebuilds the digest from these arguments exactly as in hashing.md and checks the registry key.
    function submitDecision(
        bytes32 proposalId, bytes32 actionId, bytes32 electionId, uint8 actionType, bytes32 targetHash,
        bytes32 orgId, uint8 decision, bytes calldata signature
    ) external;
    function submitFindingRecord(
        bytes32 findingId, bytes32 electionId, uint8 ruleId, uint8 result, uint64 findingCheckpoint,
        bytes32 findingHash, bytes calldata signature, string calldata findingIdText, string calldata electionIdText
    ) external;   // contract checks sha256(findingIdText) == findingId, sha256(electionIdText) == electionId and
                  // findingCheckpoint <= the election's last committed checkpoint (same rule as the Gateway path;
                  // refused while no checkpoint is committed yet, see "Checkpoint 0"),
                  // so the Gateway can index the real ID strings; a second record for the same findingId is a no-op if
                  // findingHash is identical and reverts otherwise
    function submitFindingReview(
        bytes32 findingId, bytes32 electionId, bytes32 findingHash, bytes32 orgId, uint8 decision,
        bytes32 noteHash, bytes calldata signature
    ) external;

    // ---- Gateway writes (callable only by the registered Gateway account; each applies one core system event) ----
    // The contract enforces the state machine (README#election-state-machine) and the checks named here; any violation reverts,
    // which the Gateway reports as REJECTED with the matching reject_code.
    function createElection(bytes32 electionId, bytes32 configHash, uint64 windowStart, uint64 windowEnd,
        uint32 timeBucketSeconds, uint32 checkpointIntervalSeconds, bytes32[] calldata orgIds, uint8 approvalThreshold,
        bytes32 operatorOrgId, uint32 proposalTtlHours, uint64 coreSeq) external;           // orgs/operator must equal the registry; 2 <= threshold <= orgs-1; configHash != 0;
        // orgIds = idh(org_id) of ELECTION_CREATED.organizations in ascending org_id string order; duplicates or ids not in
        // the registry revert. The order cannot be checked on chain (only hashes are stored); Core's creation check
        // compares element by element and detects a different order
    function freezeRoster(bytes32 electionId, bytes32 rosterHash, uint64 coreSeq) external;
    function openElection(bytes32 electionId, uint64 coreSeq) external;   // block time >= windowStart (else TIME_NOT_REACHED, see below)
    function closeElection(bytes32 electionId, uint64 closedAt, bool early, bytes32 proposalId, uint64 requestedClosedAt,
        uint64 executedAt, uint64 coreSeq) external;                // early: proposal APPROVED+executed, closedAt formula
    function openProposal(bytes32 proposalId, bytes32 actionId, bytes32 electionId, uint8 actionType, bytes32 targetHash,
        bytes32 requestingOrgId, bytes calldata requestSignature) external;
        // expiresAt = block time + proposalTtlHours (stored by the contract); decisions after expiresAt are refused and the
        // proposal reads EXPIRED, for relayed and direct submissions alike
    function failAction(bytes32 proposalId, uint64 coreSeq) external;   // ADMIN_ACTION_FAILED: approved proposal can no longer be executed
    function executeAction(bytes32 proposalId, uint64 coreSeq) external;  // APPROVED, not yet executed (else ALREADY_EXECUTED); opens the effect window
    function amendRoster(bytes32 electionId, bytes32 proposalId, bytes32 rosterHashBefore, bytes32 rosterHashAfter, uint64 coreSeq) external;
    function changeWindow(bytes32 electionId, bytes32 proposalId, uint64 newWindowEnd, uint64 coreSeq) external; // newWindowEnd bucket-aligned; extension rejected once block time >= current end
    function setTokenSetRoot(bytes32 electionId, bytes32 tokenSetRoot, bytes32 proposalIdOrZero, uint64 coreSeq) external;
    function commitCheckpoint(bytes32 electionId, uint64 checkpoint, uint8 reason, bytes32 tablesHash, uint64 coreSeq) external;
        // also stores the current electionState under (electionId, checkpoint) for electionStateAtCheckpoint;
        // checkpoint must be greater than the last checkpoint stored for the election (strictly increasing; a number whose
        // commit was rejected is skipped, so gaps only occur after rejections)
        // reason 0 PERIODIC, 1 STATE_CHANGE, 2 CLOSE_FINAL, 3 TALLY (lets the contract identify the TALLY checkpoint); tablesHash = Hc(payload.tables)
        // CLOSED→TALLIED commits one checkpoint with reason TALLY (not an extra STATE_CHANGE)
        // reason CLOSE_FINAL: at most once per election and only while status == CLOSED (v1.1). Consequence: if
        // ELECTION_CLOSED was rejected, Core stays CLOSED (strict) but its CLOSE_FINAL is rejected too, so no tally is
        // possible and ELECTION_VOID is the only exit (README state machine) — intended
    function commitAnchor(bytes32 electionId, bytes32 anchorId, bytes32 merkleRoot, bytes32 rejectionsRoot, uint8 mode,
        uint32 eventCount, uint64 bucketMin, uint64 bucketMax) external; // batched: bucketMax + bucket <= effective end
    function publishTally(bytes32 electionId, bytes32 resultHash, uint64 finalCheckpoint, uint64 coreSeq) external; // once only; finalCheckpoint == the CLOSE_FINAL checkpoint
    function finalizeElection(bytes32 electionId, bytes32 resultHash, uint64 coreSeq) external; // equals tally; finding quorum (README)
    function voidElection(bytes32 electionId, bytes32 proposalId, uint64 coreSeq) external; // allowed from DRAFT, ROSTER_FROZEN, OPEN, CLOSED, TALLIED
    function relayDecision(bytes32 proposalId, bytes32 orgId, uint8 decision, bytes calldata signature) external;   // same checks as submitDecision
    function relayFindingRecord(bytes32 findingId, bytes32 electionId, uint8 ruleId, uint8 result, uint64 findingCheckpoint,
        bytes32 findingHash, bytes calldata signature, string calldata findingIdText, string calldata electionIdText) external;     // same checks as submitFindingRecord
    function relayFindingReview(bytes32 findingId, bytes32 electionId, bytes32 findingHash, bytes32 orgId, uint8 decision,
        bytes32 noteHash, bytes calldata signature) external;        // same checks as submitFindingReview

    // ---- events (indexed for log queries; carry everything the Gateway needs to index them) ----
    event AnchorCommitted(bytes32 indexed electionId, bytes32 indexed anchorId, bytes32 merkleRoot, bytes32 rejectionsRoot, uint8 mode, uint32 eventCount);
    event StateChanged(bytes32 indexed electionId, uint8 status, uint64 coreSeq);
    event ProposalDecided(bytes32 indexed proposalId, bytes32 indexed orgId, uint8 decision, bytes signature);
    event FindingRecorded(bytes32 indexed findingId, bytes32 indexed electionId, uint8 ruleId, uint8 result, uint64 findingCheckpoint, bytes32 findingHash, bytes signature, string findingIdText, string electionIdText);
    event FindingReviewed(bytes32 indexed findingId, bytes32 indexed orgId, uint8 decision, bytes32 noteHash, bytes signature);
}
```

## Encodings

| Contract type | Off-chain value |
|---|---|
| `bytes32` IDs (`electionId`, `anchorId`, `proposalId`, `actionId`, `findingId`, `orgId`) | `idh(id) = sha256(utf8(id string))` — the same `idh` used inside signed digests (hashing.md) |
| `bytes32` hashes (`targetHash`, `findingHash`, `noteHash`, roots) | the 64-hex SHA-256 value as raw 32 bytes |
| `actionType` | ROSTER_AMEND 1, TOKEN_REISSUE 2, WINDOW_CHANGE 3, ELECTION_CLOSE_EARLY 4, ELECTION_VOID 5 |
| `decision` in `submitDecision` / `ProposalDecided` | APPROVE 1, REJECT 2 |
| `decision` in `submitFindingReview` / `FindingReviewed` | DISMISSED 1, ACCEPTED_RISK 2, CONFIRMED_ISSUE 3 |
| `ruleId`, `result` | R1–R5 → 1–5; PASS 0, FAIL 1, INCONCLUSIVE 2 |
| timestamps | Unix seconds (UTC) |
| `signature` | 65 bytes r ‖ s ‖ v (eip191-secp256k1) |

## Execution order of approved actions (normative)

1. `executeAction(proposalId)` first (ledger: ADMIN_ACTION_EXECUTED). It marks the proposal executed and opens its
   **effect window**.
2. Then, in the next core seq numbers and before any other system event, the effects of that proposal only:
   ROSTER_AMEND → exactly one `amendRoster` + zero or more `setTokenSetRoot` (one per revoked token);
   TOKEN_REISSUE → one or more `setTokenSetRoot`; WINDOW_CHANGE → exactly one `changeWindow`;
   ELECTION_VOID → exactly one `voidElection`. The window closes at the first system event that is not an effect of that
   proposal; an effect outside its window, a second `amendRoster` / `changeWindow` / `voidElection`, or an effect of a
   proposal of another type is rejected (ALREADY_EXECUTED / INVALID_STATE_TRANSITION).
3. ELECTION_CLOSE_EARLY is the exception: `closeElection(early=true)` comes later, at the effective closed_at, and is
   accepted once for an executed proposal. If it has not been applied one bucket after the effective closed_at, R4 reports
   `missing_effects`.

## Time preconditions

When a call fails only because its block-time condition is not reached yet (`openElection` before windowStart, a
scheduled `closeElection` before closedAt), the contract reverts with `TIME_NOT_REACHED` and the Gateway answers
**RETRY_LATER**, never REJECTED; Core's outbox retries with the same event, so clock skew of a few seconds cannot reject
a boundary transition. If ELECTION_OPENED is nevertheless REJECTED (another cause), Core stays ROSTER_FROZEN, alerts, and
may emit a new ELECTION_OPENED (new event_id and seq) after the cause is fixed, as long as the window has not ended.

## Contract state derived from ledger events (used by Core and R5)

Unset hash fields are `bytes32(0)` on chain and the 64-character string `000…0` in the Gateway API and in R5's derived state.

| Field | Initial value | Set / changed by (ledger event → contract call) |
|---|---|---|
| status | DRAFT | ROSTER_FROZEN, ELECTION_OPENED, ELECTION_CLOSED, TALLY_PUBLISHED, ELECTION_FINALIZED, ELECTION_VOIDED |
| configHash, windowStart, timeBucketSeconds, checkpointIntervalSeconds, orgIds, approvalThreshold, operatorOrgId, proposalTtlHours | from ELECTION_CREATED | never change |
| windowEnd | ELECTION_CREATED.window_end | last accepted WINDOW_CHANGED.new_window_end; ELECTION_CLOSED.closed_at if earlier |
| rosterHash | 0 | ROSTER_FROZEN.roster_hash, then each ROSTER_AMENDED.roster_hash_after |
| tokenSetRoot | 0 | each TOKEN_BATCH_ISSUED / TOKEN_REVOKED .token_set_root |
| resultHash | 0 | TALLY_PUBLISHED.result_hash |
| lastCheckpoint | none (reads as 0, see "Checkpoint 0") | each accepted CHECKPOINT_COMMITTED |

## Checkpoint 0

Checkpoint numbers start at 0. Core's open checkpoint is 0 from creation (ELECTION_CREATED and ROSTER_FROZEN carry
checkpoint 0), and the first commit is checkpoint 0 with reason STATE_CHANGE, right after ROSTER_FROZEN; then checkpoint 1
is open. The contract accepts 0 as the first commit; if that commit is rejected, the next committed number is the first
one (gaps only after rejections). Before any commit, `electionState.lastCheckpoint` reads 0 and means "none"; readers
tell the cases apart with `electionStateAtCheckpoint(e, 0).windowEnd != 0` (a commit always stores windowEnd). Checks of
the form "≤ last committed checkpoint" refuse every value while no checkpoint is committed.

**Creation check.** Right after ELECTION_CREATED is ACCEPTED, Core reads `electionState` and `electionGovernance`
**directly from a validator node** (not through the Gateway), using the pinned chain configuration above (contract
address and the RPC URL of a node not run by the Gateway operator), with the chain-interface getters only. Core
compares the immutable creation fields — configHash, windowStart, windowEnd (still unchanged at this point),
timeBucketSeconds, checkpointIntervalSeconds, orgIds (element by element, both in ascending org_id order), approvalThreshold,
operatorOrgId and proposalTtlHours — with the ELECTION_CREATED payload. Until the check has passed, Core delivers no further
outbox event of the election, and refuses `roster:freeze`, `tokens:issue` and admin-action create/submit with 409
INVALID_STATE_TRANSITION; `Election.creation_check` shows PENDING / PASSED / MISMATCH.
- **Not yet visible ≠ mismatch.** The check starts when ELECTION_CREATED is ACCEPTED or DUPLICATE; both SubmitResults carry
  `tx_hash` and `block_number`. If the pinned node's latest block is lower than `block_number + D` (the block is not
  confirmed on that node yet, see "Confirmations"), Core retries (creation_check stays PENDING); if the node is
  unreachable, Core retries and raises LEDGER_UNAVAILABLE after 60 s.
- **Not found.** If the node's latest block has reached `block_number + D` and `configHash` still reads as 0 (the
  contract's marker for "no such election"; the block is confirmed, so this is not a delay) for 60 s, the Gateway's
  report was wrong: this is treated as a mismatch (GOVERNANCE_MISMATCH, detail "not found").
- Only these two cases are a mismatch: values that exist and differ, or not found after the block was reached.
- **Mismatch** → alert (GOVERNANCE_MISMATCH), creation_check = MISMATCH (final): the FIFO cannot be resumed (resumeOutbox
  → 409) and the only exit is to delete the election at Core (`POST /v1/elections/{id}:delete`). The DRAFT record left on
  chain is abandoned: the Gateway account could still append unsigned events to it (I12), but no Core election exists for
  it, so nothing recorded under that ID is an official result; election IDs are never reused. VOID is not used here,
  because its quorum would be computed from the mismatching on-chain governance.

R5 repeats the comparison of the immutable fields at every checkpoint (`governance_mismatch`; windowEnd excluded, it is
covered by `electionStateAtCheckpoint`). So a Gateway that passes other organizations or another threshold to
`createElection` is detected by Core before the roster is frozen and before any approval or review exists, even if the
Gateway's own API reports the expected values.

## Confirmations (v1.1, D-27)

A block can be replaced by a fork on the chosen platform, so being included in a block is not enough.

- **Confirmed.** A block b is confirmed on a node when that node's latest block ≥ b + D (D = pinned `confirmation_depth`,
  provisional 2). With D = 0 this is exactly the v1.0 behaviour. The platform must seal blocks continuously (also empty
  blocks) with period P, and (1 + D) · P ≤ time_bucket_seconds / 4 (D-02).
- **Gateway output.** The Gateway indexes and exposes chain-derived data **only from confirmed blocks** (on the node it
  writes through): `POST /v1/events` outcomes ACCEPTED, DUPLICATE and REJECTED (a revert counts only when its receipt is in
  a confirmed block), finding-record results, anchor status ANCHORED, `/v1/anchors` and proofs, ledger events indexed from
  ProposalDecided / FindingRecorded / FindingReviewed and the proposal status derived from them, and the organization
  registry. Until then a submission gets RETRY_LATER and a read shows the item as not yet recorded.
- **Resubmission.** The Gateway keeps the signed transaction of each submission. While it is not in a confirmed block, the
  Gateway only re-broadcasts **that same signed transaction** (same nonce and hash), never a new one, so an effect is
  applied at most once. A retry after a confirmed revert (e.g. TimeNotReached) is a new transaction; the Gateway should
  check time preconditions with `eth_call` first and answer RETRY_LATER without mining a revert.
- **"Already applied" reverts.** If a call reverts because its effect may already be on chain (ElectionExists,
  SeqNotIncreasing, CloseFinalAlreadyCommitted), the Gateway answers ACCEPTED / DUPLICATE only on an **exact match** in
  confirmed state, otherwise REJECTED: state events — a confirmed StateChanged with coreSeq equal to the event's seq;
  ELECTION_CREATED — the election exists with the same configHash and creation fields; checkpoints —
  `electionStateAtCheckpoint(k).lastCoreSeq` equals the event's seq. (A repeated commitAnchor with identical fields does
  not revert: it is a no-op without an event; AnchorConflict always means different fields.)
- **Revert → outcome mapping** (Gateway writes):

  | Contract revert | Gateway outcome |
  |---|---|
  | TimeNotReached | RETRY_LATER (TIME_NOT_REACHED) |
  | NotGateway | RETRY_LATER and a LEDGER_UNAVAILABLE alert (the Gateway account is not configured or not confirmed: a setup error, not a rule violation) |
  | OrgNotRegistered, OperatorOrgMismatch | REJECTED ORG_NOT_REGISTERED |
  | "already applied" reverts above | ACCEPTED / DUPLICATE on an exact match, otherwise REJECTED INVALID_STATE_TRANSITION |
  | any other revert | REJECTED INVALID_STATE_TRANSITION (the revert name goes into the rejection record's detail) |

- **Readers.** Core's creation check, the Rule Engine (R4, R5) and organizations' clients count a chain item only when
  their pinned node's latest block ≥ the item's block + D; otherwise they retry, and after their normal timeout report
  the item as not available (R5: BATCH_NOT_ANCHORED for an anchor; NODE_UNAVAILABLE for any other chain item — registry,
  state, snapshot — or when no node answers) — never as a mismatch. The pinned node URLs are tried in order. A direct submission (submitDecision, submitFindingRecord, submitFindingReview) counts as recorded only when
  confirmed.
- **Limit.** D protects against accidental forks only. Validators controlling a majority (in the demo, one person) can
  rewrite the chain to any depth; this is detected afterwards by R5 and Core comparisons, not prevented (threat model I15).

## Boundary timestamps

A system event scheduled on a boundary (ELECTION_OPENED, scheduled ELECTION_CLOSED, PERIODIC CHECKPOINT_COMMITTED) carries
the **logical boundary time T** as occurred_at, even if Core processes it slightly later; R5's cadence check uses these values.

## Matching of direct submissions

`submitDecision` must carry the stored proposal's actionId, electionId, actionType and targetHash; `submitFindingReview`
must carry the stored record's electionId and findingHash. Any mismatch reverts.

## Trust in the Gateway account

Core system events reach the contract through the Gateway account and are not signed by Core. A dishonest Gateway
operator could therefore inject or pre-empt an unsigned system event (for example call `publishTally` first, which is
allowed only once). Core detects this — every ledger event with source `core` must match an event in Core's outbox by
`event_id` and `payload_hash`, otherwise Core raises an alert and stops the election's FIFO — and R5 compares the
contract's `electionStateAtCheckpoint(k)` (status, windowEnd, rosterHash, tokenSetRoot, resultHash) with the ledger
events up to its lastCoreSeq, which catches a Gateway calling the contract with arguments that differ from the events — but neither prevents it;
recovery is ELECTION_VOID. This trust assumption is listed in threat-model I12 and D-26.

## Indexing by the Gateway

The Gateway turns chain events into ledger events: `ProposalDecided` → APPROVAL_RECORDED, `FindingRecorded` →
FINDING_RECORDED, `FindingReviewed` → FINDING_REVIEWED (payload `submitted_via: chain` when the transaction did not come
from the Gateway). Directly submitted reviews have no reviewer_id or note text; the organization may later attach the note
through Core (`PATCH /v1/findings/{id}/review` with the same signature), which checks `H(note) = note_hash`.

## How R5 uses it

**Chain state comparison.** R5 at checkpoint k reads `electionStateAtCheckpoint(electionId, k)` — the state the contract stored
when CHECKPOINT_COMMITTED k was applied — and compares it with the state implied by the ledger's core events with
seq ≤ that snapshot's `lastCoreSeq` (status, windowEnd, rosterHash, tokenSetRoot, resultHash). Later chain progress
therefore never causes a mismatch, and re-running an old checkpoint gives the same answer.

Only confirmed chain data counts ("Confirmations"). Anchors are enumerated from the Gateway list (`/v1/anchors`, each with
its block_number), not by index: `anchorAt` has no block number and `anchorCount` may include unconfirmed anchors. An
on-chain anchor missing from the Gateway list is a mismatch only when its AnchorCommitted log is in a confirmed block.
For every anchor of the election: read `anchorAt` from the pinned node ("Pinned chain configuration"; `rpc_urls` from the
Gateway only for comparison), recompute `merkle_root` from `GET /v1/anchors/{id}/leaves` + the events and `rejections_root` from
`GET /v1/rejections`, and compare with the on-chain values. Any difference → R5 FAIL. No node reachable → INCONCLUSIVE
(`NODE_UNAVAILABLE`).
