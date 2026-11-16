# ElGamal ballots (D-30, contracts v1.2.0; R5 checks v1.2.2)

Scheme `ELGAMAL_ED25519_V1`. No party holds a key that opens a single ballot: each organization of the election keeps
its own secret, the election key is the sum of their public keys, and only the per-candidate sums are decrypted, with
one share per organization. Every ballot proves that it is one 1 and the rest 0 without showing which.
Implementations: `core/app/elgamal.py` (Core, trustee tool) and `web/src/crypto/elgamal.ts` (browser); both must
reproduce `tests/vectors/elgamal-vectors.json` (`tools/make_elgamal_vectors.py`). **Not audited cryptography.**

Elections created before D-30, or with `BALLOT_SCHEME=SEALED_BOX_V1`, keep the libsodium sealed box of D-08.

## 1. Group and encodings

- Group: the prime-order subgroup of Ed25519, order `L = 2^252 + 27742317777372353535851937790883648493`, generator
  `G` = the Ed25519 base point. Additive notation: `s·P`, `P + Q`.
- Point: 32-byte Ed25519 encoding. Every point received from outside (ballot `A`, `B`, share `D`, trustee key `X`)
  must pass libsodium `crypto_core_ed25519_is_valid_point` (canonical, in the prime-order subgroup, not the identity).
- Scalar: 32 bytes little-endian, `< L` (a non-canonical scalar makes the ballot or share invalid).
- In JSON, points and scalars are 64 lowercase hex characters; a ballot is the raw bytes of section 3 (base64 in
  `sealed_ballot`, hashed as bytes for `ballot_hash` like any sealed ballot).
- Challenge `Hs(label, p1, …, pk)`: SHA-512 over `u16(len(x)) || x` for `x` in
  (`"PureVoting/elgamal-ed25519/v1"`, `label`, `p1`, …, `pk`) (`u16` big-endian, strings UTF-8), read as a 64-byte
  little-endian integer, mod `L`.
- Candidates are numbered `j = 0 … n-1` in candidate_id order (the order of `candidates` in the election); `n <= 15`
  so a ballot fits the 3072-byte `sealed_ballot` column.

## 2. Organization keys (trustees)

Each organization runs `python -m app.scripts.trustee keygen`: secret `x`, public `X = x·G`, and a Schnorr proof of
knowledge (stops one organization from choosing its key to cancel the others'): `w` random, `c = Hs("key", org_id, X,
w·G)`, `z = w + c·x`. Record `{org_id, public_key: X, proof: {c, z}}`. Verify: `c == Hs("key", org_id, X, z·G − c·X)`.
The deployment registers the record (`app.scripts.register_trustee`, table `credential.trustees`). At creation Core
copies the records of the election's organizations into the election (`trustee_keys`, org_id order) and sets the
election key `Y = Σ X_i`; `public_key_hash = H(Y)` is in ELECTION_CREATED as before, so the key is anchored.

## 3. Ballot (browser)

Context `ctx = (election_id, nullifier_hash as 32 bytes, Y)`. Binding the proofs to `nullifier_hash = H(credential)`
means a ballot copied to another credential does not verify.

For each candidate `j` with `m_j = 1` for the choice, else 0, random `r_j`:
`A_j = r_j·G`, `B_j = m_j·G + r_j·Y`, and a disjunctive Chaum-Pedersen proof that `m_j ∈ {0, 1}`:
- for the false branch `s = 1 − m_j`: random `c_s, z_s`; `T_s = z_s·G − c_s·A_j`, `U_s = z_s·Y − c_s·(B_j − s·G)`;
- for the true branch: random `w`; `T_m = w·G`, `U_m = w·Y`;
- `c = Hs("bit", ctx…, u16(j), A_j, B_j, T_0, U_0, T_1, U_1)`, `c_m = c − c_s`, `z_m = w + c_m·r_j`.

Sum proof that `Σ m_j = 1`: `A* = Σ A_j`, `B* = Σ B_j`, `R = Σ r_j`, random `w`,
`c = Hs("sum", ctx…, A*, B*, w·G, w·Y)`, `z = w + c·R`.

Bytes: `"PVE1"` · `u16(n)` · for each j: `A_j B_j c_0 c_1 z_0 z_1` (192 bytes) · `c z` (64 bytes);
length `6 + 192·n + 64`.

Verification (Core at submitBallot and at tally, the trustee tool, R5 at the TALLY checkpoint: section 6): exact length and header, valid points and
canonical scalars, for each j `c_0 + c_1 == Hs("bit", ctx…, u16(j), A_j, B_j, z_0·G − c_0·A_j, z_0·Y − c_0·B_j,
z_1·G − c_1·A_j, z_1·Y − c_1·(B_j − G))`, and `c == Hs("sum", ctx…, A*, B*, z·G − c·A*, z·Y − c·(B* − G))`.

## 4. Tally

1. Core takes the ballots of runTally (ledger-ACCEPTED, row = event, `H(sealed) = ballot_hash`); one whose proofs fail
   is invalid (in `invalid_ballot_hashes`). Aggregate per candidate: `(ΣA_j, ΣB_j)` over the valid ballots.
2. `GET /v1/public/elections/{id}/tally-input` lists those ballots and the aggregate. Each organization's trustee tool
   (`python -m app.scripts.trustee decrypt`) reads the Gateway's stored events (its read-only auditorKey) and requires
   Core's list to be exactly the accepted BALLOT_RECEIVED set and the election key to match `public_key_hash` of
   ELECTION_CREATED. Core gives the bytes of every listed ballot; the tool checks each hash, decides validity from
   the proofs itself, refuses any ballot without bytes unless a person allows that many (`--allow-unavailable`),
   recomputes the aggregate, and refuses a second, different aggregate for the same election (record kept next to
   its secret); only then does it send its share: for each j, `D_j = x·A_j` with `w` random, `c = Hs("share", election_id, org_id,
   u16(j), X, A_j, D_j, w·G, w·A_j)`, `z = w + c·x` (`POST …/decryption-shares`, no login; Core verifies
   `c == Hs("share", …, z·G − c·X, z·A_j − c·D_j)` against `X` of that organization in the election).
3. With a verified share from every organization: `M_j = B_j − Σ_i D_ij = votes_j·G`, `votes_j` found by trying
   0 … number of valid ballots. TALLY_PUBLISHED is unchanged (results, counts, `ballot_set_root`, `result_hash`).
4. From the TALLY checkpoint the election_config view carries `decryption_shares`, so the Rule Engine (R5) verifies
   every ballot proof, the aggregate, every share proof and the result without any key (section 6).

## 5. What it does and does not give (threat-model S3)

- Nobody, the commission included, can open a ballot unless **every** organization of the election hands over its
  share for that ballot. The trustee tool decrypts only the sum of the ledger's accepted ballots, once per election
  (`--allow-unavailable N` lets up to N ballots without bytes drop out of that sum; a person decides it after checking why).
- This rests on the Gateway (the neutral organization) and Core (the commission) not colluding: the tool compares Core's
  list with the Gateway's stored events. Run with `--no-ledger-check`, a modified Core could pad the list with ballots
  it made itself and learn one voter's choice. A ballot Core stuffed during voting (an unused token's credential) is on
  the ledger like any other; it is counted, not hidden (R3 / R4 concerns). A commission that also controls the other
  organizations' keys (one person running all three, as in the demo) can decrypt anything: the guarantee needs
  separate key holders.
- The browser still sees the choice before encrypting it, and the bucketed time link (threat-model S1) is unchanged.
- The cryptography is implemented here, not taken from an audited library (only the group operations are libsodium).

## 6. R5 checks for ElGamal elections (Rule Engine, contracts v1.2.2)

For an election with `ballot_scheme = ELGAMAL_ED25519_V1` these checks replace two checks of the sealed box in the R5
row of `contracts/README.md`: the re-decryption with the revealed private key, and the check of that key against
`public_key_hash`. The other R5 checks stay as they are, including `ballot_set_root` and `result_hash`. R5 uses only
inputs it already reads, with no key and no new Core call.

**Inputs at checkpoint k**

| Input | Fields used | Encoding |
|---|---|---|
| `election_config` view | `public_key` (Y), `organizations`, `candidates`, `trustee_keys`, `decryption_shares` | `public_key` base64 (standard) of 32 bytes; points and scalars in `trustee_keys` and the shares 64-char hex |
| `ballot_receipts` rows | `event_id`, `row_key` (the nullifier_hash), `ballot_hash`, `receipt_hash`, `sealed_ballot` | `row_key` 64-char hex, `sealed_ballot` base64 |
| accepted BALLOT_RECEIVED events | | |
| ELECTION_CREATED | `public_key_hash` | |
| TALLY_PUBLISHED of checkpoint k (TALLY only) | `final_checkpoint`, `invalid_ballot_hashes`, `results` | |

**Checks**, in this order. Step 1 runs at every checkpoint. Steps 2–4 run only at the TALLY checkpoint, and their
keys are null at other checkpoints.

1. **Keys** → `key_mismatch`. One count for each failing item:
   - `trustee_keys` has exactly one record per organization of `organizations`, in org_id order;
   - each record's proof verifies (section 2);
   - `Σ X_i = Y`;
   - `H(Y)` over the 32 raw bytes equals `public_key_hash` of ELECTION_CREATED.

   If `key_mismatch > 0`, steps 2–4 are skipped and their keys are null. The rule is already a FAIL.
2. **Ballots** → `tally_invalid_set_mismatches`.
   - Exactly one TALLY_PUBLISHED must be at checkpoint k. Otherwise R5 is INCONCLUSIVE (`OTHER`, the summary says
     "TALLY_PUBLISHED missing at the TALLY checkpoint" or "more than one"), and the keys of steps 2–4 are null.
   - The ballots of the tally are every accepted BALLOT_RECEIVED with checkpoint ≤ `final_checkpoint`, as in Core.
   - A ballot goes to the invalid set I when any of these holds:
     - it has no row with its `event_id`;
     - the row's (`row_key`, `ballot_hash`, `receipt_hash`) differs from the event's (`nullifier_hash`, `ballot_hash`,
       `receipt_hash`);
     - `H(sealed_ballot) ≠ ballot_hash`;
     - its proofs fail (section 3, with `ctx = (election_id, the event's nullifier_hash, Y)` and `n` = number of
       candidates).

     Every other ballot is in the valid set V. The first three cases are also FAILs through `row_ledger_mismatches` or
     `sealed_hash_mismatches`.
   - `tally_invalid_set_mismatches` is the size of the symmetric difference between I and TALLY
     `invalid_ballot_hashes`, compared as multisets of ballot_hash. Duplicates are kept, as in Core.
3. **Shares** → `tally_share_failures`.
   - If `decryption_shares` is null, R5 is INCONCLUSIVE `KEY_NOT_REVEALED`, unchanged, and
     `tally_share_failures` and `tally_result_mismatches` are null.
   - **If V is empty**, no share is needed. Core sends no share and publishes 0 votes, and `decryption_shares` is `[]`.
     Each entry that is there anyway counts as one failure.
   - Otherwise the aggregate is recomputed per candidate as `(Σ A_j, Σ B_j)` over V. Each organization of the election
     must have exactly one share in `decryption_shares`, matched by org_id. The share must verify against that
     organization's X in `trustee_keys` and the recomputed aggregate (section 4, step 2). When an aggregate `A_j` is
     the identity, its `D_j` must be the identity too.
   - `tally_share_failures` counts each organization whose share is missing, duplicated or not verifying, and each
     share of an org_id that is not an organization of the election.
   - If `tally_share_failures > 0`, step 4 is skipped and `tally_result_mismatches` is null.
4. **Result** → `tally_result_mismatches`.
   - If V is empty, every candidate is expected to have 0 votes.
   - Otherwise `M_j = Σ B_j − Σ_i D_ij`, summed over the shares of the election's organizations only. `votes_j` is
     found by trying 0 … |V| (`0·G` is the identity).
   - `tally_result_mismatches` counts:
     - each candidate whose votes differ from TALLY `results` (matched by candidate_id) or are not found in that range;
     - a candidate missing from `results` counts as 0 votes there;
     - each `results` entry whose candidate_id appears twice or is not a candidate of the election.

Any non-zero key of steps 1–4 is a FAIL. With these checks the TALLY decryption check has no unimplemented part for
ElGamal elections. This does not cover the `result_hash` recomputation or `TALLY.final_checkpoint ≠ CLOSE_FINAL` in
R5. When the ElGamal item is removed from the "not implemented" list, those two must either be implemented or stay
listed.

**Implementation**

- The group operations need libsodium. Use `pynacl==1.5.0`, the same version as Core:
  - functions in `nacl.bindings`: `crypto_core_ed25519_add`, `crypto_core_ed25519_sub`,
    `crypto_core_ed25519_is_valid_point`, `crypto_scalarmult_ed25519_noclamp`,
    `crypto_scalarmult_ed25519_base_noclamp`;
  - add it to `rules/requirements.txt` and to `rules/requirements-lock.txt` (with `cffi` and `pycparser`), because
    the Docker image installs from the lock file.
- Two cases need the guards of `base()` and `mul()` in `core/app/elgamal.py`:
  - scalar multiplication raises an error for the scalar 0 or the identity point;
  - `is_valid_point` is False for the identity.

  Port those guards as they are.
- The Rule Engine is implemented in `rules/` and does not import Core. Porting the verification functions of
  `core/app/elgamal.py` is fine.
- It must reproduce `tests/vectors/elgamal-vectors.json` (trustee proofs, joint key, ballots, aggregate, shares, votes).
  That file has only valid ballots, so the Rule Engine's tests must also cover:
  - an invalid ballot;
  - an altered share;
  - forged `results`;
  - an identity aggregate;
  - an election with no valid ballot.
- Cost in the demo (100 voters, a few candidates): one ballot verification per ballot plus 3 share verifications per
  candidate. That is well under a second.
