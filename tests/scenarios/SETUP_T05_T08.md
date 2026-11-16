# T05 and T08 setup (Seungmin)

Each variant folder (`T05-api`, `T05-db-add`, `T08a`, `T08b`, `T08c`) has `expected.json` (Harits, basis in
`LABELS_T05_T08.md`) and a `setup.py` that runs the T01 flow (`T01/setup.py`: election, roster, freeze, links, 3
ballots, receipts ANCHORED, the ballots' checkpoint committed) and adds one fault from `tamper.py`.

| Variant | Fault | When | Judged checkpoint |
|---|---|---|---|
| T05-api | `PUT /roster` after the freeze; must be 409 with the roster unchanged (else FAIL) | right after the freeze | as T01 |
| T05-db-add | one ACTIVE `credential.roster` row inserted, `added_checkpoint` = Core's open checkpoint read in the same statement | after the ballots' checkpoint is committed | that `added_checkpoint` |
| T08a | one `ballot.ballot_receipts` row: `receipt_hash` changed | same | Core's open checkpoint at the edit |
| T08b | same row: first byte of `sealed_ballot` flipped, `ballot_hash` kept | same | same |
| T08c | same row: first byte flipped and `ballot_hash` = H(new bytes) | same | same |

These are the conditions of `LABELS_T05_T08.md`. The script stops with NOT COMPLETED when one does not hold:

- T08: the edited row's checkpoint is not committed yet;
- T05-db-add: the `added_checkpoint` is already committed, which would be the variant T05-db-add-old.

Judging, verdicts, exit codes and the run record are as in `T01/README.md`. The record goes to `<variant>-runs/` (not
committed) and also holds `injection` (checkpoint numbers only). `cause_code` is not judged here; it is checked with
B1.

## Run

Use the same stack and account as T01 (`T01/README.md`). The database edits run `docker compose exec -T db psql -U postgres` from the repository root. Set `COMPOSE` the same way as for `tools/trustees.sh`. Use one fresh
election per run; the edits stay in the database.

```sh
export COMPOSE="docker compose -f docker-compose.yml -f docker-compose.integration.yml"
T01_USERNAME=admin1 T01_PASSWORD=... python tests/scenarios/T08a/setup.py --config demo --seed 1
```
```powershell
$env:COMPOSE = "docker compose -f docker-compose.yml -f docker-compose.integration.yml"
$env:T01_USERNAME = "admin1"; $env:T01_PASSWORD = "..."; python tests/scenarios/T08a/setup.py --config demo --seed 1
```

Every T01 option applies. With `quick` (60 s window) the judged checkpoint may come after the window ends, and the
script then stops. Use `demo`.

## Known before the first run

- **T05-db-add:** `rules/purevoting_rules/engine.py` sets `roster_chain_breaks` and `root_mismatches` to None (not
  implemented, Anik). Until they are implemented the run is "partial" or FAIL, not a pass. This decides whether the
  M2 E2 demo can use this variant or should use T08a instead.
- **Race:** the T05-db-add insert reads the open checkpoint `FOR SHARE`, so it waits for a checkpoint commit in
  progress and cannot land between Core's snapshot and its increment of the open checkpoint. For T08 the script
  compares the row's checkpoint with the committed one read just before the edit. A commit during the edit only
  adds mismatches at the judged checkpoint.
- **Partial:** while R4 and R5 list unimplemented checks, a run that matches its label still ends as
  "<variant> (partial R4/R5)" with exit 2, like T01.
