# PureVoting 2.0 — Interface Contracts (v1.1, 2026-11-02; v1.0 frozen 2026-10-18)

These files are the single source of truth for how components talk to each other.
Implement against them; if you need a change, change the contract first (see *Change process*).
Consistent with the confirmed decisions in `../docs/decisions.md` (v1.1: D-02 PureChain, D-03, D-27 confirmation depth). Threats and known limits: `../docs/threat-model.md`.
How each defect test (T01–T16; mandatory for the MVP: T01, T02, T04, T05, T08, T15) maps to these contracts: `../docs/test-plan.md`.

| File | Producer | Consumers |
|---|---|---|
| `core-api.openapi.yaml` | Core API | Web (voter/admin), Rule Engine, Audit Agent |
| `ledger-gateway.openapi.yaml` | Ledger Gateway | Core API, Rule Engine, Audit Agent |
| `ledger-events.schema.json` | Core API (most events) / Ledger Gateway (governance events) | Rule Engine, Audit Agent |
| `finding.schema.json` | Rule Engine (`$defs/FindingSubmission`) / Core (stored form with signed reviews) | Audit Agent, Admin Web |
| `audit-report.schema.json` | Audit Agent (`$defs/AuditReportSubmission`) / Core (stored form) | Admin Web |
| `common.schema.json` | — | shared definitions |
| `hashing.md` | — | normative hash / Merkle / signature formulas |
| `chain-interface.md` | Ledger (smart contract) | Rule Engine, auditors — read anchors and state directly from a node |
| `examples/` | — | sample payloads (schema fixtures that also form one consistent example story; they are not a judged audit scenario); `tools/validate_contracts.py` checks them (also in CI) |

## Component map

```
 Voter Web ─┐                                 ┌─> Ledger Gateway ──> validator nodes (smart contract)
 Admin Web ─┼─ REST ─> Core API ─ outbox ─────┘        ▲ read-only queries
 (org keys  │            │  ▲                           │
  sign      │            │  └── /internal/v1 views ─────┼── Rule Engine (R1–R5) ──> POST findings
  approvals)│            │                              └── Audit Agent (read-only tools) ──> POST audit-reports
            └──── staff reads findings / reports / reviews via Core API
```

## Shared conventions

| Topic | Rule |
|---|---|
| IDs | Prefix + 26 Crockford-base32 chars. ULID (time-sortable) for: `elc_ cand_ evt_ act_ prp_ anc_ fnd_ rpt_ run_ usr_`. **Random (no timestamp)** for: `evr_` (voter-side events), `vtr_` (voter key), `rej_`, `att_`. `org_` uses a readable slug |
| Time | RFC 3339 UTC with `Z`, whole seconds. Voter-side times are **bucketed**: `floor(t / time_bucket_seconds) * time_bucket_seconds` (a voter event exactly on a boundary belongs to the bucket starting there) |
| Hash / Merkle / signatures | `hashing.md` (normative) |
| Voting token | 128-bit random, base64url (22 chars). Only `token_hash` is stored or sent anywhere |
| Ballot credential | 128-bit random, generated **in the voter's browser**; only `nullifier_hash` is sent at redeem time; the raw credential is sent once, with the ballot, and never stored |
| Errors | `application/problem+json` (RFC 9457) with `code` from `common.schema.json#/$defs/ProblemCode` |
| Versioning | URL prefix `/v1`. Additive changes keep `v1`; breaking changes need `v2` |
| Pagination | `?limit=` (default 100, max 1000) + opaque `?cursor=`; responses carry `next_cursor` (null at end) |
| Idempotency | `POST /v1/events` by `event_id`; voter endpoints by design (see Core API descriptions); staff create endpoints accept `Idempotency-Key` |
| Idempotent retries | Findings by `finding_id`, reports by `report_id`, approval decisions and finding reviews by (org, decision, signature), `submitAdminAction` by signature, `tally` when already TALLIED: identical resubmission → 200, different body → 409 |
| Voter-side storage | Rows about voters (token used, credential, ballot, outbox voter events) store only bucket and checkpoint values, never exact times; their primary keys are random, never serial. Physical insertion order remains visible to DB administrators (threat-model S2) |
| Timeouts / retries | 5 s per inter-service call. Outbox: RETRY_LATER retried every 1 s for the first 30 s, then exponential backoff up to 60 s, same `event_id` forever. Audit Agent: 10 s per tool call, max 20 steps, then `ABORTED` |
| Clocks | Core, Gateway and nodes sync with NTP; tolerated skew 2 s. Window decisions use bucketed times, so skew below one bucket cannot flip R2 |
| PII | Names, emails, external_ref, raw tokens and raw credentials never appear in ledger events, findings, reports, internal views, or logs. Logs also never contain token/nullifier/receipt hashes, `evr_` IDs, client IP addresses, User-Agent strings, the election private key, raw links or link CSV contents. Staff-only API responses (roster, `target_people`) are the only place names appear |

## Permission matrix (service keys / roles)

| Operation | Core API | Rule Engine (`ruleKey`) | Audit Agent (`agentKey`) | Gateway (`gatewayKey`) | Staff `admin` | Staff `approver` |
|---|---|---|---|---|---|---|
| Gateway `POST /v1/events`, `/v1/proposals*` (write) | ✅ | ❌ | ❌ | — | — | — |
| Gateway GET / `POST /v1/verify` (read; also `auditorKey`, issued to each participating org and invited auditors) | ✅ | ✅ | ✅ | — | — | — |
| Gateway `POST /v1/finding-records` (register finding hash) | ❌ | ✅ | ❌ | — | — | — |
| Gateway `POST /v1/finding-records/{id}/reviews` (relay signed review) | ✅ | ❌ | ❌ | — | — | — |
| Core `/internal/v1/views*`, `/checkpoints`, `/elections`, `GET findings` | — | ✅ | ✅ | ❌ | — | — |
| Core `/v1/elections/{id}/views/{view}`, alerts, findings, `roster:read` (auditors: read-only staff role `auditor`, no names) | — | ❌ | ❌ | ❌ | ✅ | ✅ |
| Core `POST /internal/v1/findings`, run requests | — | ✅ | ❌ | ❌ | — | — |
| Core `POST /internal/v1/audit-alerts` (own alert kinds only) | — | ✅ | ❌ | ✅ | — | — |
| Core `POST /internal/v1/audit-reports` | — | ❌ | ✅ (no `human_review`) | ❌ | — | — |
| Core read `/internal/v1/service-logs*` | — | ❌ | ✅ | ❌ | — | — |
| Core `POST /internal/v1/service-logs:append` (own service only) | — | ✅ | ❌ | ✅ | — | — |
| Election / roster / tokens / tally / finalize (`admin` exists only for operator_org staff) | — | ❌ | ❌ | ❌ | ✅ | ❌ |
| Create + submit admin action | — | ❌ | ❌ | ❌ | ✅ | ✅ |
| Approve / reject admin action (signed) | — | ❌ | ❌ | ❌ | ❌ | ✅ |
| Review finding (signed by the reviewer's org; operator_org reviews never count toward RESOLVED) | — | ❌ | ❌ | ❌ | ✅ | ✅ |
| Review AI report (`human_review`) | — | ❌ | ❌ | ❌ | ✅ | ✅ |

The Audit Agent has **no** operation that deletes or invalidates ballots, stops an election, changes results,
approves actions or reviews findings/reports. Its complete tool list is `audit-report.schema.json#/$defs/AgentTool`.

## Election state machine

| From → To | Trigger | Ledger event | Preconditions | Enforced by |
|---|---|---|---|---|
| (none) → DRAFT | admin `POST /v1/elections` | ELECTION_CREATED | window bucket-aligned; ≥3 registered orgs; 2 ≤ threshold ≤ orgs − 1 | Core + contract (registry) |
| DRAFT → ROSTER_FROZEN | admin `roster:freeze` | ROSTER_FROZEN | roster not empty | contract |
| ROSTER_FROZEN → OPEN | Core scheduler at `window_start` (block time not yet reached → RETRY_LATER; if REJECTED for another cause, Core may emit a new ELECTION_OPENED before window_end) | ELECTION_OPENED | every ACTIVE voter has a non-revoked token; if not, Core does not open, alerts, and opens at the first bucket boundary after the missing tokens are issued (if still before window_end) | contract (time), Core (tokens) |
| OPEN → CLOSED | scheduler at `window_end` (SCHEDULED), executed ELECTION_CLOSE_EARLY, or admin `:close` once every voter has voted (ALL_VOTED, decisions v1.5, no approval: every ACTIVE voter has a non-revoked token, every such token USED and every credential has a ballot; `closed_at` = floor_to_bucket(now) + bucket < window_end, so not in the last bucket; applied at once) | ELECTION_CLOSED | `closed_at` bucket-aligned; never in the past when the early-close action executes (a scheduled close delayed by a pending extension that is then rejected uses the old end) | contract |
| CLOSED → TALLIED | admin `tally` | TALLY_PUBLISHED | final_checkpoint = the CLOSE_FINAL checkpoint, which is ACCEPTED; outbox empty; no PENDING voter event ≤ final checkpoint | Core + contract (once only) |
| DRAFT, ROSTER_FROZEN, CLOSED or TALLIED (contract: also OPEN, e.g. after a rejected close) → VOIDED | executed ELECTION_VOID (multi-org approval) | ELECTION_VOIDED | cancel before voting, or the only exit when a finding is CONFIRMED_ISSUE or the tally cannot be completed; terminal | contract |
| TALLIED → FINALIZED | admin `finalize` | ELECTION_FINALIZED | FINDING_RECORDED for R1–R5 at the TALLY checkpoint; every FAIL/INCONCLUSIVE finding record RESOLVED (signed reviews of ≥ threshold participating orgs of the election other than operator_org) or SUPERSEDED; none CONFIRMED_ISSUE; result_hash equals tally | Core + contract (from ledger finding records) |

If window_end passes while the election is still ROSTER_FROZEN (it never opened), the only exit is ELECTION_VOID.
Everything else is refused (`INVALID_STATE_TRANSITION`): no backward moves; roster `PUT` only in DRAFT;
ROSTER_AMEND / TOKEN_REISSUE only in ROSTER_FROZEN or OPEN; WINDOW_CHANGE / ELECTION_CLOSE_EARLY follow the
execution-time rules in `core-api.openapi.yaml` (decideAdminAction) — a window shortening is strict (applied at once),
an extension permissive (applied after ACCEPTED; the contract rejects it once the current end has passed); nothing changes after
FINALIZED or VOIDED — these transitions are the last events of an election and are not followed by a checkpoint.
A USED token can never be re-enabled or revoked.

## Checkpoints

A checkpoint is the common reference point for audits and the unit of coarse time in views.

- Core keeps an *open* checkpoint `k` per election. Every Core ledger event carries `checkpoint = k`.
- Core commits `k` every `checkpoint_interval_seconds` while OPEN (PERIODIC; an integer multiple of `time_bucket_seconds`, on bucket
  boundaries, so checkpoint numbers are never finer than buckets), on every state transition
  (STATE_CHANGE), once after close when all voter events are in the outbox (CLOSE_FINAL), and at tally (TALLY).
  CLOSED → TALLIED commits a single checkpoint with reason TALLY. Committing emits `CHECKPOINT_COMMITTED{checkpoint: k}` with the snapshot hash of all six tables and the counts, then opens `k+1`.
- **As-of rule:** every field of every view row is a function of the data as of checkpoint `k`. Facts that change
  later (token used/revoked, credential used, action status, voter removed) are stored with the checkpoint in which
  they changed, and the view derives the value at `k` from them. Delivery status to the ledger is *not* a view field.
  Hence `GET /internal/v1/views/{v}?checkpoint=k` for a committed `k` always returns the same rows, and its
  `snapshot_hash` must equal the committed value unless stored data was altered.
- Views are always read as of a committed checkpoint; rows carry checkpoint numbers instead of times.
- **Boundary serialization:** Core commits a checkpoint (and performs a scheduled close) at its bucket boundary T under the
  same lock that guards voter acceptance. A request is timestamped when it takes the lock: a request with t < T accepted
  before the commit gets checkpoint k and a bucket ending at or before T; a request with t ≥ T first waits until the
  boundary commit / close at T has been done, then gets checkpoint k+1 and the bucket starting at T — or is refused
  (ELECTION_NOT_OPEN) if t ≥ effective_end. So a voter event's bucket never lies
  after the end of its checkpoint, and the delivery-order guarantee below holds.
- `CHECKPOINT_COMMITTED.voter_events` is defined from the views, not from the outbox: `TOKEN_USED` = token_status rows with
  `used_checkpoint = k`; `BALLOT_RECEIVED` = ballot_receipts rows with `received_checkpoint = k`. R3 recomputes both.

## Consistency model (Core DB ↔ ledger)

- Core never assumes one atomic transaction across Core DB and the chain. Audit-relevant changes are written
  to the outbox in the same DB transaction; the outbox worker delivers them (at-least-once, same `event_id`).
- **Delivery order:** the release unit is the time bucket. At the end of bucket b the outbox worker sends (1) the voter
  events of b, shuffled, then (2) the system events of b in seq order; units go in bucket order and the next request is
  sent only after every event of the previous one has a final outcome. The Gateway cannot place a system event inside a bucket.
  An event created exactly on a bucket boundary belongs to the bucket ending there, so boundary-scheduled transitions
  (ELECTION_OPENED, ELECTION_CLOSED, periodic checkpoints) are released without delay.
  So when CHECKPOINT_COMMITTED `k` is ACCEPTED, every voter event with checkpoint ≤ k already has a final outcome
  (accepted event or rejection record carrying its checkpoint).
- System events carry a gap-free `seq` per (election, source); the Gateway applies core events in order and answers
  RETRY_LATER on gaps. A rejected system event still consumes its seq. Voter events have no `seq`; RETRY_LATER until ELECTION_OPENED is applied.
- Outcome handling in Core: ACCEPTED / DUPLICATE → done; RETRY_LATER → retry; CONFLICT → alert, stop the FIFO;
  REJECTED CHECKPOINT_COMMITTED → the checkpoint stays committed in Core but is marked LEDGER_REJECTED and alerted; R5 FAILs
  on it (`rejected_checkpoints`). REJECTED permissive WINDOW_CHANGED (extension) → the old end stays in force, alert;
  REJECTED ELECTION_CREATED → the election exists only in Core (DRAFT); Core marks it LEDGER_REJECTED, alerts and
  lets the admin delete it (it was never on chain, so no VOID is needed); REJECTED ROSTER_AMENDED, TOKEN_BATCH_ISSUED, TOKEN_REVOKED, ADMIN_ACTION_EXECUTED or ELECTION_VOIDED →
  Core stops that election's FIFO, alerts, and a human resolves it (the only automatic exit is ELECTION_VOID);
  REJECTED strict WINDOW_CHANGED (shortening) → Core keeps the shorter end (never re-opens), alerts, human resolves;
  REJECTED ADMIN_ACTION_FAILED → alert and FIFO stop like other core events;
  REJECTED TOKEN_USED → the token stays USED in Core and its credential's ballot is still counted (they cannot be tied);
  the mismatch is reported by R5's row-to-ledger match (token_status row used without an accepted TOKEN_USED);
  REJECTED →
  - permissive transition (OPEN, FINALIZED — both applied only after ACCEPTED): Core reverts it (OPEN: no ballots;
    FINALIZED: stays TALLIED) and alerts;
  - strict transition (ROSTER_FROZEN, CLOSED) or TALLY: Core keeps the strict state and alerts — it
    never re-opens voting or roster editing because of a ledger rejection; a human resolves the mismatch;
  - voter event: the ballot is excluded from the tally (`excluded_ballots`) and listed for review; R1/R2 report it.
- The Gateway rejects content duplicates (DUPLICATE_CONTENT), TOKEN_USED for revoked tokens and TOKEN_REVOKED for
  used tokens, so a buggy or malicious Core cannot double-count through the ledger.
- Every Gateway rejection is kept in `/v1/rejections` (with checkpoint) and covered by the next anchor's
  `rejections_root`; every refused voter/staff request at Core is kept in the `rejected_attempts` view.

## Ledger event types (`ledger-events.schema.json`)

| Type | Source | Emitted when | Anchoring |
|---|---|---|---|
| ELECTION_CREATED | core | election created | immediate |
| ROSTER_FROZEN | core | roster locked | immediate |
| ROSTER_AMENDED | core | approved ROSTER_AMEND executed | immediate |
| TOKEN_BATCH_ISSUED | core | `tokens:issue` | immediate |
| TOKEN_REVOKED | core | unused token revoked (voter removed, or TOKEN_REISSUE) | immediate |
| TOKEN_USED | core | voter redeems a link | **batched**, bucketed time, `evr_` ID |
| BALLOT_RECEIVED | core | ballot accepted | **batched**, bucketed time, `evr_` ID |
| ADMIN_ACTION_REQUESTED | gateway | signed proposal opened | immediate |
| APPROVAL_RECORDED | gateway | an org's signed APPROVE / REJECT | immediate |
| PROPOSAL_CLOSED | gateway | proposal APPROVED / REJECTED / EXPIRED | immediate |
| ADMIN_ACTION_EXECUTED / ADMIN_ACTION_FAILED | core | approved action applied / refused at execution | immediate |
| ELECTION_OPENED / ELECTION_CLOSED | core | window start / end or early close | immediate |
| WINDOW_CHANGED | core | approved window change executed | immediate |
| CHECKPOINT_COMMITTED | core | checkpoint committed | immediate |
| FINDING_RECORDED | gateway | Rule Engine registers a finding hash | immediate |
| FINDING_REVIEWED | gateway | an org's signed review of a finding | immediate |
| TALLY_PUBLISHED / ELECTION_FINALIZED / ELECTION_VOIDED | core | tally / finalization / voiding | immediate |

## Evidence references

`EvidenceRef.ref` format per kind (enforced by `common.schema.json`; Core resolves every ref before storing a report):

| kind | ref | Resolved with |
|---|---|---|
| ledger_event | `evt_…` / `evr_…` | Gateway `GET /v1/events/{id}` |
| ledger_rejection | `rej_…` | Gateway `GET /v1/rejections/{id}` |
| anchor / proposal / finding | `anc_…` / `prp_…` / `fnd_…` | corresponding GET |
| view_row | `<view>:<row_key>@<checkpoint>` e.g. `ballot_receipts:<nullifier_hash>@12` | Core `GET /internal/v1/views/{view}/rows/{row_key}?checkpoint=` |
| snapshot | `<table>@<checkpoint>` | Core view `snapshot_hash` + the CHECKPOINT_COMMITTED event |
| service_log | `<service>:<log_id>` | Core `GET /internal/v1/service-logs/{service}/{log_id}` |

Row keys: election_config → `config`; roster → voter_key; token_status → token_hash; credential_issuance and
ballot_receipts → nullifier_hash; admin_actions → action_id; rejected_attempts → attempt_id.

## Audit rules (Rule Engine contract)

Each run evaluates one rule for one election **at one committed checkpoint `k`**. The Rule Engine registers the
finding hash at the Gateway (`POST /v1/finding-records` → FINDING_RECORDED) and then stores the body at Core.
Inputs: views read with `?checkpoint=k` (and with every committed checkpoint ≤ k for R5); ledger events (core and gateway) with `checkpoint_max=k`; Gateway rejections
with checkpoint ≤ k; anchors, organization registry and election state read **directly from a node** via
`chain-interface.md`, using the pinned chain configuration (contract address and a node not run by the Gateway operator).

**Discovering work:** the Rule Engine polls `GET /internal/v1/elections` and `/internal/v1/checkpoints` (plus manual run
requests) to find elections and newly committed checkpoints.

**When rules run:** for checkpoint `k`, only after CHECKPOINT_COMMITTED `k` is ACCEPTED and every event with checkpoint ≤ k is
anchored — or, at the latest, 2 × batch interval + 60 s after the CHECKPOINT_COMMITTED was accepted (then unanchored events give
INCONCLUSIVE `BATCH_NOT_ANCHORED`). If an INCONCLUSIVE cause clears, the Rule Engine re-runs the same checkpoint; that later PASS
supersedes it (the only way at the TALLY checkpoint). Before that — or if the Gateway is unreachable before the rule has read its inputs —
the Rule Engine produces **no finding** and raises an alert (`POST /internal/v1/audit-alerts`, AUDIT_DELAYED /
LEDGER_UNAVAILABLE); alerts need no review and do not block finalization. Every rule at `k` evaluates the cumulative data
of all checkpoints ≤ k. BATCH_NOT_ANCHORED concerns only checks that need anchors (R5); the other rules evaluate the stored events. A manual re-run that produces an identical result (same rule, checkpoint and observed values) is not recorded again. Chain data counts only from confirmed blocks (chain-interface.md "Confirmations"); an item the pinned node does not show as confirmed yet is treated as not anchored / not available, never as a mismatch. Missing or unreachable evidence is never PASS (INCONCLUSIVE: BATCH_NOT_ANCHORED, NODE_UNAVAILABLE —
for any rule that reads the chain directly, e.g. R4 registry and R5 anchors —, VIEW_UNAVAILABLE, KEY_NOT_REVEALED). If the Gateway becomes unreachable after a rule has read its inputs and produced its finding, the Rule
Engine registers the record directly on chain (`submitFindingRecord`) and stores the body at Core once the record is
indexed (503 until then). The Rule Engine validates each finding body against FindingSubmission and stores it durably before registering it.
Because of the delivery-order rule, no grace period is needed for counts: at that point every voter event ≤ k is
either an accepted event or a rejection record.

| Rule | FAIL when | Not a FAIL (reported in `observed`) | `observed` keys |
|---|---|---|---|
| **R1** duplicate / state | a token_hash in >1 *accepted* TOKEN_USED; a nullifier_hash in >1 accepted BALLOT_RECEIVED; TOKEN_USED for a token not in token_status, or revoked before the use; TOKEN_REVOKED for a token that has an accepted TOKEN_USED (either order), or a token_status row with both used_checkpoint and revoked_checkpoint; BALLOT_RECEIVED whose nullifier is not in credential_issuance | refused duplicates (Gateway DUPLICATE_CONTENT / TOKEN_REVOKED / TOKEN_ALREADY_USED, rejected_attempts TOKEN_ALREADY_USED / CREDENTIAL_ALREADY_USED) | `accepted_duplicate_tokens`, `accepted_duplicate_nullifiers`, `unknown_tokens`, `unknown_nullifiers`, `used_then_revoked`, `refused_duplicates` |
| **R2** time | an accepted voter event with `occurred_at < window_start` or `occurred_at + time_bucket_seconds > effective_end` (effective_end = the new_window_end of the last accepted WINDOW_CHANGED (else window_end), capped by ELECTION_CLOSED.closed_at or the effective closed_at of an executed ELECTION_CLOSE_EARLY if earlier); a Gateway rejection of a voter-side event with reject_code ELECTION_NOT_OPEN (Core accepted a request outside the window); any voter event, or rejection of a voter-side event, with checkpoint greater than the CLOSE_FINAL checkpoint | refused ELECTION_NOT_OPEN attempts at Core (`rejected_attempts`) | `events_before_start`, `events_after_end`, `ledger_rejected_window`, `events_after_close_final`, `refused_not_open` |
| **R3** count (at `k`) | `token_used ≠ credentials_issued` (redeem is atomic); `ballots_received > credentials_issued`; `credentials_issued > roster_active`; `tokens_active > roster_active`; view counts ≠ CHECKPOINT_COMMITTED counts; `voter_events` of any checkpoint ≤ k ≠ the same count recomputed from the views; **for each voter event type: accepted ledger events + Gateway rejections with checkpoint ≤ k ≠ cumulative view count** (token_status used ≤ k / ballot_receipts ≤ k). At the TALLY checkpoint additionally: `TALLY.final_checkpoint ≠` the checkpoint committed with reason CLOSE_FINAL; `TALLY.ballot_count + invalid_ballots ≠ accepted BALLOT_RECEIVED ≤ final_checkpoint`; `excluded_ballots ≠ rejected BALLOT_RECEIVED ≤ final_checkpoint` | `credentials_issued − ballots_received` (redeemed, not yet voted) | `roster_active`, `tokens_active`, `token_used`, `credentials_issued`, `ballots_received`, `ledger_token_used`, `ledger_ballots`, `ledger_rejected_voter_events`, `redeemed_not_voted` (+ `tally_*` at TALLY) |
| **R4** admin action | an ELECTION_CLOSED(ALL_VOTED) when, at the CLOSE_FINAL checkpoint, `tokens_active ≠ roster_active`, `token_used ≠ tokens_active` or `ballots_received ≠ credentials_issued`, or whose `closed_at ≠ floor_to_bucket(occurred_at) + time_bucket_seconds` or `closed_at ≥` the effective window_end (ALL_VOTED needs no proposal, decisions v1.5); any ROSTER_AMENDED, TOKEN_REVOKED, WINDOW_CHANGED or ELECTION_CLOSED(EARLY) without an ADMIN_ACTION_EXECUTED whose proposal is APPROVED with a valid request signature and ≥ threshold valid APPROVE signatures from distinct participating orgs of the election (ELECTION_CREATED.organizations) other than the requester (keys from the on-chain registry); any decision or review by a non-participating org counted and matching target_hash; **more than one ADMIN_ACTION_EXECUTED per proposal**; an ADMIN_ACTION_EXECUTED of ROSTER_AMEND / TOKEN_REISSUE / WINDOW_CHANGE / ELECTION_VOID without its effect events in its effect window, or of ELECTION_CLOSE_EARLY without ELECTION_CLOSED(EARLY) by one bucket after its effective closed_at, unless the election was already closed earlier for another reason (`missing_effects`); **executed effect ≠ approved target as sets**: roster rows added / removed in the execution checkpoint ≠ `target.add` / `target.remove` voter_keys, `roster_hash_after ≠ set_root(H(voter_key) of ACTIVE roster rows after execution)`, TOKEN_REVOKED voter_keys ≠ (target.remove voter_keys that had an ISSUED token at execution) ∪ TOKEN_REISSUE target voter_keys (an add-only amendment has no revocations; several amendments in one checkpoint are checked one by one in seq order via their roster_hash_before/after chain); evaluating system events in seq order: right after each TOKEN_BATCH_ISSUED, non-revoked tokens ≠ ACTIVE roster rows at that moment (each active voter then has exactly one live token), or at any checkpoint non-revoked tokens > ACTIVE roster rows (voters added but not yet issued are allowed); roster_hash chain broken (ROSTER_FROZEN → ROSTER_AMENDED before/after → roster snapshot); roster or token_status rows changed between checkpoints without a matching event; ELECTION_CREATED orgs or operator_org ≠ registry; recomputed target_hash (from admin_actions.target) ≠ proposal target_hash; **WINDOW_CHANGED.new_window_end ≠ target.new_window_end** or old_window_end ≠ the effective end before it; **ELECTION_CLOSED(EARLY): requested_closed_at ≠ target.closed_at or closed_at ≠ max(requested_closed_at, ceil_to_bucket(ADMIN_ACTION_EXECUTED.occurred_at))**; ELECTION_VOIDED without an executed ELECTION_VOID | proposals rejected / expired; actions FAILED at execution | `unapproved_changes`, `invalid_signatures`, `duplicate_executions`, `missing_effects`, `target_mismatches`, `roster_chain_breaks`, `executed_actions`, `failed_actions` |
| **R5** integrity | for **every committed checkpoint j ≤ k**: recomputed `snapshot_hash(view, j)` ≠ the CHECKPOINT_COMMITTED j value (so a later change to an already-committed row is caught); **row-level match with the ledger**: every ballot_receipts row (nullifier_hash, ballot_hash, receipt_hash, received_bucket, event_id) ≠ the accepted BALLOT_RECEIVED with that event_id, every accepted BALLOT_RECEIVED without its row, every token_status row used ≤ k without an accepted TOKEN_USED of that token_hash (and vice versa); a ballot_receipts row with `H(sealed_ballot) ≠ ballot_hash`; recomputed roster_hash / token_set_root ≠ latest ROSTER_* / TOKEN_* value; recomputed config_hash (using config_window_start / config_window_end) / public_key_hash ≠ ELECTION_CREATED; recomputed payload_hash or leaf_hash ≠ stored; recomputed Merkle root or rejections_root ≠ the value read **from a node** (`chain-interface.md` anchorAt). At the TALLY checkpoint additionally: `TALLY.final_checkpoint ≠` the CLOSE_FINAL checkpoint; recomputed `ballot_set_root` from accepted BALLOT_RECEIVED ≤ final ≠ TALLY value, or ≠ set_root of view ballot_hash ≤ final; re-decrypting the sealed_ballot of every ledger-accepted ballot (rows matched to the ledger as above) with the revealed private key gives results ≠ TALLY results, or a different invalid set (valid plaintext = UTF-8 JSON exactly `{"candidate_id": <a candidate of the election>}`); recomputed result_hash ≠ TALLY; `electionStateAtCheckpoint(k)` (status, windowEnd, rosterHash, tokenSetRoot, resultHash) ≠ the state derived from the ledger core events with seq ≤ its lastCoreSeq (derivation table in chain-interface.md) → `chain_state_mismatch` (catches a Gateway calling the contract with other arguments; unaffected by later chain progress); the immutable creation fields read from a node (`electionState`: configHash, windowStart, timeBucketSeconds; `electionGovernance`: orgIds, approvalThreshold, operatorOrgId, proposalTtlHours, checkpointIntervalSeconds) ≠ ELECTION_CREATED → `governance_mismatch` (windowEnd is not compared here); the revealed private key does not match public_key_hash (or does not derive the public key); any checkpoint whose CHECKPOINT_COMMITTED the Gateway rejected (`rejected_checkpoints`); PERIODIC CHECKPOINT_COMMITTED events not on multiples of checkpoint_interval_seconds from ELECTION_OPENED.occurred_at (the actual opening time), or more than one per interval (`checkpoint_cadence_violations`). If the private key is not revealed at the TALLY checkpoint → INCONCLUSIVE `KEY_NOT_REVEALED`. (ELECTION_FINALIZED.result_hash = tally is enforced by the contract, since no checkpoint follows FINALIZED) | events not anchored when the rule runs, or no node reachable → INCONCLUSIVE (`BATCH_NOT_ANCHORED` / `NODE_UNAVAILABLE`) | `rejected_checkpoints`, `chain_state_mismatch`, `governance_mismatch`, `checkpoint_cadence_violations`, `row_ledger_mismatches`, `key_mismatch`, `tables_mismatched`, `sealed_hash_mismatches`, `root_mismatches`, `config_mismatch`, `events_hash_mismatch`, `anchors_mismatch`, `events_unanchored` (+ `tally_*` at TALLY) |

**Repeated anomalies.** Because every run is cumulative, a persisting anomaly produces a FAIL at each later checkpoint.
In the MVP each FAIL needs its own signed reviews; the Rule Engine sets `previous_finding_id` to the earlier FAIL of the
same rule so reviewers can process repeats quickly, and experiment counts use distinct anomalies, not findings.

**Review and supersession.** FAIL / INCONCLUSIVE findings need signed reviews (see Finding.review). An INCONCLUSIVE
finding is SUPERSEDED (no review needed) once a finding of the same rule at a strictly higher committed checkpoint is
PASS, or a PASS of the same rule at the same checkpoint recorded later (re-run after the cause cleared): every rule's later evaluation covers the cumulative data (R5 explicitly re-checks all earlier checkpoints). FAIL
findings are never superseded, and any CONFIRMED_ISSUE review overrides SUPERSEDED / RESOLVED.

## Change process

- Contracts change only through pull requests. No mandatory reviewer, but mention (@) the owners of affected
  components in the PR and add one line to `CHANGELOG.md`.
- Run `python tools/validate_contracts.py` before pushing; CI (`.github/workflows/contracts.yml`) runs it on every push and PR.
