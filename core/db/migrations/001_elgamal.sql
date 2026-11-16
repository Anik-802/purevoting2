-- D-30 (ElGamal ballots) for a database created before it; a new database gets the same from init/10_schema.sql.
-- Keeps every existing row: earlier elections stay SEALED_BOX_V1 with their private key. Safe to run twice.
-- Run as the superuser, e.g.:
--   docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" < core/db/migrations/001_elgamal.sql
BEGIN;
SET ROLE pv2_credential;

ALTER TABLE credential.elections
    ADD COLUMN IF NOT EXISTS ballot_scheme text NOT NULL DEFAULT 'SEALED_BOX_V1'
        CHECK (ballot_scheme IN ('SEALED_BOX_V1', 'ELGAMAL_ED25519_V1')),
    ADD COLUMN IF NOT EXISTS trustee_keys jsonb,
    ALTER COLUMN private_key DROP NOT NULL;
ALTER TABLE credential.elections DROP CONSTRAINT IF EXISTS elections_scheme_keys;
ALTER TABLE credential.elections ADD CONSTRAINT elections_scheme_keys
    CHECK ((ballot_scheme = 'SEALED_BOX_V1') = (private_key IS NOT NULL)
           AND (ballot_scheme = 'ELGAMAL_ED25519_V1') = (trustee_keys IS NOT NULL));

CREATE TABLE IF NOT EXISTS credential.trustees (
    org_id         text PRIMARY KEY,
    record         jsonb NOT NULL,
    registered_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS credential.decryption_shares (
    election_id  text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    org_id       text NOT NULL,
    share        jsonb NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (election_id, org_id)
);

RESET ROLE;
GRANT SELECT, INSERT, UPDATE, DELETE ON credential.trustees, credential.decryption_shares TO pv2_core;
COMMIT;
