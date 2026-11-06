# Rule Engine service — November 9 handoff

The HTTP worker polls Core, evaluates rules at accepted checkpoints, signs a finding, registers its hash at the Gateway, then sends the same durable body to Core. Nothing calls the worker. The previous fixture simulation remains available with `python rules/run_simulation.py`.

## Run against a local development stack

Use a **fresh development project and credentials**. This stack writes a real PostgreSQL database. Its Gateway and RPC are test doubles, not PureChain. They do not independently verify inclusion, organizational signatures or the on-chain signer registry. Do not use this overlay for the official T01, E1/E2, or part B run.

1. Copy the repository's `.env.example` to a private `.env`. Fill all required passwords, service keys, JWT secret and staff setup credentials. Use distinct, disposable keys. The worker needs its own valid secp256k1 private key in `RULE_ENGINE_SIGNER_KEY`; never send it in a progress report. For **this development setup only**, choose a positive numeric `CHAIN_ID`, a nonzero test `CONTRACT_ADDRESS`, `CONFIRMATION_DEPTH=2`, and `PINNED_NODE_URL=http://ledger-gateway:8100/rpc`. For a team deployment use the independently agreed pins and registered signer.
2. Run the following from the repository root using Docker Compose v2. Stop competing local services on those ports or choose separate published host ports. This worker intentionally does not run in the default part-B mock stack. The command below is provided for the demo PC; Docker execution was not tested in the authoring environment.

   ```sh
   docker compose -p pv-anik-dev --profile mock -f docker-compose.yml -f docker-compose.integration.yml -f rules/devtools/compose.worker-smoke.yml up -d --build
   ```
3. To create a disposable election through the Core API, use a local Python 3.11+ environment with `core/requirements.txt` installed (keep it separate from the rules environment). Set `SMOKE_USERNAME` and `SMOKE_PASSWORD` to the staff setup credentials in that private development database. Run:

   ```sh
   python rules/devtools/worker_smoke.py --create-local-election --core http://127.0.0.1:8000
   ```

The check creates one election, issues 20 links, submits one sealed ballot and reads the stored findings for its checkpoint. Expect R1/R2/R3 PASS and R4/R5 INCONCLUSIVE. It exits nonzero for other results. This checks one development path; it does not execute the formal scenario script or supply anyone's expected labels. Shut down using the same compose arguments and `down`. The named data volumes persist.

## Run directly or against the real Gateway

With a rules Python 3.11+ virtual environment active:

```sh
python -m pip install -r rules/requirements-lock.txt
python -m pytest -c rules/pytest.ini rules/tests -q
cd rules
python -m purevoting_rules.live_worker --once
python -m purevoting_rules.live_worker
```

For direct execution export `CORE_URL`, `GATEWAY_URL`, `RULE_KEY`, `RULE_ENGINE_SIGNER_KEY`, `CHAIN_ID`, `CONTRACT_ADDRESS`, `PINNED_NODE_URL`, `CONFIRMATION_DEPTH`, and a writable private `RULE_STATE_DIR`. For local real-Core + dev-Gateway processes, use the host URLs that match the Gateway's configured pins. The real Gateway uses the same API; no mock flag exists on the worker. With Harits’s healthy Gateway and nodes available and merged, use the base + integration compose files only. No service contacts a model provider.

The worker uses one state directory per deployment and signer, one writer, a persistent findings database, and a separate poll journal. `RULE_POLL_SECONDS` defaults to 60; `RULE_PARTIAL_RECHECK_SECONDS` to 600; `ANCHOR_BATCH_SECONDS` to 60. Publication retries pending bodies on subsequent polls. A durable 4xx rejection needs investigation; increasing the poll rate does not correct an invalid request. `--healthcheck` only checks worker liveness. Review poll counters (`read_errors`, `retry`, `rejected`, `alert_errors`) and verify stored findings to determine integration readiness. No keys are passed to independent node RPCs.

## What the evidence means

- Reads are cumulative through checkpoint k. The adapter validates the identity, pagination, checkpoint and shape of the responses against the checked-out contracts. A missing rejection route does not mean an empty rejection list. Missing Core views remain unavailable to the affected rules.
- The worker uses the pinned node list in local order and compares Gateway chain configuration. A depth check on a reported block is **not** a proof of the anchor. With a valid Gateway-accepted checkpoint but missing independent confirmation, it waits; after the deadline it can evaluate rules with off-chain evidence while leaving unsupported chain checks inconclusive. Malformed/unaccepted commitments are not audited as confirmed. The wait starts at the worker's first observation of Core's ACCEPTED status; downtime or a long poll can move it later than the real acceptance time.
- R4 and R5 still list their unimplemented checks in each finding summary. Reading the pinned node's ID and head does not finish registry/signature verification, anchor/leaf verification, historical state replay, or tally decryption. An audit report generator, local model, and formal T01/T02/T04/T05/T08/T15 results are outside this patch.
- The unmodified Core development Gateway does not implement the worker's full reads. The opt-in development overlay provides the missing event timestamps, cutoff filter, mock rejection read and chain-ID RPC for this smoke. Its simulated records are not a complete replay of a real Gateway, and it will explicitly refuse missing rejection evidence. The November 19 mock UI stack remains separate.
- Before merging, verify the compose stack and process-restart persistence on the target host and volume. Keep Harits's expected labels independent and committed before formal runs.
