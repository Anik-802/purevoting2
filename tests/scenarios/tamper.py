"""T05 and T08 variants (docs/test-plan.md): the T01 flow (T01/setup.py) plus one fault, judged against the variant's
expected.json (Harits, tests/scenarios/LABELS_T05_T08.md). Each variant folder's setup.py calls main() here. How to
run: SETUP_T05_T08.md.

  T05-api     after the freeze, PUT the roster again: Core must answer 409 and change nothing
  T05-db-add  after the ballots' checkpoint is committed, INSERT one ACTIVE roster row in the database with
              added_checkpoint = Core's open checkpoint (read in the same statement)
  T08a        after that checkpoint, change one ballot_receipts row's receipt_hash
  T08b        ... change one byte of sealed_ballot, ballot_hash kept
  T08c        ... change one byte of sealed_ballot and set ballot_hash = H(new bytes)

The database edits run as the postgres superuser through `$COMPOSE exec -T db psql` (COMPOSE as in tools/trustees.sh).
The edited row must already be in a committed checkpoint and the judged checkpoint is the one that commits the edit
(Core's open checkpoint at the time of the edit); the script stops (NOT COMPLETED) if either does not hold.
Nothing secret is printed: the SQL holds only the election id, and psql returns checkpoint numbers.
"""
import importlib.util
import os
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("t01_setup", HERE / "T01" / "setup.py")
t01 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(t01)

ELECTION_ID = re.compile(r"elc_[0-9A-HJKMNP-TV-Z]{26}")   # core/app/models.py ELC
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# one ballot_receipts row of the election, picked by its key; SET clauses use the row's old values
ROW_EDITS = {
    "T08a": "receipt_hash = encode(sha256(convert_to(b.receipt_hash || 'T08a', 'UTF8')), 'hex')",
    "T08b": "sealed_ballot = set_byte(b.sealed_ballot, 0, get_byte(b.sealed_ballot, 0) # 1)",
    "T08c": "sealed_ballot = set_byte(b.sealed_ballot, 0, get_byte(b.sealed_ballot, 0) # 1), "
            "ballot_hash = encode(sha256(set_byte(b.sealed_ballot, 0, get_byte(b.sealed_ballot, 0) # 1)), 'hex')",
}


def row_edit_sql(variant: str, eid: str) -> str:
    """Returns one line "open_checkpoint|received_checkpoint" when exactly one row was changed."""
    return (
        "WITH target AS (SELECT nullifier_hash FROM ballot.ballot_receipts "
        f"WHERE election_id = '{eid}' ORDER BY nullifier_hash LIMIT 1), "
        f"upd AS (UPDATE ballot.ballot_receipts b SET {ROW_EDITS[variant]} FROM target t "
        f"WHERE b.election_id = '{eid}' AND b.nullifier_hash = t.nullifier_hash RETURNING b.received_checkpoint) "
        f"SELECT (SELECT open_checkpoint FROM credential.elections WHERE election_id = '{eid}'), "
        "received_checkpoint FROM upd;"
    )


def roster_insert_sql(eid: str, voter_key: str, ref: str) -> str:
    """Returns one line: the added_checkpoint, which is Core's open checkpoint when the row is inserted. FOR SHARE
    waits for a checkpoint commit in progress (Core holds the election row FOR UPDATE while it commits)."""
    return (
        "INSERT INTO credential.roster (election_id, voter_key, name, email, external_ref, added_checkpoint) "
        f"SELECT '{eid}', '{voter_key}', 'injected voter', NULL, '{ref}', open_checkpoint "
        f"FROM credential.elections WHERE election_id = '{eid}' FOR SHARE RETURNING added_checkpoint;"
    )


def psql(sql: str) -> list[str]:
    compose = shlex.split(os.environ.get("COMPOSE", "docker compose"))
    cmd = compose + ["exec", "-T", "db", "sh", "-c",
                     'psql -X -q -t -A -v ON_ERROR_STOP=1 -U postgres -d "$POSTGRES_DB"']
    try:
        r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=60, cwd=HERE.parents[1])
    except (OSError, subprocess.SubprocessError) as exc:
        raise t01.Stop(f"database edit not run: {type(exc).__name__}") from exc
    if r.returncode != 0:
        raise t01.Stop(f"database edit failed (psql exit {r.returncode}): {r.stderr.strip()[:200]}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def committed(ctx) -> int:
    return ctx["staff_election"]()["counters"]["as_of_checkpoint"]


def check_election_id(eid: str) -> str:
    if not ELECTION_ID.fullmatch(eid):
        raise t01.Stop("unexpected election id format; not put into SQL")
    return eid


def roster_put_after_freeze(ctx) -> None:
    """T05-api: the roster PUT after the freeze is refused (409) and the roster is unchanged."""
    c, auth, eid, rec = ctx["client"], ctx["auth"], ctx["election_id"], ctx["rec"]
    before = ctx["staff_election"]().get("roster_hash")
    r = c.put(f"/v1/elections/{eid}/roster", headers=auth,
              json={"voters": [{"external_ref": "T05-api-extra", "name": "added after freeze"}]})
    rec["checks"]["roster_put_after_freeze"] = {"status": r.status_code,
                                                "code": (r.json().get("code") if r.status_code >= 400 else None)}
    n = len(t01.expect(c.post(f"/v1/elections/{eid}/roster:read", headers=auth), 200).json()["voters"])
    same = before is not None and ctx["staff_election"]().get("roster_hash") == before
    rec["checks"]["roster_size_after_put"], rec["checks"]["roster_hash_unchanged"] = n, same
    t01.log(f"roster PUT after freeze: HTTP {r.status_code}; roster has {n} voters, roster_hash unchanged: {same}")
    if r.status_code != 409 or n != ctx["voters"] or not same:
        raise t01.Failed(f"roster PUT after freeze: HTTP {r.status_code}, {n} voters (expected 409, {ctx['voters']})")


def roster_insert(ctx, k: int) -> int:
    """T05-db-add: one ACTIVE row as of Core's open checkpoint, which must not be committed yet."""
    eid, rec = check_election_id(ctx["election_id"]), ctx["rec"]
    voter_key = "vtr_" + "".join(secrets.choice(CROCKFORD) for _ in range(26))
    out = psql(roster_insert_sql(eid, voter_key, "T05-injected"))
    if len(out) != 1:
        raise t01.Stop(f"roster insert returned {len(out)} rows")
    added = int(out[0])
    done = committed(ctx)
    rec["injection"] = {"kind": "roster row inserted in the database", "added_checkpoint": added,
                        "committed_at_insert": done}
    t01.log(f"inserted one ACTIVE roster row in the database, added_checkpoint {added} (committed: {done})")
    if added <= done:   # LABELS_T05_T08.md: an already committed value is the variant T05-db-add-old
        raise t01.Stop(f"added_checkpoint {added} is already committed (T05-db-add-old, not this label)")
    return added


def row_edit(variant: str):
    def hook(ctx, k: int) -> int:
        """T08a/b/c: edit one ballot row that a committed checkpoint already contains."""
        eid, rec = check_election_id(ctx["election_id"]), ctx["rec"]
        done = committed(ctx)
        out = psql(row_edit_sql(variant, eid))
        if len(out) != 1:
            raise t01.Stop(f"ballot row edit changed {len(out)} rows (expected 1)")
        open_k, received = (int(x) for x in out[0].split("|"))
        rec["injection"] = {"kind": f"ballot_receipts row edited ({variant})", "row_received_checkpoint": received,
                            "open_checkpoint_at_edit": open_k, "committed_before_edit": done}
        t01.log(f"edited one ballot_receipts row ({variant}); row in checkpoint {received}, "
                f"committed {done}, judged checkpoint {open_k}")
        if received > done:   # LABELS_T05_T08.md: tampered before its checkpoint = a different scenario
            raise t01.Stop(f"the edited row's checkpoint {received} was not committed yet")
        return open_k
    return hook


VARIANTS = {
    "T05-api": {"after_freeze": roster_put_after_freeze},
    "T05-db-add": {"after_commit": roster_insert},
    "T08a": {"after_commit": row_edit("T08a")},
    "T08b": {"after_commit": row_edit("T08b")},
    "T08c": {"after_commit": row_edit("T08c")},
}


def main(variant: str, argv=None) -> int:
    return t01.main(argv, scenario=variant, folder=HERE / variant, hooks=VARIANTS[variant])


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in VARIANTS:
        sys.exit(f"usage: {sys.argv[0]} {{{','.join(VARIANTS)}}} [T01 options]")
    sys.exit(main(sys.argv[1], sys.argv[2:]))
