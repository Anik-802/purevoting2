# Design decisions (v1.2)

> 2026-10-18 v1.0: after a supervisor review (DECISIONS VERDICT: READY TO CONFIRM) every item except D-02 and D-15 is
> **confirmed as recommended**. Wording added at confirmation is marked **Confirmed note (v1.0)**. D-02 is delegated to
> Harits and D-15 to Anik — each records the choice here by PR.
> `contracts/` v1.0 (frozen) matches these decisions. Changing a decision later means changing the contract files listed
> under **Affects** as well (PR, D-20).
> Owners: Seungmin (Core API, web, contracts, integration), Harits (ledger, smart contract, Gateway), Anik (Rule Engine,
> AI Audit Agent). Team rules (repository, freeze, mocks, integration points, scope): `docs/system-design.md` section 7.
> 2026-11-02 v1.1: D-02 confirmed (PureChain, on Harits's delegation), D-03 node count set, new D-27 (confirmation depth),
> after two supervisor reviews of the answers to Harits's open questions. Values marked *provisional* wait for Harits's
> measurement (due 2026-11-09). Contracts changed accordingly (v1.1, `contracts/CHANGELOG.md`).
> 2026-11-04 v1.2: new D-28 (schedule compression: demo 2026-11-19, final 2026-12-04, `docs/schedule.md`), after
> three supervisor reviews of the schedule (PASS). D-27's measurement deadline moves to 2026-11-13.
> History: drafts v0.1–v0.18 were revised after supervisor reviews 1–17; the Korean original with the full history is
> `decisions.ko.md`. If the two differ, this English version applies (D-19).

## Summary

| ID | Topic | Decision | Owner | Status |
|---|---|---|---|---|
| D-01 | 2-month scope | One single-choice voting method (one person, one vote, one candidate): one normal path + fault scenarios; advanced cryptography design-only | All | Confirmed |
| D-02 | Blockchain platform | **Private PureChain network** (go-ethereum fork, EVM, Solidity; option C); blocks sealed continuously with period P ≤ 5 s (*provisional*); must verify signatures on chain (D-11) | Harits | Confirmed (v1.1) |
| D-03 | Nodes and operators | **3 validators, one per organization**; pinned nodes: observer first, commission second (not the Gateway operator); fault tolerance measured | Harits | Confirmed |
| D-04 | Ledger recording unit | System events anchored individually; voter-side events only as Merkle roots of shuffled batches (60 s / 50 events) | Harits | Confirmed |
| D-05 | Languages | Python (FastAPI) servers, TypeScript (React) web; Solidity contract (EVM, D-02) | All | Confirmed |
| D-06 | Database | PostgreSQL, **one database, two schemas** (credential / ballot, different DB roles); Rule Engine and AI have no DB access | Seungmin | Confirmed |
| D-07 | Anonymous credential | **The browser generates the credential**; the server registers only its hash (in the same transaction as the link use) | Seungmin | Confirmed |
| D-08 | Ballot encryption | Sealed with the election public key (libsodium sealed box), decrypted after closing | Seungmin | Confirmed |
| D-09 | Lost link / credential | **A used link is never re-enabled.** Only a lost unused link is reissued after approval (TOKEN_REISSUE, optional). A lost credential = abstention | Seungmin | Confirmed |
| D-10 | Voter-side time precision | Ledger and views keep only bucketed times and checkpoint numbers (bucket size: ≥ 10 expected voters per bucket, no default); window bounds aligned to buckets | Seungmin, Harits, Anik | Confirmed |
| D-11 | Approvals | Roster amendment, link reissue, window change, early close, void: approval by **threshold** orgs other than the requester, each signing with its own key; signatures verified on chain | Harits, Seungmin | Confirmed |
| D-12 | Core ↔ ledger consistency | Outbox + seq; strict transitions applied at once and kept if rejected (alert); permissive ones applied after ACCEPTED, reverted if rejected | Seungmin, Harits | Confirmed |
| D-13 | Rule Engine data access | Core read-only views (as of a checkpoint) + Gateway query API + direct node reads; no DB access | Anik, Seungmin | Confirmed |
| D-14 | Rule / checkpoint cadence | Checkpoint interval = integer multiple of the bucket (default 1×) + on state changes; rules at every checkpoint + after closing + manual | Anik, Seungmin | Confirmed |
| D-15 | AI model and calling | Recommended Claude API tool use, single agent | Anik | Delegated (Anik) |
| D-16 | Link delivery | Development: MailHog (fake mailbox); demo: CSV export | Seungmin | Confirmed |
| D-17 | Authentication | Staff: ID/password + JWT (admin / approver / auditor + org); **a different key per service** and a permission matrix | Seungmin | Confirmed |
| D-18 | Repository and runtime | GitHub monorepo + docker compose + GitHub Actions | Seungmin | Confirmed |
| D-19 | Document language | Contracts and design documents in English (reference); `system-design.ko.md` is a translation, `decisions.ko.md` the Korean original | Seungmin | Confirmed |
| D-20 | Contract change process | PRs only, owners notified, no mandatory approval | All | Confirmed |
| D-21 | Organization signing keys | Each org's node account registers its own key in the **on-chain registry**; the commission cannot set keys; approvers keep their org's key | Harits, Seungmin | Confirmed |
| D-22 | Who judges voter-side violations | Per-event checks (window, duplicates) in Gateway software; the chain checks batch time ranges and system events (state machine, approval signatures) | Harits | Confirmed |
| D-23 | Election private key | MVP: held by the commission, **revealed after the tally** so auditors can recount (R5); threshold decryption is future work | Seungmin | Confirmed |
| D-24 | Finalization and review authority | Rule Engine records findings on the ledger; FAIL / INCONCLUSIVE resolved only by signed reviews of ≥ threshold orgs **other than the operator**; the contract checks at finalization; AI reports optional | Seungmin, Anik, Harits | Confirmed |
| D-25 | Rule Engine operator | ruleKey held and operated by an org other than the commission and the Gateway operator (demo: observer, v1.1) | All | Confirmed (v1.1) |
| D-26 | Gateway operator | Neutral org. It can delay relaying but cannot forge signed approvals, reviews or findings; signers can submit directly on chain | Harits | Confirmed |
| D-27 | Confirmation depth | A chain item counts only in a block with ≥ D blocks on top (D = 2, *provisional*); pinned like the contract address | Harits, Seungmin, Anik | Confirmed (v1.1) |
| D-28 | Schedule compression | Demo version 2026-11-19, final 2026-12-04; milestones M1–M4, per-owner dates, handoffs and fallbacks in `docs/schedule.md`; optional features not built; experiment reduced; mandatory scope unchanged | All | Confirmed (v1.2; AI agent option open) |

---

## Details

### D-01. 2-month scope
- **Decision**
  - Implement one single-choice voting method along one normal path: create election → freeze roster → issue links →
    register credential → encrypted ballot → ledger → checkpoint → rules R1–R5 → (on FAIL / INCONCLUSIVE) signed reviews
    by organizations → tally and finalize; plus fault-injection scenarios.
  - Other voting methods (multiple choice, ranked) only as an extension plan after the normal path works.
  - Design / future work only: blind signatures, threshold decryption, an Isolation Forest baseline, large repeated statistical experiments.
- **Affects:** overall schedule, `docs/test-plan.md`
- **Confirmed note (v1.0):** AI reports are optional (system-design section 9). Mandatory fault tests: T01, T02, T04,
  T05, T08, T15; the rest are optional.

### D-02. Blockchain platform — delegated to Harits
- **Options:** (A) Hyperledger Besu (QBFT) + Solidity, (B) Hyperledger Fabric, (C) reuse the original PureVoting private chain.
- **Recommendation:** (A); (C) if Harits can use the existing chain right away.
- **Hard requirement:** whatever the platform, the chain verifies the signatures of approvals, reviews and finding records (D-11).
- **Affects:** `Signature.scheme` (Besu: `eip191-secp256k1`; Fabric: `ed25519`); the Gateway API is platform-independent.
- **Decision (v1.1, Harits):** (C) — the original PureVoting private chain, **PureChain** (geth 1.13.15-purechain, a
  go-ethereum fork; EVM, Solidity, `eip191-secp256k1`), run as a private network with one validator per organization
  (D-03). Harits compared its modes (private always-mining, Smart Auto Mining, PoA2) on a local network.
  - **Block production:** the chosen mode seals blocks continuously, also without transactions (empty blocks), with a
    fixed period P. A mode that seals only when transactions arrive (Smart Auto Mining) is not allowed: confirmations
    (D-27) need blocks to keep coming.
  - **Timing:** (1 + D) · P ≤ time_bucket_seconds / 4. With the 60 s demo bucket and D = 2: P ≤ 5 s (whole seconds).
    Recomputed when D is final.
  - chain-interface.md is normative for EVM platforms (A or C). `GET /v1/chain` reports `platform: other`.
  - *Pending measurement (Harits, due 2026-11-09):* mode name and measured P.

### D-03. Nodes and operators
- **Background:** BFT-type consensus (e.g. QBFT) stalls with 3 nodes if one fails (3f+1).
- **Decision (platform-independent part):**
  1. Validator nodes are assumed to be split among the participating organizations. The report states that several docker
     nodes are **not evidence of independent operators**.
  2. At least one node run by an organization other than the Gateway operator (demo: the observer) is the pinned node in
     the deployment configuration (chain-interface.md).
  3. The node count follows D-02 and is set by Harits (4 for BFT-type consensus).
- **Decision (v1.1):** **3 validators, one per organization** (commission, neutral, observer). PureChain's modes are not
  BFT-type, so the 3f+1 rule does not apply. A non-validator standby node is optional and not counted.
  - Pinned RPC URLs for Core and the Rule Engine: the observer's validator first, the commission's second (both are
    run by orgs other than the Gateway operator; two URLs keep reads working when one node is stopped in a fault test).
  - Fault tolerance is measured, not assumed: stop one validator (do blocks continue?) and two (do they stop?). If one
    stopped validator halts the chain, the report states it as a demo limit.
  - *Pending measurement (Harits, due 2026-11-09):* the two stop tests.
- **Affects:** `GET /v1/nodes`, demo scenario, `docs/threat-model.md`

### D-04. Ledger recording unit
- **Decision**
  - System events (election state, roster, approvals, checkpoints, …) are anchored without batching, individually or in small groups.
  - Voter-side events (TOKEN_USED, BALLOT_RECEIVED) are collected in shuffled batches and **only the Merkle root** goes on
    chain. Batch interval 60 s or 50 events (the "batch interval" used in the README rule timing).
  - So the chain never sees individual voter events and checks only the batch time range (D-22).
- **Confirmed note (v1.0):** Core releases system events at the end of their time bucket (contracts README "Delivery
  order"); "immediately" refers to how the Gateway anchors them. Batch values are platform-independent.
- **Affects:** `ledger-events.schema.json`, `/v1/anchors`

### D-05. Languages
- Python 3.11 + FastAPI, web TypeScript + React.
- **Confirmed note (v1.0):** the contract language follows D-02 (Besu: Solidity; Fabric: chaincode with the same function
  names and arguments as chain-interface.md).
- **v1.1:** D-02 = PureChain (EVM), so the contract is Solidity.

### D-06. Database
- **Decision:** one PostgreSQL **database** with schemas `credential` (elections, roster, token hashes, admin work) and
  `ballot` (credential hashes, encrypted ballots, receipts), owned by different DB roles. No common key between the
  schemas. Marking a link used and registering the credential happen in one transaction.
- **Disclosed limit:** a DB superuser or anyone with access to WAL / transaction metadata can link token and credential
  without changing code. Unlinkability holds only against parties without DB-admin access (Rule Engine, AI, ledger, auditors).
- **Rejected alternatives:** two DB instances with a non-atomic two-step process (needs an R3 correction rule); blind
  signatures (unlinkable even for the operator, too risky for the schedule).
- **Affects:** Core API description, `docs/threat-model.md` S2

### D-07. Anonymous credential
- **Decision**
  1. The web reads the token from the URL fragment (`#t=…`).
  2. **The browser generates a random credential and stores it first**, then sends only `nullifier_hash = H(credential)`.
  3. In one transaction the server checks the token, marks it used and registers the nullifier_hash in the ballot schema.
     The link between token and nullifier is never stored.
  4. The raw credential is sent with the ballot; the server checks that its hash is registered and unused.
- **Benefit:** a lost response is safe to retry because the browser keeps the credential; the server never stores the raw credential.
- **Disclosed limit:** a server operator who manipulates memory during a request can link token and nullifier; blind
  signatures are future work.
- **Affects:** `POST /v1/credentials:redeem`, `POST /v1/ballots`

### D-08. Ballot encryption
- **Decision:** one key pair per election; the web seals the ballot with the public key; the private key is used only at
  tally after closing (D-23).

### D-09. Lost link / credential
- **Background:** re-enabling used links would let a voter hide a credential, claim it lost and vote twice — undetectable by R3.
- **Decision**
  - A used link can never be used again (no re-enable operation exists).
  - Only a **lost unused** link may be reissued (`TOKEN_REISSUE`, approval required): the old token is revoked
    (TOKEN_REVOKED) and a new link issued.
  - A credential lost after using the link (e.g. browser storage cleared) counts as abstention.
- **Confirmed note (v1.0):** TOKEN_REISSUE is optional (system-design section 9). If it is not implemented, a lost unused
  link also counts as abstention.
- **Affects:** `AdminActionType`, R1, R4

### D-10. Voter-side time precision
- **Background:** receive times, ULID IDs and sort order would leak precise times.
- **Decision**
  - Voter-side events: time rounded down to the bucket, random `evr_` IDs without timestamps, no receive time, no sequence number.
  - Views carry checkpoint numbers instead of times.
  - Window start and end are aligned to buckets, so R2 decides late submissions exactly with `occurred_at + bucket > end`.
- **Enforced at roster freeze:** refused if `roster size × bucket ÷ window length < 10` (BUCKET_TOO_SMALL). No default bucket size.
- **Sizing rule:** at least 10 expected voters per bucket on average (roughly `bucket ≥ 10 × window ÷ expected voters`);
  e.g. 100 voters over 2 hours → bucket ≥ 15 minutes. With 1–2 voters per bucket, link use and ballot receipt pair up and,
  together with the key revealed after tally, a holder of the CSV / credential DB could identify ballots (threat model S1).
- **Affects:** `ledger-events.schema.json`, `ElectionCreate.time_bucket_seconds`

### D-11. Approvals

| Action | Approval |
|---|---|
| `ROSTER_AMEND` (correction after freeze) | threshold orgs other than the requester |
| `TOKEN_REISSUE` (reissue an unused link) | threshold orgs other than the requester |
| `WINDOW_CHANGE` (before closing only) | threshold orgs other than the requester |
| `ELECTION_CLOSE_EARLY` | threshold orgs other than the requester |
| `ELECTION_VOID` (void / cancel before voting) | threshold orgs other than the requester |

- The requesting org signs the request; each deciding org signs its approval or rejection **with its own key**. Core and
  the Gateway only relay signatures.
- Only organizations **taking part in the election** (ELECTION_CREATED.organizations) count; a registered org that does
  not take part never counts toward the quorum.
- **Signatures are verified on chain** (contract or chaincode) on every platform. Moving verification into the Gateway
  would break D-26 and threat-model I1, I7, I11; that would require changing the contracts and the threat model first.
  **This is a hard requirement for the D-02 choice.**
- Business approval (organization signatures) and blockchain consensus (nodes) are different things and are presented separately.
- **Confirmed note (v1.0):** the threshold satisfies 2 ≤ threshold ≤ participating orgs − 1 (demo: 2). Mandatory
  implementation: ROSTER_AMEND and ELECTION_VOID; the others are optional (system-design section 9).
- **Affects:** `contracts/hashing.md`, `/v1/proposals`, R4

### D-12. Core ↔ ledger consistency
- **Decision**
  - Core writes the DB change and the outbox record in one transaction; the worker retries with the same event_id.
  - System events are ordered by a per-election seq; voter events have none (no order leak).
  - **Strict behaviour first** (close: stop accepting at once); permissive behaviour only after the ledger accepts (open:
    accept ballots only after ACCEPTED).
  - If the Gateway rejects: permissive transitions (open, finalize — both applied only after ACCEPTED) are reverted
    (finalize → stays TALLIED). Strict transitions (freeze, close) and the tally are **kept** with an alert; a ledger
    rejection never re-opens voting or roster editing. Rejected ballots are excluded from the count and flagged for review.
- **Confirmed note (v1.0):** window shortening is strict, window extension permissive (contracts README).
- **Affects:** Core API description, `contracts/README.md#consistency-model`

### D-13. Rule Engine data access
- **Decision:** the Rule Engine and the AI never connect to the DB. They use Core's views (**as of a committed checkpoint**)
  and the Gateway query API; the Rule Engine reads anchors, registry and contract state directly from a validator node
  (chain-interface.md). Views contain no names, contacts or student IDs.
- **Affects:** `/internal/v1/views`, `/internal/v1/checkpoints`

### D-14. Rule / checkpoint cadence
- **Decision:** the checkpoint interval is an integer multiple of the bucket (default = the bucket), committed only on
  bucket boundaries, plus on state changes, once after closing and at tally. Checkpoints finer than buckets are forbidden
  (checkpoint numbers would reveal times finer than buckets). Rules run only after the checkpoint is applied on the
  ledger; before that an alert (AUDIT_DELAYED) is recorded instead of a finding.
- **Affects:** `CHECKPOINT_COMMITTED`, `Finding.checkpoint`

### D-15. AI model and calling — delegated to Anik
- **Recommendation:** Claude API tool use, single agent; contract limits: 10 s per tool call, at most 20 steps.
- **To decide:** model, who manages the API key, budget.

### D-16. Link delivery
- Development: MailHog; demo: a CSV downloadable once right after issuing.
- **Disclosed limit:** an admin holding the CSV can vote on behalf of voters (threat model).
- **Confirmed note (v1.0):** if the CSV response is lost, a retry with the same Idempotency-Key within 15 minutes returns
  the same CSV. Later loss → TOKEN_REISSUE (optional); without it the affected voters abstain.

### D-17. Authentication
- **Decision:** staff use JWT (admin / approver + org); invited auditors have the read-only `auditor` role (no names, no
  CSVs). Service calls use **a different key per service**, limited by the permission matrix
  (`contracts/README.md#permission-matrix`). The AI key can only read and create reports.
- **Confirmed note (v1.0):** only operator-org staff get `admin`. Accounts are created by the deployment setup script;
  there is no account API (threat model O1).

### D-18. Repository and runtime
- GitHub monorepo, `docker compose up` starts everything, GitHub Actions validates the contracts. Same as
  system-design section 7, items 1 and 6.

### D-19. Document language
- Contracts and design documents are in English. **The English design documents are the reference**;
  `system-design.ko.md` is a translation and `decisions.ko.md` is the Korean original with the review history. If they
  differ: contracts → English documents.

### D-20. Contract change process
- `contracts/` changes only by PR. No mandatory approval, but @mention the affected owners and add one line to
  `contracts/CHANGELOG.md`.

### D-21. Organization signing keys
- **Background:** if the commission entered organization keys at election creation, it could forge "multi-org approval" with its own keys.
- **Decision:** at network setup each organization's node account registers its own signer key in the on-chain registry.
  Elections only list org IDs; keys come from the registry. Threshold: 2 ≤ threshold ≤ orgs − 1.
- **Operator org:** which org runs Core (operator_org) is recorded in the registry and becomes effective only after a
  confirmation transaction from **every** organization — never decided by a staff login.
- **Censorship resistance:** organizations can submit approvals and reviews directly on chain, bypassing Core.
- **Disclosed limit:** in a demo where one person runs all nodes and keys, approvals are not independent.
- **Confirmed note (v1.0):** in the demo all org keys sit in one `.env` (system-design 10.2, threat model I1). "Node
  account" means the account or identity of the D-02 platform.

### D-22. Who judges voter-side violations
- **Decision:** the Gateway software checks each voter event (window, duplicates); the chain checks batch time ranges and
  system events (state machine, approval signatures). Presentations never claim "the smart contract rejects each late ballot".
- **Rejected alternative:** one chain record per vote (the chain could reject directly, at an anonymity cost).

### D-23. Election private key
- **Decision:** in the MVP the commission holds the key alone and **reveals it after the tally is on the ledger**. The Rule
  Engine and auditors re-decrypt every ballot and recompute the result (R5). Ballots are separated from identities, so the
  key reveals no voter's choice to auditors. The commission, however, operates Core and the DB and holds the key, so it can
  link voters to choices even before the tally (threat model S2, S3).
- **Scope of reveal:** Rule Engine, staff and auditors only — not the public, never the AI (external API).
- **Disclosed limit:** before the tally the key holder can read ballots early; after the reveal a voter who kept their
  sealed ballot can prove their choice (vote-selling / coercion risk). Threshold decryption and decryption proofs are future work.

### D-24. Finalization and review authority
- **Decision**
  - The Rule Engine records each finding hash itself (FINDING_RECORDED); Core cannot hide or alter findings.
  - FAIL / INCONCLUSIVE reviews are signed by each organization with its own key and recorded on the ledger
    (FINDING_REVIEWED). Signed reviews from ≥ threshold organizations **other than the operator** resolve a finding
    (RESOLVED); one CONFIRMED_ISSUE blocks finalization.
  - An INCONCLUSIVE caused by an outage is superseded by a later PASS of the same rule (SUPERSEDED). A FAIL is never superseded.
  - Core accepts a finding body under one condition only: schema-valid + record exists + hash matches. Unresolved evidence
    is flagged, not rejected. The Rule Engine validates the schema before recording.
  - If a record exists but Core has no body yet, the Rule Engine uploads the same body later (Core answers 503 until the
    record is indexed). The Rule Engine keeps every body until the election is finalized or voided.
  - The smart contract checks these conditions again from the ledger at finalization. AI reports are advisory (optional).
- **On a confirmed issue:** no finalization; the only exit is an approved `ELECTION_VOID` (terminal). A new election is a new election.
- **Affects:** Gateway `/v1/finding-records`, `PATCH /v1/findings/{id}/review`, `POST /v1/elections/{id}:finalize`

### D-25. Rule Engine operator
- **Background:** whoever holds the Rule Engine key could record selectively (only PASS) or delay audits.
- **Decision:** an org other than the commission (v1.1: and other than the Gateway operator) holds and runs ruleKey — demo:
  the observer (v1.1 below). The Rule Engine code is public so each
  participating org (and invited auditors) can re-run the rules on the same data with staff views and the read-only
  Gateway key (auditorKey) and compare.
- **Disclosed limit:** if one person runs it in the demo, it is not an "independent audit".
- **Confirmed note (v1.0):** in the demo the neutral org runs the Gateway, the Rule Engine and the AI together, so that
  power is concentrated in one org; this is stated as a limit. *(Superseded by v1.1 below.)*
- **Decision (v1.1):** follows Harits's registry setup (`setup_registry.py`, `--rule-engine-operated-by org_observer`):
  in the demo the **observer** operates the Rule Engine and the AI Audit Agent and registers `ruleEngineSigner`; the
  neutral org keeps the Gateway (D-26). The Rule Engine is thus not run by the org whose Gateway it audits, and reads its
  own (pinned) node. The observer also reviews findings, but a review needs both non-commission orgs (threshold 2), so it
  cannot resolve its own findings alone. The demo limit (one person runs every org) is unchanged.

### D-26. Gateway operator
- **Background:** the Gateway operator sits on the path of every ledger record.
- **Decision:** a neutral org, not the commission, runs the Gateway. The Gateway account is registered by that org's node
  and effective only after every org confirms it. Approvals, reviews and finding records are signed by their signers and
  verified by the contract, so the Gateway operator cannot forge them; if it blocks or delays relaying, signers submit
  directly on chain (chain-interface.md).
- **Disclosed limits**
  - In a demo where one person runs the Gateway and the nodes there is no independence.
  - Creation fields the Gateway passes to the contract (organizations, threshold, …) are checked by Core right after
    creation (reading a node not run by the Gateway operator) and by R5 at every checkpoint; on a mismatch the election is
    abandoned (deleted at Core).
  - The contract address and the pinned node URL are fixed in the deployment configuration confirmed by every org; values
    reported by the Gateway are only compared.
  - Core system events are not signed by Core, so the Gateway operator could inject or pre-empt them — Core detects this by
    comparing with its outbox but cannot prevent it (threat model I12). Core signatures are future work.
- **Confirmed note (v1.0):** demo layout — Gateway = neutral org, pinned node = observer's node (not the Gateway
  operator). Core and the Rule Engine both read that node.

### D-27. Confirmation depth (v1.1)
- **Background:** contracts v1.0 assumed final blocks ("QBFT blocks are final"; ACCEPTED = "included in a block"). On
  PureChain a recent block can be replaced by a fork, so "included" is not "final".
- **Decision**
  - One more pinned chain value, `confirmation_depth` **D** (provisional **2**), next to chain_id, contract_address and the
    RPC URLs. A block b is **confirmed** on a node when that node's latest block ≥ b + D.
  - The Gateway exposes chain-derived data only from confirmed blocks (submission outcomes including REJECTED, finding
    records, anchors and proofs, indexed chain events, proposal status, registry). It re-broadcasts only the same signed
    transaction while it is unconfirmed, and treats "already applied" reverts as ACCEPTED / DUPLICATE only on an exact
    match in confirmed state (chain-interface.md "Confirmations").
  - Core's creation check and the Rule Engine count a chain item only when the pinned node's latest block ≥ its block + D.
  - Core retries RETRY_LATER every 1 s for the first 30 s, then with exponential backoff up to 60 s.
  - **Choosing D:** finality is a protocol property and cannot be proven by measurement. D = max(2, deepest reorg Harits
    observes in validator stop/restart tests + 1).
- **Disclosed limit:** D only protects against accidental forks. Two of three validators (in the demo, one person) can
  rewrite the chain to any depth; this is detected afterwards by R5 and Core comparisons (threat model I15), not prevented.
- **Affects:** `contracts/chain-interface.md`, `contracts/ledger-gateway.openapi.yaml`, `contracts/README.md`, `.env.example`,
  `docker-compose.yml`, `docs/threat-model.md` (I15), `docs/test-plan.md` (T07, T10), `docs/roles/*`

### D-28. Schedule compression (v1.2)
- **Background:** the project must show a full demo on 2026-11-19 and be finished by 2026-12-04, instead of the 8-week
  plan of system-design section 9 (D-01 "2-month scope").
- **Decision**
  - `docs/schedule.md` replaces the week table of system-design section 9 and the milestone tables of `docs/roles/*`:
    M1 11-11 (T01 on the real stack), M2 11-19 (demo version), M3 11-26 (mandatory scope), M4 12-01 (code freeze,
    T14), final 12-04.
  - Mandatory scope unchanged (D-01): normal vote, roster amendment, voiding, R1–R5, T01, T02, T04, T05, T08, T15.
  - Not built: WINDOW_CHANGE, ELECTION_CLOSE_EARLY, TOKEN_REISSUE, direct chain submissions, T03, T06, T07, T09–T13, T16.
  - AI Audit Agent: **open, the team lead chooses** (a) future work, B2 not run (recommended), or (b) Anik builds it
    11-17 – 11-24 within 3 days, dropped if R4 / R5 or Anik's 11-25 work slip.
  - Experiment (test-plan): mandatory scenarios × seeds 1–3 (4–5 if time allows), demo configuration, baseline B1,
    2026-11-27 – 11-30; labels still committed before the first run.
  - D-27 measurements due 2026-11-13 instead of 11-09; D = 2 until then.
  - Milestone verdicts are PASS / CONDITIONAL / FAIL; work continues in parallel and P1 findings are fixed before the next
    run. Fallbacks (`docs/schedule.md` section 4) are fixed now and switched on by one line here when needed.
- **Affects:** `docs/schedule.md`, `docs/system-design.md` (sections 7, 9, risks), `docs/roles/*` (milestones),
  `docs/test-plan.md` (experiment protocol), `tests/scenarios/README.md` (label deadlines)
