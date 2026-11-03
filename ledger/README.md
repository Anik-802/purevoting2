# ledger/ (stage 3a: contract skeleton with anchors and tally)

`contracts/PureVotingLedger.sol` implements part of `contracts/chain-interface.md` (v1.0):

| Area | Status |
|---|---|
| Registry: `orgSigner`, `operatorOrg`, `ruleEngineSigner`, `gatewayAccount` with all-org confirmation | done |
| Election state: `createElection`, `freezeRoster`, `openElection`, scheduled `closeElection`, getters | done |
| Checkpoints: `commitCheckpoint`, `electionStateAtCheckpoint` | done |
| Anchors: `commitAnchor`, `anchorCount`, `anchorAt` | done (stage 3a) |
| `publishTally` (once only; `finalCheckpoint` must be the CLOSE_FINAL checkpoint, T15 d) | done (stage 3a) |
| Proposals and execution order, early close, `changeWindow`, `amendRoster`, `setTokenSetRoot` | not yet (week 4) |
| Signature checks, finding records and reviews, `finalizeElection`, `voidElection` | not yet (week 2 to 5) |

16 of the 32 interface functions are implemented. Each has the exact selector of the interface
(`tests/check_interface.py` compares them against the Solidity block in `chain-interface.md`).
Added functions (allowed by the interface text): `registerOrgSigner`, `setOperatorOrg`, `confirmOperatorOrg`,
`setRuleEngineSigner`, `confirmRuleEngineSigner`, `setGatewayAccount`, `confirmGatewayAccount`, `orgCount`, `orgAt`.

Compiler settings: solc 0.8.28, EVM target shanghai, optimizer on (200 runs). Without the optimizer the compile fails
with "stack too deep" (createElection has 11 parameters), so a Hardhat config needs `optimizer.enabled = true`
(or `viaIR = true`).

## Tests

`tests/test_ledger.py` deploys the contract to a local PureChain network and runs 123 checks: registry setup and
confirmation, every revert reason of the implemented functions, the state machine, time preconditions
(`TimeNotReached`), strictly increasing checkpoints and seqs, the stored checkpoint snapshots, anchors (batched rule, duplicates, order) and the tally rules.
Hardhat is not used yet (npm is not available where this was written); the tests are standard-library Python
against the nodes from the PureChain test kit.

    python3 -I ledger/tests/check_interface.py <repo root> <solc>
    python3 -I ledger/tests/test_ledger.py <solc>        # network with 4 nodes on RPC ports 8541 to 8544 running

## Assumptions to confirm (not stated in the contract docs)
1. Scheduled `closeElection` requires `closedAt == windowEnd` (the current effective end).
2. `commitCheckpoint` stores `lastCoreSeq` including its own seq.
3. `openElection` at or after `windowEnd` reverts `InvalidStateTransition` (README: only VOID remains).
4. Sanity checks added to `createElection`: `windowEnd > windowStart`, bucket and interval seconds not zero.
5. One node account can register only one org, and one signer key only one org (`registerOrgSigner`).
6. A node account that is not a registered org cannot set or confirm registry items.
7. Any existing election accepts `commitAnchor`, whatever its status (an anchor records data; it is not a state change).
8. The same `anchorId` with identical fields is a no-op (a retried transaction); with other fields it reverts `AnchorConflict`.
   Also: `bucketMin <= bucketMax`, mode 0 or 1, and the batched rule is checked against the stored (effective) `windowEnd`.
9. At most one checkpoint per election with reason CLOSE_FINAL; `publishTally` needs it to exist and `finalCheckpoint` to equal it.
   The TALLY checkpoint (reason 3) is not restricted by the contract; R3 and R5 check it.
10. A CLOSE_FINAL checkpoint is accepted only while the election status is CLOSED (condition added by the team lead).

Items 1 to 9 were approved by the team lead on Nov 2, together with item 10. Checkpoint numbering starts at 0;
`hasCheckpoint` (stored per election) tells "checkpoint 0 committed" from "no checkpoint yet" and is what
`submitFindingRecord` will use once it exists.
