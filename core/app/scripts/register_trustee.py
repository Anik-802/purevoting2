"""Registers an organization's trustee key (D-30). Runs in the deployment like create_staff, before elections exist.

    python -m app.scripts.register_trustee --file org_observer.public.json
    (the public record printed by `python -m app.scripts.trustee keygen`; never the secret file)

A key already registered for the organization is left unchanged unless --replace is given; elections keep the keys
they were created with. Run it with the DB connection of Core (DATABASE_URL).
"""
import argparse
import json
import sys

from sqlalchemy import text

from app import elgamal
from app.db import get_engine
from app.store import jsonb


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--file", required=True)
    p.add_argument("--replace", action="store_true", help="replace a different key already registered")
    a = p.parse_args(argv)
    with open(a.file, encoding="utf-8") as f:
        record = json.load(f)
    if "secret" in record:
        p.error("this is a secret file; register the public record only")
    if set(record) != {"org_id", "public_key", "proof"} or not elgamal.verify_trustee_key(record):
        p.error("not a valid trustee public record (its proof does not verify)")
    org = record["org_id"]
    with get_engine().begin() as conn:
        row = conn.execute(text("SELECT record FROM credential.trustees WHERE org_id = :o"), {"o": org}).first()
        if row is not None and row.record == record:
            print(f"{org}: already registered, unchanged")
            return 0
        if row is not None and not a.replace:
            p.error(f"{org} already has a different key; use --replace to change it")
        conn.execute(text("INSERT INTO credential.trustees (org_id, record) VALUES (:o, CAST(:r AS jsonb)) "
                          "ON CONFLICT (org_id) DO UPDATE SET record = EXCLUDED.record, registered_at = now()"),
                     {"o": org, "r": jsonb(record)})
    print(f"{org}: registered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
