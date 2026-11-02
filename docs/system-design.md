# PureVoting 2.0 — System Design

> Date 2026-10-18 · Based on `contracts/` **v1.0 (frozen)** · Author: Seungmin (team lead)
> Team: Seungmin (Core API, web, integration), Harits (ledger, smart contract, Gateway), Anik (Rule Engine, AI Audit Agent)
>
> This document gives the overall picture of the system and how the team works. The detailed rules between components
> (API fields, hash computation, state transition conditions) are defined in **`contracts/`, which is the source of truth**;
> if this document and the contracts disagree, follow the contracts. This English version is the reference design document;
> `system-design.ko.md` is a translation for convenience (D-19).

---

## 0. Reading order

| Who | Read first | Then |
|---|---|---|
| Everyone | Sections 1–3 and 7–10 of this document | `contracts/README.md` (shared conventions, state machine, checkpoints) |
| Seungmin | Section 8.1, then `docs/roles/core-web.md` | `core-api.openapi.yaml`, `hashing.md`, `ledger-events.schema.json` |
| Harits | Section 8.2, then `docs/roles/ledger.md` | `chain-interface.md`, `ledger-gateway.openapi.yaml`, `hashing.md` |
| Anik | Section 8.3, then `docs/roles/rules-ai.md` | `contracts/README.md` "Audit rules", `finding.schema.json`, `audit-report.schema.json`, `chain-interface.md` "How R5 uses it" |

---

## 1. Goal and scope

**In one sentence:** an online voting system with one-time links. Every audit-relevant record goes to an audit ledger
(blockchain) run jointly by several organizations; a Rule Engine judges first, and an AI agent assists by collecting evidence.

| Item | Content |
|---|---|
| Duration | 2 months (8 weeks) |
| Voting method | Single choice (one person, one vote, one candidate) |
| What we claim | ① An administrator change (roster, window, …) made without multi-organization signed approval is detected. ② Later changes to rows of an already committed checkpoint and tally manipulation are detected as FAIL by the Rule Engine (outbox omissions: only if T09 is run). ③ Views, ledger and logs contain no join key between voter and ballot; time linkage is limited to the bucket / checkpoint, with ≥ 10 expected voters per bucket (D-10) — fewer actual voters per bucket, or a reissued / added voter, can still be linked (threat model S1, S7). |
| What we do not claim | Ballot secrecy against the election commission (it operates the DB and holds the private key). Coercion resistance (not receipt-free). Detection of a Core insider casting ballots for abstainers. Prevention of delivery delays by the Gateway (delays are only detected). Prevention of proxy voting. End-to-end verifiability. Independent multi-organization operation (one person runs all orgs in the demo, D-21). "The smart contract rejects each late ballot" (D-22). Any optional feature (direct submissions, AI) that was not run. Full list: threat model "Claims we must NOT make in the MVP". |
| Designed but not implemented | Threshold decryption, blind signatures and other advanced cryptography |

---

## 2. Architecture

### 2.1 Component diagram

```
                         ┌──── Gateway: neutral org · Rule Engine, AI: observer ────────┐
 Voter web ─┐            │                                                              │
            ├─ REST ─> Core API ── outbox (FIFO) ──> Ledger Gateway ──> validator nodes (smart contract)
 Admin web ─┘ (commission) │  ▲                       ▲ query API           ▲ direct reads (observer's pinned node)
 (org approvers           │  └─ /internal/v1 views ────┼──── Rule Engine (R1–R5) ┘──> register finding, store body
  sign with own keys)     │                            └──── AI Audit Agent (read-only) ──> report
                         └── staff read findings, reports and alerts via Core API and sign reviews
```

### 2.2 Components

| Component | Owner | Responsibility | Operated by (demo) |
|---|---|---|---|
| Voter web | Seungmin | Use the link, **generate the ballot credential in the browser**, seal and submit the ballot, check the receipt | Commission |
| Admin web | Seungmin | Create elections, roster, issue links, request and sign approvals, review findings, tally, finalize | Commission (approvers belong to each org) |
| Core API | Seungmin | Election state machine, DB, outbox, checkpoints, views, tally | Commission |
| Ledger Gateway | Harits | Validate and record ledger events, batch-anchor voter events, relay proposals and approvals, query API | Neutral org |
| Smart contract | Harits | Organization registry, election state, approval quorum, finding records and reviews, finalization conditions | Validator nodes (3, one per organization, D-03) |
| Rule Engine | Anik | Evaluate R1–R5 at every checkpoint, sign and register findings | Observer (D-25, v1.1) |
| AI Audit Agent | Anik | Collect evidence with read-only tools and write reports (optional feature) | Observer (D-25, v1.1) |

### Technology stack

| Item | Choice | Decision | Cost |
|---|---|---|---|
| Servers (backend) | Python 3.11 + FastAPI | D-05 | free |
| Web | TypeScript + React | D-05 | free |
| Database | PostgreSQL (one database, two schemas) | D-06 | free |
| Where it runs | Each developer's PC with `docker compose up`; the demo runs on one PC (no cloud) | section 7 #6, section 10 | free |
| Blockchain | Private PureChain network (go-ethereum fork, EVM; chosen by Harits, v1.1) | D-02 | free |
| Mail | MailHog (fake mailbox) in development, CSV export in the demo | D-16 | free |
| Repository / CI | GitHub monorepo + GitHub Actions | D-18 | free |
| Mock servers | prism (from the OpenAPI files) | section 7 #4 | free |
| AI model | Chosen by Anik (recommended: Claude API); the AI agent is optional | D-15 | owner's choice (API usage may cost money) |

Harits and Anik only have to keep the chain interface in `chain-interface.md` and the Gateway API; the platform and the
model are theirs to choose.

### 2.3 Demo organizations

| Organization | Role |
|---|---|
| Commission (`org_commission`) | Operator org. Runs Core and the web, creates elections, tallies. Excluded from the finding-review quorum |
| Observer (`org_observer`) | Runs the Rule Engine and AI (D-25, v1.1); approves admin actions, reviews findings, runs the **pinned node** (the node Core and the Rule Engine read directly) |
| Neutral org (`org_neutral`) | Runs the Gateway; approves admin actions, reviews findings |

- 3 organizations, approval threshold 2: an admin action needs signatures from 2 organizations **other than the requester**;
  a finding review needs signatures from 2 organizations other than the commission (observer and neutral org). Same as the
  common setup in `docs/test-plan.md`.
- Validators: 3, one per organization (D-03). Pinned nodes: the observer's first, the commission's second — never the
  Gateway operator's (neutral org) — `chain-interface.md`, D-26.
- All organizations run in docker on one PC. That they are not really independent is disclosed in the threat model.

---

## 3. Main flows

### 3.1 Election states

```
DRAFT ──freeze roster──> ROSTER_FROZEN ──window start──> OPEN ──window end / early close──> CLOSED ──tally──> TALLIED ──finalize──> FINALIZED
  └────────────── void with multi-org approval (ELECTION_VOID) ──────────────> VOIDED
```

- **Strict transitions** (freeze roster, close, window shortening): Core applies them at once. If the ledger rejects them,
  Core does not roll back; it raises an alert and a human resolves it.
- **Permissive transitions** (open, finalize, window extension): applied only after the ledger answers ACCEPTED.

### 3.2 Preparing an election
1. An admin creates the election. Core sends `ELECTION_CREATED` and the Gateway records it in the contract.
2. **Creation check:** Core reads the pinned node directly and compares the participating organizations, threshold and the
   other creation fields stored in the contract with the ledger event.
   - The election can move on only after it passes (`creation_check = PASSED`).
   - On a mismatch the election is abandoned and deleted at Core.
3. Register and freeze the roster. Freezing checks that there are at least 10 expected voters per time bucket.
4. Issue links. Raw tokens leave only as CSV or email; the server keeps only their hashes.

### 3.3 Voting
```
Voter ──clicks link──> Voter web: browser generates the ballot credential (128-bit random)
      ──redeem(token, nullifier_hash)──> Core: mark token used + register credential hash (one transaction)
      ──ballot(credential, sealed ballot)──> Core: store ballot, issue receipt
Core ──outbox──> Gateway: TOKEN_USED, BALLOT_RECEIVED (random evr_ IDs, bucketed times, shuffled batches)
Gateway ──> contract: anchor the batch Merkle root → receipt status ANCHORED
```
- Token use and ballots are stored in different DB schemas, with no key that joins the two records.

### 3.4 Ledger recording and checkpoints
- Core writes DB changes and outbox records in one transaction. Delivery is at-least-once and `event_id` never changes.
- System events carry a per-election sequence number (`seq`); the Gateway applies them strictly in order.
- **Checkpoints** are the reference points for audits.
  - They are committed periodically (an integer multiple of the time bucket), on every state change, right after closing
    (CLOSE_FINAL) and at tally (TALLY).
  - A commit records the snapshot hashes and counts of the six tables on the ledger.
  - Views are always read "as of checkpoint k"; reading the same k always returns the same result.

### 3.5 Approving admin actions
1. An admin of the requesting organization drafts the action (roster amendment, void, …), signs it with the
   organization's key and submits it. A proposal is opened on the ledger.
2. Approvers of other organizations check the target on screen; their client recomputes the hash before they sign.
3. When the threshold is reached the proposal becomes APPROVED. Core executes it and sends `ADMIN_ACTION_EXECUTED`
   followed by its effect events.
- Each organization registers its own signing key in the on-chain registry. The commission cannot set another
  organization's key.

### 3.6 Auditing
1. Once a checkpoint is applied on the ledger and its events are anchored, the Rule Engine runs R1–R5.
2. It signs each result (finding) and registers it on the ledger; the body is stored at Core.
3. A FAIL or INCONCLUSIVE finding is resolved only by signed reviews from 2 organizations other than the commission.
   An INCONCLUSIVE finding superseded by a later PASS of the same rule also counts as resolved.
4. If a rule cannot run (ledger behind, Gateway unreachable), only an alert is recorded instead of a finding.

| Rule | What it checks |
|---|---|
| R1 duplicates / state | Duplicate acceptance of the same token or credential, unknown tokens, use after revocation |
| R2 time | Accepted events outside the effective voting window |
| R3 counts | Relations between roster, token, credential and ballot counts; view vs ledger counts; tally counts |
| R4 admin actions | Unapproved changes, forged signatures, duplicate executions, executed result ≠ approved target |
| R5 integrity | Recomputing snapshots of past checkpoints, row-level match with the ledger, anchor roots, re-decryption with the revealed key, contract state comparison |

### 3.7 Tally and finalization
1. After closing and once the outbox is empty, the tally runs. Only ballots accepted on the ledger are counted.
2. The commission reveals the private key. The Rule Engine (R5) re-decrypts and checks the result.
3. The contract itself checks the finalization conditions:
   - R1–R5 findings at the TALLY checkpoint are all recorded;
   - no unresolved FAIL / INCONCLUSIVE remains;
   - no CONFIRMED_ISSUE exists.
4. If a problem is confirmed, the only exit is voiding (VOID).

---

## 4. Data design

### 4.1 Core DB (one PostgreSQL database, two schemas)

| Schema | Tables | Note |
|---|---|---|
| credential | `roster`, `token_status`, `admin_actions` (election settings are stored here too but are **not snapshotted**; they are verified through `config_hash`) | Elections, roster, token hashes, admin work |
| ballot | `credential_issuance`, `ballot_receipts` | Credential hashes, sealed ballots, receipts. No common key with the credential schema |

The six snapshot tables are exactly `common.schema.json#/$defs/SnapshotTable`: `roster`, `token_status`,
`credential_issuance`, `ballot_receipts`, `admin_actions`, **`rejected_attempts`**. `rejected_attempts` (and the outbox)
may live in either schema, but `rejected_attempts` is snapshotted at every checkpoint; `election_config` is not.

- Marking the token used and registering the credential happen in one transaction.
- Voter-related rows store only time buckets and checkpoints, never exact times; their primary keys are random.
- The two schemas use different DB privileges. A DB administrator can still link them; this limit is disclosed.
- The Rule Engine and the AI never access the DB; they use the view API only.

### 4.2 Views (what the Rule Engine and the AI read)
`election_config`, `roster`, `token_status`, `credential_issuance`, `ballot_receipts`, `admin_actions`, `rejected_attempts`.
Always read with `?checkpoint=k`. No names or personal data.

### 4.3 Ledger events (`ledger-events.schema.json`)

| Group | Events |
|---|---|
| Election state | ELECTION_CREATED, ROSTER_FROZEN, ELECTION_OPENED, ELECTION_CLOSED, TALLY_PUBLISHED, ELECTION_FINALIZED, ELECTION_VOIDED |
| Roster / tokens | ROSTER_AMENDED, TOKEN_BATCH_ISSUED, TOKEN_REVOKED |
| Voting (batch-anchored) | TOKEN_USED, BALLOT_RECEIVED |
| Admin actions | ADMIN_ACTION_REQUESTED, APPROVAL_RECORDED, PROPOSAL_CLOSED, ADMIN_ACTION_EXECUTED / FAILED, WINDOW_CHANGED |
| Audit | CHECKPOINT_COMMITTED, FINDING_RECORDED, FINDING_REVIEWED |

### 4.4 Shared conventions (summary)
- **IDs:** prefix + 26 characters. System objects use ULIDs; voter-side IDs (`evr_`, `vtr_`, …) are random, with no timestamp.
- **Time:** UTC, whole seconds. Voter-side times are rounded down to the time bucket.
- **Hashes / signatures:** SHA-256 over RFC 8785 (JCS) canonical JSON. Signature digests use a fixed 32-byte layout and are
  signed with EIP-191 secp256k1 (EVM: PureChain, D-02). Details in `hashing.md`.
- **Errors:** `application/problem+json`, with codes from `ProblemCode` in `common.schema.json`.

---

## 5. Interfaces (summary)

| Interface | File | Main paths |
|---|---|---|
| Core API (web, staff) | `core-api.openapi.yaml` | `/v1/credentials:redeem`, `/v1/ballots`, `/v1/elections/*` (create, roster, issue, tally, finalize), `/v1/admin-actions/*`, `/v1/findings/*` |
| Core API (internal services) | same file | `/internal/v1/elections`, `/checkpoints`, `/views/{view}`, `/findings`, `/audit-alerts`, `/audit-reports`, `/service-logs` |
| Gateway API | `ledger-gateway.openapi.yaml` | `/v1/events`, `/v1/proposals/*`, `/v1/finding-records/*`, `/v1/anchors/*`, `/v1/rejections`, `/v1/elections/{id}/state`, `/v1/chain` |
| Smart contract | `chain-interface.md` | Registry, `electionState` / `electionGovernance` / `electionStateAtCheckpoint`, Gateway write functions, direct-submission functions |
| Audit results | `finding.schema.json`, `audit-report.schema.json` | Finding body, AI report and tool list |

**Permissions (summary):** each service has its own key (`coreKey`, `ruleKey`, `agentKey`, `gatewayKey`); staff are
distinguished by JWT roles (`admin`, `approver`, `auditor`). The AI agent can only read and write reports. The full table is
"Permission matrix" in `contracts/README.md`.

---

## 6. Security and anonymity (summary)
- Separating voters from ballots: browser-generated credential, random IDs, time buckets, shuffled batch delivery,
  separate schemas.
- Not trusting the Gateway:
  - signed approvals, reviews and findings cannot be forged, and signers can submit directly on chain;
  - the contract address and the pinned node are fixed in the deployment configuration;
  - Core and R5 compare the creation fields.
- Remaining risks are disclosed in `docs/threat-model.md`. In presentations, do not go beyond "What we do not claim"
  (section 1).

---

## 7. Team decisions (final)

| # | Topic | Decision |
|---|---|---|
| 1 | Repository | One GitHub monorepo. `core/`, `web/` = Seungmin; `ledger/` = Harits; `rules/` = Anik; `contracts/` = shared. Change your own folder freely; only `contracts/` changes go through PRs |
| 2 | Contract freeze | Frozen at v1.0. Changed only by PR when implementation is blocked (@mention the owners + one CHANGELOG line, no mandatory approval) |
| 3 | Shared code | No shared library. Everyone implements hashing, signatures and Merkle trees themselves and must reproduce every vector in `tests/vectors/hash-vectors.json` (generated from `hashing.md` by `tools/make_hash_vectors.py`; covers leaf_hash, rejection_leaf, rejections_root, target_hash per action type, config_hash, roster_hash, token_set_root, receipt_hash, ballot_set_root, result_hash, snapshot_hash and the signature digests). Each module's CI checks them |
| 4 | Independent development | At first, replace the other services with OpenAPI mocks (prism). Real connections start at the integration points |
| 5 | Integration points | Week 3 T01, week 5 mandatory fault tests, week 7 demo rehearsal. Seungmin is the integrator |
| 6 | Running | `docker compose up` starts everything. Ports, service keys and organization keys live in one `.env`. Separate demo configuration. Each owner ships a Dockerfile, a compose service block with a healthcheck and their `.env` keys by week 2; Seungmin merges them. Bootstrap order: nodes → contract deploy (deterministic address, or an init job that writes `CONTRACT_ADDRESS`) → registry setup script (Harits) → staff-account setup script (Seungmin) → Core, Gateway, Rule Engine |
| 7 | Scope | Split into mandatory and optional (section 9) |
| 8 | Progress sharing | Work tracked in GitHub Issues. Once a week everyone shares "done / blocked / contract change requests" |

The platform (D-02) and the AI model (D-15) are chosen by their owners. All other design choices are confirmed in
`docs/decisions.md` (v1.0).

---

## 8. Roles

### 8.1 Seungmin — Core API, web, integration

| Item | Content |
|---|---|
| Mandatory | Election state machine, roster and link issuance, redeem / ballot (two schemas), outbox (FIFO, seq, retries, per-bucket release), checkpoints and snapshot hashes, view API (`?checkpoint=k`), creation check, executing admin actions (ROSTER_AMEND, ELECTION_VOID), tally and finalization, storing finding bodies and relaying reviews, voter web and admin web (including approval signing) |
| Optional | WINDOW_CHANGE, ELECTION_CLOSE_EARLY, TOKEN_REISSUE, AI report storage and review screen, service logs |
| Reference files | `core-api.openapi.yaml`, `contracts/README.md` (state machine, checkpoints, consistency model), `hashing.md`, `ledger-events.schema.json` |
| Counterpart during development | Gateway mock (`ledger-gateway.openapi.yaml`) |
| Integration duties | `docker-compose.yml`, `.env.example`, demo scenario scripts |

### 8.2 Harits — ledger, smart contract, Gateway

| Item | Content |
|---|---|
| Mandatory | Contract per `chain-interface.md` (registry, election state, per-checkpoint state, proposal and approval quorum, finding records and reviews, finalization conditions), Gateway event validation (sequence, voting window, duplicates, token state), batching and Merkle anchoring of voter events, rejection records, anchor-rejection alerts, query API, node setup with a pinned node run by the observer, registry setup script |
| Optional | Indexing of the direct chain submissions (`submitDecision`, `submitFindingRecord`, `submitFindingReview`) |
| Reference files | `chain-interface.md`, `ledger-gateway.openapi.yaml`, `hashing.md` (Merkle, digests), `ledger-events.schema.json` |
| Counterpart during development | Test with the event examples in `contracts/examples/` instead of Core |
| Decides | Blockchain platform (D-02) and node setup. Hard requirement: the chain (contract or chaincode) must verify the signatures of approvals, reviews and finding records (D-11) |

### 8.3 Anik — Rule Engine, AI Audit Agent

| Item | Content |
|---|---|
| Mandatory | Discovering checkpoints (polling `/internal/v1/elections`, `/checkpoints`), R1–R5 including the `observed` keys, INCONCLUSIVE handling, signing and registering findings and storing their bodies, alerts (AUDIT_DELAYED, LEDGER_UNAVAILABLE), direct reads from the pinned node (anchors, registry, contract state) |
| Optional | AI Audit Agent (read-only tools, evidence-citing reports), manual re-run requests |
| Reference files | `contracts/README.md` "Audit rules", `finding.schema.json`, `audit-report.schema.json`, `chain-interface.md` "How R5 uses it", `hashing.md` |
| Counterpart during development | Core mock, Gateway mock, `contracts/examples/` |
| Decides | AI model and how it is called (D-15) |
| Note | Ground-truth labels for fault tests are written first by Harits (not Anik), for a fair experiment |

---

## 9. Schedule and scope

| Week | Goal | Done when |
|---|---|---|
| 1–2 | Repository and environment, each module's core features against mocks, hash test vectors | Each module talks to its mocks; test vectors pass; the T01 path **including finding registration and body storage** works against mocks; registry setup script and pinned node exist |
| **3** | **Integration 1: normal vote** | T01 path runs end to end (vote → receipt ANCHORED → R1–R5 findings recorded); reported as "T01 (partial R4/R5)" if any rule check is still unimplemented |
| 4–5 | Mandatory fault handling, admin actions, tally and finalization | — |
| **5** | **Integration 2: mandatory fault tests** | full T01 pass; T02, T04, T05, T08, T15 pass |
| 6–7 | Optional features (window change, early close, reissue, T10, T11, direct submissions) | Whatever fits |
| **7** | **Integration 3: demo rehearsal** | On a fresh PC, `docker compose up` and demo a normal vote plus 2 faults (T14) |
| 8 | AI Audit Agent (if time allows), experiments, report, presentation | — |

**Mandatory:** normal vote, roster amendment, voiding, rules R1–R5, tests T01, T02, T04, T05, T08, T15.
**Optional:** window change, early close, link reissue, direct chain submissions, AI Audit Agent, remaining tests.

---

## 10. Development environment

### 10.1 Repository layout
```
purevoting2/
├─ contracts/        # interface contracts (frozen at v1.0, changes by PR)
├─ docs/             # this design, decisions, threat model, test plan
├─ core/             # Seungmin: Core API
├─ web/              # Seungmin: voter web, admin web
├─ ledger/           # Harits: smart contract, Gateway, node config
├─ rules/            # Anik: Rule Engine, AI Audit Agent
├─ tests/scenarios/  # fault scenarios and ground-truth labels (expected.json)
├─ tests/vectors/    # hash-vectors.json (shared hash test vectors)
├─ tools/            # validate_contracts.py, make_hash_vectors.py
├─ docker-compose.yml
└─ .env.example
```

### 10.2 Running and configuration
- `docker compose up` starts the DB, Core, web, nodes, Gateway, Rule Engine and (AI).
- Values managed in `.env` (see `.env.example`):
  - service ports
  - service keys (`CORE_KEY`, `RULE_KEY`, `AGENT_KEY`, `GATEWAY_KEY`)
  - per-organization signing keys (demo)
  - pinned chain configuration (chain_id, contract address, pinned node URL)
  - demo election settings used by scripts (Core applies no default bucket, D-10)
- Secrets never go into the repository; `.env.example` shows only the format.

| Setting | Evaluation | Demo |
|---|---|---|
| Roster | 100 voters | 100 voters |
| Voting window | 2 hours | 10 minutes |
| Time bucket | 900 s | 60 s |
| Checkpoint interval | 900 s | 60 s |
| Expected voters per bucket (≥ 10) | 12.5 | 10 |

### 10.3 Collaboration rules
- Commit freely in your own folder. Change `contracts/` by PR, @mention the owners and add one line to `CHANGELOG.md`.
- CI runs `tools/validate_contracts.py` on every push; add each module's unit tests to CI too.
- Register your work as GitHub Issues. Once a week share "done / blocked / contract change requests".
- After large design changes or integration tests, check with the supervisor agent (`.claude/agents/psp1-project-supervisor.md`).

---

## 11. Tests and demo
- **Tests:** detailed steps and pass criteria for T01–T16 are in `docs/test-plan.md`. Fault scenarios live in
  `tests/scenarios/<id>/`; their ground-truth labels are committed before running.
- **Experiment scope:** the protocol in `docs/test-plan.md` is the target; the MVP minimum is the mandatory scenarios ×
  seeds 1–3 for B1 (and B2 if the agent exists).
- **Experiment baselines:**
  - B0: original PureVoting
  - B1: rules only
  - B2: rules + AI
  - Metrics: FAIL precision and recall, root-cause accuracy, evidence validity, AI cost
- **Demo flow (draft):**
  1. Normal vote
  2. Roster change without approval (T05) → R4 FAIL
  3. Ballot tampering in the DB (T08) → R5 FAIL
  4. Signed reviews by organizations
  5. Finalization refused

---

## 12. Risks

| Risk | Response |
|---|---|
| Short schedule for the size of the contracts | Mandatory / optional split; re-scope at the week 3, 5 and 7 integration points |
| Hash or signature computation differs between modules | `tests/vectors/hash-vectors.json` mandatory in every module's CI, done within weeks 1–2 |
| Blocked waiting for each other | Independent development against mocks |
| Different readings of the contracts | When blocked, open an Issue → change the contract by PR. No verbal-only agreements |
| Overclaiming in presentations | Keep to "What we do not claim" (section 1) and the threat model |

---

## Appendix. Design review history
- The supervisor agent reviewed the contracts 17 times.
  - Rounds 1–15 failed; each round's findings were fixed.
  - Rounds 16 (v0.16) and 17 (v0.17) passed in a row (no P1).
- The remaining round-17 findings were fixed in v0.18, which was then frozen as v1.0. History: `contracts/CHANGELOG.md`.
