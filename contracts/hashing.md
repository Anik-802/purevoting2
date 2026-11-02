# Hashing, Merkle trees and signatures (normative)

Every component MUST compute these values exactly as written; R4/R5 compare them across components.
`tools/validate_contracts.py` contains a reference implementation of `H`, `canonical`, `merkle_root` and `set_root`.

## Primitives

| Name | Definition |
|---|---|
| `H(bytes)` | SHA-256, output as 64 lowercase hex chars |
| `H(str)` | `H(utf8(str))` |
| `canonical(obj)` | RFC 8785 JSON Canonicalization Scheme (JCS). Hashed objects MUST NOT contain floating-point numbers (all numeric fields in the schemas are integers) |
| `Hc(obj)` | `H(canonical(obj))` |
| `node(l, r)` | `H(l + r)` where `l`, `r` are 64-char hex strings concatenated as ASCII text (128 chars) |
| `merkle_root([x1..xn])` | Leaves are the given hex strings, in the given order. Pair adjacent nodes left to right with `node`; if a level has an odd count, the last node is carried up unchanged. Root of one leaf = that leaf. Empty list = `H("")` |
| `set_root([x..])` | `merkle_root(sorted ascending, duplicates removed)` |
| base64 text | Base64 values inside hashed objects or hashed view rows (sealed_ballot, public_key, private_key) use standard RFC 4648 §4 base64 with padding, no line breaks |
| timestamp text | Timestamps inside hashed objects use exactly `YYYY-MM-DDTHH:MM:SSZ` (UTC, no fractional seconds) — enforced by `common.schema.json#/$defs/Timestamp` |
| sort order | Everywhere in this file "sorted" means ascending by Unicode code point of the string (= byte order for the ASCII keys/hex used here) |

## Values

| Value | Formula | Where |
|---|---|---|
| `token_hash` | `H(token)` (token = 22-char base64url string) | token_status, TOKEN_* events |
| `nullifier_hash` | `H(credential)` (credential = 22-char base64url string generated **by the voter's browser**) | redeem, ballots, BALLOT_RECEIVED |
| `ballot_hash` | `H(sealed_ballot raw bytes)` (after base64 decoding) | ballots, BALLOT_RECEIVED |
| `receipt_hash` | `Hc({"election_id", "nullifier_hash", "ballot_hash"})` — the voter's browser can compute it before sending | BALLOT_RECEIVED, receipts |
| `payload_hash` | `Hc(payload)` | every ledger event |
| `leaf_hash` | `Hc(event)` where `event` = the LedgerEventSubmission object exactly as stored, **including** `payload_hash`, **excluding** `received_at` and `anchor`. Covers type, election, time, actor, checkpoint and payload | Merkle anchors |
| `anchor merkle_root` | `merkle_root(leaf_hash of events in the anchor, in the order returned by GET /v1/anchors/{id}/leaves)`. Batched anchors list leaves in a random order chosen at batch close | anchors |
| `roster_hash` | `set_root([H(voter_key) for ACTIVE voters])` | ROSTER_*, election_config |
| `token_set_root` | `set_root([token_hash for tokens not revoked])` after the operation | TOKEN_BATCH_ISSUED, TOKEN_REVOKED |
| `ballot_set_root` | `merkle_root(sorted([ballot_hash of EVERY accepted BALLOT_RECEIVED with checkpoint ≤ final_checkpoint]))` — sorted, duplicates **kept** (one leaf per accepted event, valid and invalid alike), so anyone can recompute it from the ledger | TALLY_PUBLISHED |
| `finding_hash` | `Hc(FindingSubmission)` — the finding exactly as submitted by the Rule Engine | FINDING_RECORDED / FINDING_REVIEWED |
| `note_hash` | `H(note)` (review note text, UTF-8) | FINDING_REVIEWED |
| `rejection_leaf` | `Hc({"rejection_id","election_id","event_id","type","checkpoint","reject_code","recorded_at","payload_hash","subject_hash"})` — `subject_hash` is `null` when absent; `detail` and `anchor_id` are **not** hashed | rejections |
| `rejections_root` | `set_root([rejection_leaf of every rejection of the election recorded since the previous anchor of that election])`; `H("")` if none | anchors |
| `public_key_hash` | `H(raw 32-byte X25519 public key)` (after base64 decoding) | ELECTION_CREATED |
| `config_hash` | `Hc({"title","candidates":[{"candidate_id","name"} sorted by candidate_id],"window_start","window_end","time_bucket_seconds","checkpoint_interval_seconds","organizations":[{"org_id","signer"} sorted by org_id, signer as lowercase 0x-hex address (eip191) or base64url public key (ed25519)],"approval_threshold","proposal_ttl_hours","operator_org","public_key_hash"})` — `window_start` / `window_end` are the values **at creation** (election_config view fields `config_window_start` / `config_window_end`), not the effective window | ELECTION_CREATED |
| `snapshot_hash(view, k)` | `Hc(rows)` where `rows` = **all** rows of the view as of checkpoint `k`, sorted by `row_key`, each row exactly as returned by `GET /internal/v1/views/{view}?checkpoint=k`. Every field of a row is defined "as of k" (README#checkpoints), so the value can be recomputed later. `ballot_receipts` rows include the `sealed_ballot` bytes, so changing ballot content changes the snapshot | CHECKPOINT_COMMITTED, views |
| `target_hash` | `Hc({"election_id","action_id","action_type","target"})`. `target` per type: ROSTER_AMEND `{"add":[voter_key..],"remove":[voter_key..]}` — both keys always present, arrays sorted, possibly empty; TOKEN_REISSUE `{"voter_keys":[voter_key..]}` sorted, ≥1; WINDOW_CHANGE `{"new_window_end"}`; ELECTION_CLOSE_EARLY `{"closed_at"}` (requested time); ELECTION_VOID `{"finding_ids":[finding_id..]}` sorted, possibly empty. Contains no names/emails | proposals |
| `result_hash` | `Hc({"election_id","final_checkpoint","results":[{"candidate_id","votes"} for EVERY candidate of the election, including 0 votes, sorted by candidate_id],"ballot_count","invalid_ballots","invalid_ballot_hashes":[sorted],"excluded_ballots","ballot_set_root"})` | TALLY_PUBLISHED, ELECTION_FINALIZED |

## Signatures (business approval, finding records and reviews)

Signed digests use a **fixed binary layout** so that the smart contract can rebuild them from its `bytes32` arguments
(no JSON on chain). All parts are exactly 32 bytes and concatenated:

| Part | Encoding |
|---|---|
| `tag(s)` | ASCII string `s`, right-padded with zero bytes to 32 bytes |
| `idh(x)` | `sha256(utf8(x))` (32 raw bytes) for an ID string such as `elc_…`, `prp_…`, `org_observer`. The **same value** is the `bytes32` ID used in `chain-interface.md` |
| `h32(x)` | the 32 raw bytes of a 64-hex SHA-256 value from this file (target_hash, finding_hash, note_hash) |
| `u(n)` | unsigned integer `n`, 32-byte big-endian |

Codes: action type ROSTER_AMEND 1, TOKEN_REISSUE 2, WINDOW_CHANGE 3, ELECTION_CLOSE_EARLY 4, ELECTION_VOID 5 ·
proposal decision REQUEST 0, APPROVE 1, REJECT 2 · review decision DISMISSED 1, ACCEPTED_RISK 2, CONFIRMED_ISSUE 3 ·
rule R1..R5 = 1..5 · result PASS 0, FAIL 1, INCONCLUSIVE 2.

| Digest | Formula (`sha256` of the concatenation) | Signed by |
|---|---|---|
| request digest | `tag("PV2-PROPOSAL-v1") ‖ idh(proposal_id) ‖ idh(action_id) ‖ idh(election_id) ‖ u(action type) ‖ h32(target_hash) ‖ idh(org_id) ‖ u(0)` | requesting organization |
| approval digest | same layout with `u(1)` APPROVE or `u(2)` REJECT and the deciding `org_id` | each deciding organization |
| finding record digest | `tag("PV2-FINDING-v1") ‖ idh(finding_id) ‖ idh(election_id) ‖ u(rule) ‖ u(result) ‖ u(finding_checkpoint) ‖ h32(finding_hash)` | the Rule Engine's registered key (`ruleEngineSigner` in the registry) |
| review digest | `tag("PV2-REVIEW-v1") ‖ idh(finding_id) ‖ idh(election_id) ‖ h32(finding_hash) ‖ idh(org_id) ‖ u(review decision) ‖ h32(note_hash)` | each reviewing organization |

`cause_code` and `report_id` sent with a review are informational and **not** signed.

**Approver obligation.** Before signing, the client recomputes `target_hash` from the `target` it displays and the digest
itself — for a request from the draft's fields (the proposal is not on the ledger yet), for a decision from the proposal
read from the ledger; on mismatch it refuses to sign.

**Replay across deployments.** Digests contain no chain_id or contract address; organizations must use fresh signer keys
for each deployment (test vs. demo network), otherwise a signature could be replayed on another deployment.

**Reviewer obligation.** Before signing a review, the reviewer's client recomputes `Hc(finding body)` — the stored Finding
without `review`, `recorded_event_id` and `evidence_check`, i.e. the FindingSubmission — from the body it displays and compares it with the `finding_hash` of the FINDING_RECORDED event read from the ledger (Gateway with the
organization's auditorKey, or a node). On mismatch it refuses to sign — so Core cannot show reviewers an altered finding.

**Message encoding.** The digest's 32 raw bytes are the signed message.
- `eip191-secp256k1` (required on EVM platforms: Besu D-02 A, PureChain D-02 C): Ethereum `personal_sign` of the 32 bytes, i.e. secp256k1 over
  `keccak256(0x19 || "Ethereum Signed Message:\n32" || digest)`. `value` = `0x` + r (32 B) + s (32 B) + v (1 B, 27 or 28)
  as 130 lowercase hex chars; `signer` = 0x-prefixed lowercase address. The contract verifies with `ecrecover`.
- `ed25519` (Fabric only, D-02 B — EVM has no Ed25519 verification): RFC 8032 over the 32 bytes; `value` = base64url
  64-byte signature (86 chars); `signer` = base64url 32-byte public key (43 chars).
- Test vectors (checked by `tools/validate_contracts.py`): `SIG_DIGEST_VECTOR` (approval digest),
  `FINDING_DIGEST_VECTOR` and `REVIEW_DIGEST_VECTOR`, with their inputs in that file; eip191 prefix bytes
  `19457468657265756d205369676e6564204d6573736167653a0a3332`.

**Registry and verification.**
- **Key registration.** Each organization's signer key is written to the on-chain registry by a transaction signed with
  *that organization's own node account* at network setup. The commission cannot add or replace another organization's key.
  `ELECTION_CREATED.organizations` must equal the registry entries for the listed org_ids; the contract rejects the election otherwise.
- **Operator organization.** The registry records which organization operates Core (`operatorOrg`); effective only after a
  confirmation transaction from **every** registered organization's node account. `ELECTION_CREATED.operator_org` must equal it.
  A staff login (JWT issued by Core) never determines it.
- **Rule Engine key.** The registry records the Rule Engine's signer (`ruleEngineSigner`), registered by the node account of the
  organization that operates the Rule Engine (D-25) and confirmed by every organization, like `operatorOrg`.
- Signatures are made **in the signer's own client**; Core and the Gateway only relay them. Organizations and the Rule Engine
  may also submit directly to the contract (chain-interface.md), which verifies the same digests.
- The contract verifies: signer = registry key of the org (or `ruleEngineSigner`), digest rebuilt from its arguments, the org is
  a participating organization of that election (in ELECTION_CREATED.organizations) — registered organizations that do not take part in the election never count —, the org has not
  already decided / reviewed, and (proposals) the org is not the requesting org.
- Whoever controls an organization's node account and signer key *is* that organization for the system. In a demo where one
  person runs all nodes and keys, approvals and reviews are not independent (threat-model I1, D-21).
