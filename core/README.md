# core/ — Core API (owner: Seungmin)

Implements `contracts/core-api.openapi.yaml`. Role design: `docs/roles/core-web.md`. Work log (Korean): `DEVLOG.ko.md`.

| Path | What |
|---|---|
| `app/main.py` | FastAPI app, routers, background loops (scheduler, outbox worker) |
| `app/routes_public.py` | voter endpoints (public election, redeem, ballots, receipts) and staff login |
| `app/routes_staff.py` | staff endpoints: elections, roster, freeze, link CSV, staff views, alerts |
| `app/routes_internal.py` | `/internal/v1/*`: elections, checkpoints, views, findings, audit alerts |
| `app/elections.py` | election creation (key pair, `config_hash`, registry), roster, freeze (D-10), token issue |
| `app/voting.py` | redeem (one transaction across both schemas) and ballots (idempotent) |
| `app/lifecycle.py` | checkpoints (six snapshot tables) and boundary transitions: open, PERIODIC, close + CLOSE_FINAL |
| `app/ledger.py`, `app/outbox.py` | ledger events into the outbox; delivery per time bucket, outcome handling |
| `app/views.py` | the seven views "as of checkpoint k" |
| `app/findings.py`, `app/schemas.py` | finding storage (single acceptance condition), JSON Schema checks against `contracts/` |
| `app/chain.py`, `app/creation_check.py` | pinned-node JSON-RPC reader (URLs tried in order); creation check with confirmation depth D, Gateway `/v1/chain` comparison; `stub` mode for dev only |
| `app/hashing.py` | `contracts/hashing.md` (H, Hc, Merkle, roots, signature digests) |
| `app/scripts/create_staff.py` | staff-account setup script (no account API) |
| `devtools/fake_gateway.py` | stateful stand-in Gateway for tests and local runs, plus `/rpc`, a stand-in for the pinned node |
| `devtools/demo_flow.py` | real-time demo of one election, creation to close (about 3 minutes, Korean output) |
| `db/init/` | first-start DB setup: roles, schemas `credential` / `ballot`, tables, SECURITY DEFINER ballot functions |
| `tests/` | unit tests (39 hash vectors, …) and `test_flow_db.py` (week 2 path on a real DB) |

```bash
# unit tests (no database needed)
cd core && pip install -r requirements-dev.txt && pytest -q

# + the week 2 flow on a real database (pv2_core of a DB initialised with db/init)
TEST_DATABASE_URL=postgresql+psycopg://pv2_core:<DB_CORE_PASSWORD>@127.0.0.1:5432/purevoting pytest -q

# full stack with the stand-in Gateway (from the repository root, after copying .env.example to .env)
docker compose --profile mock up --build
docker compose exec -e STAFF_PASSWORD=... core python -m app.scripts.create_staff --username admin1 --org org_commission --role admin
# then http://127.0.0.1:8000/docs

# real-time demo against the running stack: create → roster → freeze → links → open → vote → close → anchors
docker compose exec core python -m devtools.demo_flow
# (the creation check reads the pinned node: with the mock, PINNED_NODE_URL=http://ledger-gateway:8100/rpc in .env)
```

The DB init scripts run only when the `pgdata` volume is empty; after changing them use `docker compose down -v`.

Not done yet (later weeks): T01 against the real Gateway and Rule Engine (waiting for their services), admin actions,
tally, finalize, review relay, comparing ledger events with the outbox, AI reports, service logs, the web apps.
