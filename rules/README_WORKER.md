# Rule Engine HTTP worker — v1.2.2 checks

The worker reads Core checkpoint views and cumulative Gateway evidence, verifies the independent pinned chain, evaluates R1–R5, signs each finding, registers its hash at the Gateway, and sends the same durable body to Core. Full R4/R5 coverage is described in [README_R4_R5.md](README_R4_R5.md). Finalization requirements are unchanged.

## Verify locally

From the repository root with Python 3.11:

```sh
python -m pip install -r rules/requirements-lock.txt
python -m pytest -q -c rules/pytest.ini rules/tests
```

The dependency lock includes PyNaCl 1.5.0, cffi and pycparser, and is also used by `rules/Dockerfile`. The synthetic complete-TALLY tests exercise all five rules, but do not certify a deployed Gateway/PureChain run. The optional local-EVM getter check is documented in `devtools/check_chain_evm.py`; its additional dependencies are separate from the worker's.

## Run on the team's configured integration stack

Use the deployment owner's existing chain, contract and service configuration. `docker-compose.integration.yml` documents the required bootstrap and registry order. The deployed contract must match the checked-out specification, and the observer's rule signer must be registered. ElGamal elections need the three organizations' trustee public records before creation and their shares before tally, as documented in `contracts/ballot-elgamal.md` and `tools/trustees.sh`.

After the integration stack is bootstrapped, rebuild the worker from the repository root:

```sh
docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d --build rule-engine
```

For a directly executed worker, set `CORE_URL`, `GATEWAY_URL`, `RULE_KEY`, `RULE_ENGINE_SIGNER_KEY`, `CHAIN_ID`, `CONTRACT_ADDRESS`, `PINNED_NODE_URL`, `CONFIRMATION_DEPTH`, and a private writable `RULE_STATE_DIR`. Use pins agreed with the deployment owner: locally ordered observer/commission URLs must also appear in Gateway `/v1/chain`. Secrets belong in the local environment, outside evidence files.

```sh
cd rules
python -m purevoting_rules.live_worker --once
python -m purevoting_rules.live_worker
```

A standalone `--once` run can report that it is still waiting; the continuous worker retains acceptance times across restarts. A healthy process is a liveness result, not an audit PASS. Inspect the findings at the exact TALLY checkpoint and the poll counters before judging readiness.

## Evidence and failure behavior

The HTTP reader validates election/checkpoint identity, schemas, pagination, ordering and uniqueness. It reads all seven views at every accepted historical checkpoint. Missing views remain unavailable. A missing rejection endpoint never becomes an empty rejection list.

The node evidence is fixed before reading the Gateway anchor index. Every confirmed anchor is examined, including anchors not referenced by the returned event list, so an omitted accepted ballot cannot disappear from the audit. A cross-checkpoint batch is verified with all its leaves; extra future events remain outside that checkpoint's rule inputs. An unread tree whose checkpoint scope cannot be established leaves R5 INCONCLUSIVE. The default limits are 4,096 anchor records and 20,000 tree members per collection, in addition to page and response-byte limits. Exceeding a limit aborts the collection; no truncated collection produces PASS.

The node reader checks chain identity and reads the registry, governance, checkpoint state, anchors and logs at one explicit confirmed block. It tries locally pinned nodes in order and falls back when a node is lagging. No service key is sent to RPC, and redirects are not followed. Roots and proofs supplied by the Gateway are checked against node commitments.

The worker waits up to `2 * ANCHOR_BATCH_SECONDS + 60` seconds from its first observation of acceptance for missing chain/proof evidence. After that deadline it can publish the rules supported by the complete off-chain snapshot while leaving unavailable R4/R5 checks INCONCLUSIVE. Missing or multiple TALLY events, null decryption shares, and invalid proofs cannot become PASS. Recoverable evidence gaps are reconsidered on the next poll; completed checkpoints are not repeatedly alerted when a later preflight fails. Queued publication failures remain visible.

A selected event's cached `PENDING` anchor status can predate the node/tree read. This is `BATCH_NOT_ANCHORED`, not proof of an anchor mismatch; immutable payload, leaf and root checks still detect tampering. A confirmed node anchor absent from the Gateway index also begins as unavailable evidence. Each such commitment gets its own persisted wait of `2 * ANCHOR_BATCH_SECONDS + 60` seconds from first observation, independent of checkpoint age. Only a complete Gateway-index read started after that wait can confirm the same continuing omission as a mismatch. Recovery clears the timer; changed commitments start a new timer. An unavailable index cannot confirm an omission, and a slow tree/proof read cannot turn an earlier index read into a qualifying later one.

Findings use a single-writer durable SQLite outbox and a separate poll journal bound to the deployment and signer. `RULE_POLL_SECONDS` defaults to 60. Publication retries the same stored bytes; a permanent 4xx needs investigation. `RULE_PARTIAL_RECHECK_SECONDS` remains supported for future partially implemented rules, but current R4/R5 evidence failures use prompt recovery polling. Optional manual audit requests, direct-chain submission fallback and the AI agent are separate work.

## Limited developer HTTP smoke

`devtools/audit_smoke_gateway.py` and `compose.worker-smoke.yml` are for a separate disposable smoke project. The latter selects `SEALED_BOX_V1` only in that project because a fresh smoke database has no trustee registrations. It does not change the default ElGamal scheme of the team deployment. The fake Gateway provides schema-valid event/anchor/proof reads but does not provide a genuine registry or blockchain.

`devtools/worker_smoke.py` creates one local election and casts one ballot. Its expected results are R1–R3 PASS and R4/R5 INCONCLUSIVE with `NODE_UNAVAILABLE`. It allows the worker's evidence-wait deadline and prints `official_T01_run: false`. Use the isolated Compose helper already reviewed for that smoke project; this small overlay alone does not replace the current integration stack's real node/bootstrap dependencies. The November 12 evidence remains a historical result, not validation of this new patch.

## Rehearsal evidence still needed

On the real team deployment, run the agreed E1/T01 flow, collect the exact TALLY checkpoint's five findings, show zero required mismatch counters, record node/worker health and sanitized poll counters, and then test the contract's actual finalization transaction. Keep Harits's committed scenario expectations unchanged. The upstream contract at the tested base `95793b3` lacks `finalizeElection`, so successful E1 finalization also requires Harits's G18 implementation to be merged and deployed. It also lacks `proposalState`; administrative-action elections require that interface before R4 can fully verify their proposal evidence. A clean election with no proposals does not call that getter.
