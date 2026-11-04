# T01 — normal vote

Pass criterion (`docs/test-plan.md`): 201 + receipt; receipt status becomes ANCHORED; R1–R5 PASS at the next
checkpoint; ELECTION_OPENED sent a few seconds early by the chain clock gets RETRY_LATER and is applied, never REJECTED.

| File | Owner | |
|---|---|---|
| `setup.py` | Seungmin | drives the running stack over Core's HTTP API, judges, writes a run record |
| `expected.json` | Harits | ground-truth label, committed **before** the first run (`../README.md`) |

## Run

The whole stack must be up with the integration override (nodes, deployed contract and registry, Core, the real
Gateway and the Rule Engine; no mock profile, `CREATION_CHECK_MODE=node`). Its `staff-setup` step creates the admin
account from `STAFF_ADMIN_USERNAME` / `STAFF_ADMIN_PASSWORD` in `.env`:

```sh
docker compose -f docker-compose.yml -f docker-compose.integration.yml up -d --build
```

Then, from the repository root (needs `httpx` and `pynacl`, e.g. `pip install -r core/requirements.txt`), with that
account:

```sh
T01_USERNAME=admin1 T01_PASSWORD=... python tests/scenarios/T01/setup.py --config demo --seed 1
```
```powershell
$env:T01_USERNAME = "admin1"; $env:T01_PASSWORD = "..."; python tests/scenarios/T01/setup.py --config demo --seed 1
```

| `--config` | Voters | Window | Bucket = checkpoint interval | Votes |
|---|---|---|---|---|
| `evaluation` (system-design 10.2) | 100 | 2 h | 900 s | 3 |
| `demo` (system-design 10.2, default) | 100 | 10 min | 60 s | 3 |
| `quick` (development, a few minutes) | 20 | 60 s | 30 s | 3 |

Changing a value (`--voters`, `--bucket`, `--buckets`, `--votes`) records the configuration as `custom`. Seeds 1–10
for the experiment (the seed picks which voters vote and their candidates). `--lead` (default 180 s) is the time
before window_start for the creation check, freeze and link issue; raise it if the chain is slow.

## What the script checks

1. Election (orgs in ascending order) → roster → creation check PASSED → freeze → links.
2. Status OPEN after window_start (Core opens only when the Gateway accepted ELECTION_OPENED); the opening transition
   REJECTED is a FAIL.
3. Voters: redeem 200 → ballot 201 + receipt; every receipt reaches ANCHORED.
4. **Judged checkpoint C** = Core's open checkpoint right after the last ballot, i.e. the checkpoint that commits it
   (one later if Core committed one in between, never earlier). Each rule's result at C follows contracts/README.md
   "Review and supersession": a FAIL at C stands; otherwise a PASS at C decides; an INCONCLUSIVE at C waits for a
   later PASS of the rule (at C or higher). Result and non-zero `observed` keys are compared with `expected.json`;
   a FAIL at any other checkpoint is also a difference (FAIL is never superseded).
   Polling stops once all five rules are decided at C, so a finding recorded later for a higher checkpoint is not
   in the record (the criterion is the next checkpoint).
5. No TRANSITION_REJECTED alert. Whether the Gateway answered RETRY_LATER first is seen in the Gateway's log; the
   script records every alert.

| Verdict | Exit |
|---|---|
| `PASS` — as labelled | 0 |
| `FAIL` — differs from the label, a receipt LEDGER_REJECTED, or the opening REJECTED | 1 |
| `T01 (partial R4/R5)` — as labelled, but a deciding finding's `summary` says a check is not implemented (`docs/roles/rules-ai.md` week 3); not a T01 pass | 2 |
| `NOT JUDGED` (no `expected.json`) or `NOT COMPLETED` (stopped before the end) | 2 |

The run record (`t01-runs/T01-seed<n>-<time>.json`, not committed) holds the commit (`-dirty` if the tree had
changes), seed, configuration and values, timings, every finding of the election, the decided result per rule and the
alerts; tokens, credentials and receipt hashes are never in it.
