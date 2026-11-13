# Rule Engine service and Docker evidence

The HTTP worker polls Core, evaluates rules at accepted checkpoints, signs a finding, registers its hash at the Gateway, then sends the same durable body to Core. Nothing calls the worker. The previous fixture simulation remains available with `python rules/run_simulation.py`.

## Capture Docker evidence with a development Gateway

On a local Docker Desktop host using **Linux containers**, with Compose **v2.24.4+** and Python 3.11+, open
PowerShell at the repository root and run:

```powershell
docker version
docker compose version
.\.venv\Scripts\python.exe rules\devtools\compose_evidence.py
```

Use the existing rules virtual environment shown above, or `py -3.11 rules/devtools/compose_evidence.py`.
The helper itself uses only Python's standard library.

The helper creates a fresh, randomly named Compose project. It uses only throwaway credentials, does
not read the project's private `.env`, and publishes no host ports. It uses the merged integration Rule Engine
service and a temporary final overlay that restores the stand-in Gateway's Core image, removes its real-chain
settings and mounts, clears its node/registry dependencies, and makes staff setup depend only on PostgreSQL.
Only `db`, `staff-setup`, `core`, the stand-in `ledger-gateway`, and `rule-engine` are started. It builds those services,
waits for the real Docker healthcheck, and runs this repository's `worker_smoke.py` inside the real Core container.
The stand-in Gateway/RPC and the one synthetic election are for this developer check only. The script tears down
its own containers and volumes; it does not start the web service or modify the team's stack.

The script prints the path of a **new** UTF-8 JSON file under `purevoting-evidence` in your home folder. Attach the
JSON (or a screenshot showing those fields) **only after** the script exits successfully and the report says
`passed: true`. It contains worker health before/after, the first poll (often all zero on a fresh DB), the first
poll that stored findings, and the actual smoke result: one ballot, five findings, R1–R3 PASS and R4/R5
INCONCLUSIVE. It never exports raw Docker logs, inspect environment blocks, tokens or credentials. If the command
fails, its evidence file records the failing stage rather than reporting a pass. This does not verify the **real**
Gateway, PureChain signer/anchors or the official T01 scenario. For those, use the integration stack only after
Harits supplies the agreed pins, deployment address and healthy real Gateway.

## Development smoke scope

Use `compose_evidence.py` above for the isolated local check. The current integration file also defines real nodes,
contract deployment and a registry setup job that writes `.env`. The `compose.worker-smoke.yml` overlay alone does
not remove those dependencies or the real Gateway image and data mount; the helper's final temporary overlay is
required for this developer run. No team `.env` or chain bootstrap is needed, and the helper does not change either.

The check creates one election, issues 20 links, submits one sealed ballot and reads the stored findings for its
checkpoint. Its Gateway and RPC are test doubles: they do not independently verify inclusion, organizational
signatures or the on-chain signer registry. Expect R1/R2/R3 PASS and R4/R5 INCONCLUSIVE; other results cause failure.
This checks one development path, does not execute official T01/E1/E2 or part B, and supplies no independent expected
labels. A passing Docker run must be collected on the target host; source/configuration review alone is not runtime
evidence. The helper removes its own disposable containers and named volumes after the check.

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
