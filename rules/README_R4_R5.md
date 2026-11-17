# R4 and R5 — contract specification v1.2.2

This implementation follows `contracts/README.md`, `contracts/hashing.md`,
`contracts/chain-interface.md`, and Section 6 of `contracts/ballot-elgamal.md`,
including specification commit `572c51f`. The contracts remain the source of truth.

## What is implemented

`purevoting_rules/admin_audit.py` implements R4: registry-bound request, approval,
and review signatures; participating organizations and quorum; approved target
hashes and confirmed proposal state; duplicate executions; exact effect sets and
execution windows; sequential roster-hash chains; historical roster/token changes;
token-batch coverage; window changes; early-close deadlines; `ALL_VOTED` turnout
and timing; and election-void targets.

`integrity_local.py` implements R5's historical snapshot hashes, row-to-ledger
matches, sealed-byte hashes, roster/token roots, immutable configuration hash,
rejected checkpoints, and checkpoint cadence. At `TALLY`, it separately checks
that `final_checkpoint` identifies the unique `CLOSE_FINAL`, recomputes
`ballot_set_root` from accepted events and rows while retaining duplicate hashes,
and recomputes `result_hash`. These checks were not removed when ElGamal coverage
was completed.

`elgamal.py` and `tally_elgamal.py` independently verify organization key proofs,
the combined public key and its creation commitment, ballot proofs and the invalid
ballot multiset, every organization's decryption share, and candidate counts.
Zero scalars and identity aggregates are handled explicitly. Zero valid ballots
require no shares and produce zero votes; unexpected shares are violations.
The retained `SEALED_BOX_V1` path verifies the disclosed key and decrypts ballots,
parsing UTF-8 JSON with exactly one valid `candidate_id` key; duplicate or extra
keys are invalid. Rules code does not import Core.

## Evidence and chain interface

The live adapter supplies all seven checkpoint views, historical views for every
accepted checkpoint, cumulative accepted events and rejections, complete anchor
leaf/rejection sets, and event proofs. A batch crossing checkpoints is verified
using its complete tree; later bodies stay outside the selected checkpoint's
rule inputs.

`live_chain.py` reads locally configured `PINNED_NODE_URL`, `CHAIN_ID`,
`CONTRACT_ADDRESS`, and `CONFIRMATION_DEPTH`. Each evidence bundle uses one node
and one explicit confirmed block (`head - depth`), trying later pinned nodes when
needed. Service keys are not sent to RPC. `chain_audit.py` compares `anchorAt`
values with confirmed `AnchorCommitted` logs, independently recomputed roots and
proofs, immutable governance, and state replay against
`electionStateAtCheckpoint(k)`. Later mutable chain progress does not substitute
for that stored checkpoint state.

Cached `PENDING` metadata remains unavailable when a subsequent read already sees
the event's confirmed tree. A node anchor missing from the Gateway index is also
initially unavailable: the worker persists a separate wait for that commitment
and requires a fresh complete index read after the wait before confirming an
omission. Neither timing case hides independently demonstrated hash/root errors.

## Decision and deployment limits

Complete implementation coverage does not mean evidence is available or a
deployment passed T01. Missing registry/state evidence yields `NODE_UNAVAILABLE`;
missing Gateway anchor/proof evidence with a usable node yields
`BATCH_NOT_ANCHORED`; unavailable views yield
`VIEW_UNAVAILABLE`. At `TALLY`, null ElGamal shares or an undisclosed sealed-box key
yield `KEY_NOT_REVEALED`. Zero or multiple `TALLY_PUBLISHED` events yield `OTHER`
with an explanation. Demonstrated violations take precedence over evidence gaps.
Finalization conditions remain unchanged: incomplete evidence never becomes PASS.

Token views intentionally omit the voter-to-token association. R4 checks the
specified counts, reconstructed holder sets, approved revocation voter sets,
visible token state, and historical consistency; it does not reconstruct a hidden
association. Administrative proposal verification requires the normative
`proposalState` getter. The upstream contract used for this implementation does
not yet provide it, so otherwise-clean proposal-bearing elections remain
inconclusive until the ledger supplies that evidence. Proven violations can still
produce FAIL. Elections without proposals do not need this call. That upstream
version also lacks `finalizeElection`; successful E1 finalization requires Harits's
G18 contract implementation and a separate real deployment test.

## Run and verify

From the repository root, using a Python 3.11 environment:

```sh
python -m pip install -r rules/requirements-lock.txt
python -m pytest -q -c rules/pytest.ini rules/tests
```

The lock includes PyNaCl 1.5.0, cffi, and pycparser. Tests consume committed hash
and ElGamal vectors and cover tampering, invalid ballots, forged results, identity
aggregates, empty valid sets, administrative attacks, and missing evidence. The
synthetic complete-TALLY test demonstrates all five rule decisions against
controlled evidence. It is not real Gateway/PureChain finalization or official
T01 evidence; those require a separate deployment run. The protocol's custom
cryptography remains unaudited, as stated in the contract specification.
