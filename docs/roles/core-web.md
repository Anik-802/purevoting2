# Role design — Core API, web and integration (Seungmin)

> Part of the PureVoting 2.0 design. Overall picture: `docs/system-design.md`. Source of truth for every rule below:
> `contracts/` v1.0 — if this page and a contract disagree, the contract wins. Folders: `core/`, `web/`.

## 1. Mission and boundaries

You own everything the voter and the staff touch, and the integration of the whole system:

- **Core API** (`contracts/core-api.openapi.yaml`, port 8000) — election state machine, PostgreSQL, outbox,
  checkpoints, views, tally, finalization, relaying signed approvals and reviews;
- **Voter web** and **admin web**;
- **Integration** — `docker-compose.yml`, `.env.example`, demo scripts, the week 3 / 5 / 7 integration runs.

You do **not** own: the chain or the Gateway (Harits), judging anomalies (Anik). Core never signs on behalf of an
organization and never creates findings; it only relays signatures made in the signer's own client.

## 2. Read first

| File | Sections |
|---|---|
| `contracts/core-api.openapi.yaml` | `info.description` (callers, storage, consistency), every operation description |
| `contracts/README.md` | Shared conventions, Permission matrix, Election state machine, Checkpoints, Consistency model, Ledger event types |
| `contracts/hashing.md` | token / nullifier / ballot / receipt hashes, `snapshot_hash`, `roster_hash`, `token_set_root`, `config_hash`, `target_hash`, `result_hash`, Signatures (client-side signing) |
| `contracts/ledger-events.schema.json` | every event you emit |
| `contracts/chain-interface.md` | Pinned chain configuration, Creation check, Execution order |
| `contracts/ledger-gateway.openapi.yaml` | `POST /v1/events` (outcomes, delivery order), proposals, finding-record reviews |

## 3. Interfaces

**You provide:** all of `core-api.openapi.yaml`.
- Voter (no login): `/v1/public/*`, `/v1/credentials:redeem` and `/v1/ballots`.
- Staff: JWT with roles `admin` / `approver` / `auditor` plus the user's org.
- Internal: `/internal/v1/*` for the Rule Engine (`ruleKey`), the Audit Agent (`agentKey`) and the Gateway (`gatewayKey`), as limited by the permission matrix.

**You consume:**
- Gateway `POST /v1/events`, `/v1/proposals*`, `/v1/finding-records/{id}/reviews` and queries (`coreKey`);
- the pinned validator node, for the creation check only.

## 4. Mandatory features

### 4.1 Core API
1. **Elections:**
   - Create elections with `config_hash`, `public_key_hash` and a bucket-aligned window. The operator org comes from the registry, never from the login.
   - Create the election's X25519 key pair; keep the private key until it is revealed at the TALLY checkpoint (D-23).
   - Run the **creation check** once ELECTION_CREATED is ACCEPTED or DUPLICATE: read `electionState` and `electionGovernance` from the pinned node and compare them with the event. `creation_check` is PENDING, PASSED or MISMATCH. Node behind `block_number` → retry; unreachable for 60 s → LEDGER_UNAVAILABLE alert; `configHash == 0` 60 s after the node reached the block → MISMATCH (not found). Until PASSED, deliver no further outbox event of the election. Before week 3 a dev-only node stub may return the expected values; it must be removed for T01.
   - Until the check is PASSED, refuse freeze, issue and admin actions. On MISMATCH, only deletion is possible.
2. **Roster and links:**
   - Roster PUT only in DRAFT.
   - Freeze applies the D-10 sizing rule (`BUCKET_TOO_SMALL`).
   - Issue tokens (CSV, with Idempotency-Key retry for 15 min). Only `token_hash` is stored.
3. **Voting:**
   - `redeem` locks the token, marks it USED and inserts `nullifier_hash` into the `ballot` schema, all in **one transaction**. No column links the two.
   - `ballots`: idempotent retry (same ballot → 200 with the original receipt); window check after the idempotency check; `rejected_attempts` for refusals.
4. **Outbox:**
   - Creation check (v1.1): wait until the pinned node's latest block ≥ `block_number` + `CONFIRMATION_DEPTH`; `PINNED_NODE_URL` is a comma-separated list, try the URLs in order.
   - Write the change and its event in one transaction. Retry with the same `event_id` (RETRY_LATER: every 1 s for the first 30 s, then exponential backoff up to 60 s; contracts v1.1).
   - Give system events a gap-free `seq` per election. Voter events get random `evr_` IDs and bucketed times, and no seq.
   - **Release unit = time bucket.** At the end of a bucket, send its voter events (shuffled) first, then its system events in seq order. Send the next request only after the previous one has final outcomes.
   - Handle each outcome:
     - ACCEPTED / DUPLICATE → done;
     - RETRY_LATER → retry;
     - CONFLICT / REJECTED → follow the consistency model (alert, FIFO stop, strict vs permissive transitions).
   - Compare every `source: core` ledger event with the outbox (`event_id`, `payload_hash`). Alert and stop on mismatch.
5. **Checkpoints:**
   - Commit PERIODIC checkpoints on bucket boundaries (an interval that is a multiple of the bucket), plus STATE_CHANGE, CLOSE_FINAL and TALLY.
   - Each commit carries the snapshot hashes and counts of the six `SnapshotTable` tables (`roster`, `token_status`, `credential_issuance`, `ballot_receipts`, `admin_actions`, `rejected_attempts`; not `election_config`) and `voter_events` derived from the views.
   - PERIODIC checkpoints are counted from `ELECTION_OPENED.occurred_at` (R5 cadence check).
   - **Boundary serialization:** requests with t ≥ T wait for the boundary commit or close at T.
6. **Views:**
   - Seven views served with `?checkpoint=k`. Every field is "as of k", so a committed k always returns the same rows and the same `snapshot_hash`.
   - No names, and no view that joins tokens with credentials or ballots.
7. **Admin actions** (mandatory: ROSTER_AMEND, ELECTION_VOID):
   - Flow: create (returns `target`, `target_hash`, request digest) → submit (requester's signature) → decisions (relay signatures) → execute.
   - At execution, re-check the rules atomically.
   - Emit ADMIN_ACTION_EXECUTED first, then exactly that proposal's effects in the next seq numbers.
8. **Findings and reviews:**
   - Store finding bodies under the single acceptance condition (schema + record exists + `Hc(body)` matches). Return 503 while the record is not indexed.
   - Resolve evidence into `evidence_check`.
   - Relay signed reviews; status = the contract's `contract_status`.
9. **Tally:**
   - Preconditions: CLOSE_FINAL accepted, outbox drained.
   - Count one ballot per accepted BALLOT_RECEIVED ≤ final, check `H(sealed_ballot)`, validate the plaintext.
   - Compute `result_hash` / `ballot_set_root`. Reveal the private key (`election_config`, ruleKey + staff only) from the TALLY checkpoint on.
10. **Finalize:**
    - Same conditions as the contract: R1–R5 at TALLY, all FAIL / INCONCLUSIVE resolved or superseded, no CONFIRMED_ISSUE.
    - Apply FINALIZED only after ACCEPTED.
11. **Alerts and auth:**
    - Alert list; accept only the alert kinds each service may raise.
    - Staff JWT (accounts created by the setup script, no account API).
    - Service keys per the permission matrix.

### 4.2 Web
- **Voter web:**
  1. Read the token from the URL fragment.
  2. Generate the credential, store it and send `nullifier_hash` only.
  3. Seal the ballot with the public key (libsodium sealed box), store it, then submit.
  4. Show the receipt and check it (`receipt_hash` computed locally); its status becomes ANCHORED when Core sees the BALLOT_RECEIVED anchored (poll Gateway `GET /v1/events/{event_id}`).
- **Admin web:**
  - election setup, roster, link CSV, tally, finalize, alerts;
  - an approval screen that **recomputes `target_hash` and the digest before signing** with the organization's key;
  - a review screen that recomputes `Hc(finding body)` against the ledger before signing;
  - signing keys: the approver loads their org's signer key into the admin web (file or paste, kept in browser memory only). Core never reads `ORG_*_SIGNER_KEY`; only the registry setup script and the scenario scripts do.

### 4.3 Integration
- `docker-compose.yml` brings up the DB, Core, web, nodes, Gateway, Rule Engine and (AI). Each owner hands you a Dockerfile, a compose block with a healthcheck and their `.env` keys by week 2; you merge them in the bootstrap order of system-design section 7 (#6).
- `.env.example` lists ports, service keys, demo org keys, the pinned chain configuration and demo election settings used by scripts (no Core default, D-10). Secrets are never committed.
- Demo configuration: 100 voters, 10-minute window, 60 s bucket and checkpoint interval.
- Run the integration tests at weeks 3, 5 and 7, and call the supervisor agent after each.

## 5. Optional features
WINDOW_CHANGE, ELECTION_CLOSE_EARLY, TOKEN_REISSUE; AI report storage and review screen; service logs; outbox resume
screen.

## 6. Pitfalls
- **Hashing must match bit for bit.** Port the reference functions from `tools/validate_contracts.py` and reproduce every vector in `tests/vectors/hash-vectors.json` in week 1 (regenerate it with `tools/make_hash_vectors.py` only if `hashing.md` changes).
- **Time on voter rows:**
  - Voter-side rows store only bucket and checkpoint values, never exact times.
  - Their primary keys are random.
  - Logs never contain token / nullifier / receipt hashes, `evr_` IDs, IPs or User-Agents.
- **Strict vs permissive transitions:**
  - Strict transitions (freeze, close, shortening, tally) are kept if the ledger rejects them.
  - Permissive ones (open, finalize, extension) are applied only after ACCEPTED.
- **Event times:**
  - A boundary-scheduled event carries the logical boundary time T as `occurred_at`.
  - A **system** event created exactly on a boundary belongs to the bucket that ends there (released at once); a **voter** event at a boundary belongs to the bucket that starts there (floor).

## 7. Tests you own
- **Mandatory scenarios (you drive them as integrator):** T01, T02, T04, T05, T08 and T15. The setup and fault injection scripts live in `tests/scenarios/`.
- **Ground-truth labels:** Harits writes them before any run (T01 before week 3, the others before week 5); you write the setup / fault-injection scripts.
- **T14:** a fresh-PC demo.
- **Optional:** T03, T06, T09, T10, T13 and T16.

## 8. Milestones

| When | Done when |
|---|---|
| Week 1 | Repo skeleton, `docker-compose.yml` with DB + Core, `tests/vectors/hash-vectors.json` reproduced |
| Week 2 | Election → roster → links → redeem → ballot → outbox against the Gateway mock; checkpoints and views; **`/internal/v1/elections`, `/internal/v1/checkpoints`, `/internal/v1/views*`, `POST /internal/v1/findings` (record lookup at the Gateway mock)**; creation check against the pinned node (dev stub allowed until week 3); merge the other owners' compose blocks |
| **Week 3** | **T01 end to end** with the real Gateway and Rule Engine |
| **Week 5** | Admin actions (ROSTER_AMEND, ELECTION_VOID), tally, finalize; T02, T04, T05, T08, T15 |
| **Week 7** | Demo rehearsal on a fresh PC (T14) |

## 9. First issues
1. Repo skeleton, `docker-compose.yml`, `.env.example`, CI for module tests.
2. Hashing library + `tests/vectors/hash-vectors.json`.
3. DB schemas (`credential`, `ballot`) + migrations.
4. Elections + creation check; roster + freeze; token issue.
5. Redeem + ballots.
6. Outbox worker (seq, bucket release, outcomes) + checkpoints + views + internal endpoints for the Rule Engine.
7. Findings storage (`POST /internal/v1/findings`) — week 2, needed for T01.
8. Admin actions (ROSTER_AMEND, ELECTION_VOID) + approval relay.
9. Review relay; tally; finalize.
10. Voter web; admin web (signing screens).
11. Staff-account setup script — week 2 (T01 needs an admin JWT; it runs before Core in the bootstrap order).
12. Scenario scripts for the mandatory tests.
