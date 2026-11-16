-- PureVoting 2.0 Core database (D-06): one PostgreSQL database, two schemas owned by different roles.
-- Runs once at first start (after 00_roles.sh). Source of the rules referenced below: contracts/ v1.0.
--
-- Storage rules from contracts/README.md "Shared conventions":
--   * rows about voters (token use, credential, ballot, outbox voter events) store only bucket and checkpoint
--     values, never exact times; their primary keys are random IDs or hashes, never serial numbers;
--   * every field of every view row is a function of the data as of checkpoint k, so facts that change later are
--     stored with the checkpoint in which they changed (README "Checkpoints").
-- No column joins the credential schema to the ballot schema (threat-model S2).
-- track_commit_timestamp must stay off (docker-compose.yml passes -c track_commit_timestamp=off).

-- =====================================================================================================
-- schema credential (owner pv2_credential)
-- =====================================================================================================
CREATE SCHEMA credential AUTHORIZATION pv2_credential;
SET ROLE pv2_credential;

-- Election settings. Not a snapshot table: the election_config view is verified through config_hash.
CREATE TABLE credential.elections (
    election_id                 text PRIMARY KEY CHECK (election_id ~ '^elc_[0-9A-HJKMNP-TV-Z]{26}$'),
    title                       text NOT NULL,
    candidates                  jsonb NOT NULL,              -- [{candidate_id, name}] sorted by candidate_id
    config_window_start         timestamptz NOT NULL,        -- window at creation (hashed in config_hash)
    config_window_end           timestamptz NOT NULL,
    time_bucket_seconds         integer NOT NULL CHECK (time_bucket_seconds >= 1),   -- no default (D-10)
    checkpoint_interval_seconds integer NOT NULL CHECK (checkpoint_interval_seconds >= 30
                                    AND checkpoint_interval_seconds % time_bucket_seconds = 0),
    organizations               jsonb NOT NULL,              -- [{org_id, signer}] sorted by org_id, from the registry
    approval_threshold          smallint NOT NULL CHECK (approval_threshold >= 2),
    proposal_ttl_hours          integer NOT NULL CHECK (proposal_ttl_hours BETWEEN 1 AND 168),
    operator_org                text NOT NULL,               -- from the on-chain registry, never from the login
    ballot_scheme               text NOT NULL DEFAULT 'SEALED_BOX_V1'
                                    CHECK (ballot_scheme IN ('SEALED_BOX_V1', 'ELGAMAL_ED25519_V1')),   -- D-30
    public_key                  bytea NOT NULL CHECK (octet_length(public_key) = 32),   -- X25519 (D-08) or the ElGamal joint key (D-30)
    private_key                 bytea CHECK (octet_length(private_key) = 32),  -- SEALED_BOX_V1 only, revealed from TALLY (D-23)
    trustee_keys                jsonb,                       -- ELGAMAL_ED25519_V1: [{org_id, public_key, proof}] by org_id
    CONSTRAINT elections_scheme_keys CHECK ((ballot_scheme = 'SEALED_BOX_V1') = (private_key IS NOT NULL)
                                            AND (ballot_scheme = 'ELGAMAL_ED25519_V1') = (trustee_keys IS NOT NULL)),
    public_key_hash             char(64) NOT NULL,
    config_hash                 char(64) NOT NULL,
    creation_check              text NOT NULL DEFAULT 'PENDING' CHECK (creation_check IN ('PENDING', 'PASSED', 'MISMATCH')),
    created_block               bigint,                      -- block_number of the accepted ELECTION_CREATED (creation check)
    open_checkpoint             bigint NOT NULL DEFAULT 0,
    open_at                     timestamptz,                 -- boundary at which the scheduler opens (set when every voter has a token)
    open_event_id               text,                        -- ELECTION_OPENED waiting for ACCEPTED (permissive transition)
    opened_at                   timestamptz,                 -- ELECTION_OPENED.occurred_at; PERIODIC checkpoints count from it
    fifo_stopped                boolean NOT NULL DEFAULT false,
    ledger_rejected             boolean NOT NULL DEFAULT false,   -- ELECTION_CREATED rejected: deletable
    created_at                  timestamptz NOT NULL DEFAULT now()     -- system data, not a voter row
);

-- Status and effective window end per checkpoint (election_config view "as of k").
CREATE TABLE credential.election_history (
    election_id  text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    checkpoint   bigint NOT NULL,
    status       text NOT NULL CHECK (status IN ('DRAFT', 'ROSTER_FROZEN', 'OPEN', 'CLOSED', 'TALLIED', 'FINALIZED', 'VOIDED')),
    window_end   timestamptz NOT NULL,
    PRIMARY KEY (election_id, checkpoint)
);

-- Snapshot table `roster`. Names and contacts are staff-only (never in views, events or logs).
CREATE TABLE credential.roster (
    election_id         text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    voter_key           text NOT NULL CHECK (voter_key ~ '^vtr_[0-9A-HJKMNP-TV-Z]{26}$'),   -- random, no timestamp
    name                text NOT NULL,
    email               text,
    external_ref        text,
    added_checkpoint    bigint NOT NULL,
    removed_checkpoint  bigint,
    PRIMARY KEY (election_id, voter_key),
    CHECK (removed_checkpoint IS NULL OR removed_checkpoint >= added_checkpoint)
);

-- Snapshot table `token_status`. Only token_hash is stored, never the raw token.
CREATE TABLE credential.token_status (
    election_id         text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    token_hash          char(64) NOT NULL,
    voter_key           text NOT NULL,                      -- needed for revocation / reissue (R4); staff-side only
    issued_checkpoint   bigint NOT NULL,
    used_checkpoint     bigint,
    revoked_checkpoint  bigint,
    PRIMARY KEY (election_id, token_hash),
    FOREIGN KEY (election_id, voter_key) REFERENCES credential.roster,
    CHECK (used_checkpoint IS NULL OR revoked_checkpoint IS NULL)   -- a USED token can never be revoked
);
CREATE INDEX token_status_voter ON credential.token_status (election_id, voter_key);

-- Snapshot table `admin_actions`; status as of k comes from admin_action_status.
CREATE TABLE credential.admin_actions (
    action_id             text PRIMARY KEY CHECK (action_id ~ '^act_[0-9A-HJKMNP-TV-Z]{26}$'),
    election_id           text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    action_type           text NOT NULL CHECK (action_type IN ('ROSTER_AMEND', 'TOKEN_REISSUE', 'WINDOW_CHANGE',
                                                               'ELECTION_CLOSE_EARLY', 'ELECTION_VOID')),
    requested_by_org      text NOT NULL,
    proposal_id           text NOT NULL UNIQUE,
    target                jsonb NOT NULL,                    -- non-PII, exactly as hashed into target_hash
    target_hash           char(64) NOT NULL,
    request_signature     text,
    requested_checkpoint  bigint NOT NULL
);

CREATE TABLE credential.admin_action_status (
    action_id   text NOT NULL REFERENCES credential.admin_actions ON DELETE CASCADE,
    checkpoint  bigint NOT NULL,
    status      text NOT NULL,
    PRIMARY KEY (action_id, checkpoint, status)
);

-- Snapshot table `rejected_attempts` (voter-side refusals and refused staff actions). Coarse time only.
CREATE TABLE credential.rejected_attempts (
    attempt_id    text PRIMARY KEY CHECK (attempt_id ~ '^att_[0-9A-HJKMNP-TV-Z]{26}$'),   -- random
    election_id   text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    kind          text NOT NULL,
    subject_hash  char(64),                                  -- token_hash or nullifier_hash, never raw values
    checkpoint    bigint NOT NULL
);

-- Gap-free system-event seq per election (README "Consistency model"). A SEQUENCE / SERIAL would leave gaps.
CREATE TABLE credential.seq_counters (
    election_id  text PRIMARY KEY REFERENCES credential.elections ON DELETE CASCADE,
    last_seq     bigint NOT NULL DEFAULT 0
);

-- Outbox: written in the same transaction as the change; released per time bucket (README "Delivery order").
CREATE TABLE credential.outbox (
    event_id       text PRIMARY KEY,                         -- evt_ (system, ULID) or evr_ (voter, random)
    election_id    text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    kind           text NOT NULL CHECK (kind IN ('system', 'voter')),
    seq            bigint,                                   -- system events only
    bucket_start   timestamptz NOT NULL,                     -- release unit
    event          jsonb NOT NULL,                           -- LedgerEventSubmission exactly as sent
    payload_hash   char(64) NOT NULL,
    status         text NOT NULL DEFAULT 'PENDING'
                   CHECK (status IN ('PENDING', 'ACCEPTED', 'DUPLICATE', 'REJECTED', 'CONFLICT')),
    reject_code    text,
    block_number   bigint,                                   -- system events: block including the transaction
    UNIQUE (election_id, seq),
    CHECK ((kind = 'system') = (seq IS NOT NULL))
);
CREATE INDEX outbox_release ON credential.outbox (election_id, bucket_start, kind, seq) WHERE status = 'PENDING';

-- Committed checkpoints (CHECKPOINT_COMMITTED payloads).
CREATE TABLE credential.checkpoints (
    election_id     text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    checkpoint      bigint NOT NULL,
    reason          text NOT NULL CHECK (reason IN ('PERIODIC', 'STATE_CHANGE', 'CLOSE_FINAL', 'TALLY')),
    boundary        timestamptz NOT NULL,
    tables          jsonb NOT NULL,                          -- snapshot hash + count of each of the six tables
    event_id        text NOT NULL,
    PRIMARY KEY (election_id, checkpoint)
);

-- Finding bodies stored by the Rule Engine (POST /internal/v1/findings) once their FINDING_RECORDED exists.
CREATE TABLE credential.findings (
    finding_id         text PRIMARY KEY CHECK (finding_id ~ '^fnd_[0-9A-HJKMNP-TV-Z]{26}$'),
    election_id        text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    body               jsonb NOT NULL,                       -- FindingSubmission exactly as received
    finding_hash       char(64) NOT NULL,                    -- Hc(body) = FINDING_RECORDED.finding_hash
    recorded_event_id  text NOT NULL,
    review_status      text NOT NULL CHECK (review_status IN ('NOT_REQUIRED', 'PENDING', 'RESOLVED', 'SUPERSEDED', 'CONFIRMED_ISSUE')),
    evidence_status    text NOT NULL DEFAULT 'PENDING' CHECK (evidence_status IN ('OK', 'UNRESOLVED', 'PENDING')),
    stored_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX findings_election ON credential.findings (election_id);

-- Alerts from the Rule Engine, the Gateway and Core itself. Idempotent by (election_id, checkpoint, kind, source).
CREATE TABLE credential.audit_alerts (
    election_id  text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    checkpoint   bigint NOT NULL,
    kind         text NOT NULL,
    source       text NOT NULL CHECK (source IN ('rule-engine', 'ledger-gateway', 'core-api')),
    raised_at    timestamptz NOT NULL,
    detail       text,
    PRIMARY KEY (election_id, checkpoint, kind, source)
);

-- Idempotency-Key records of staff create endpoints (core-api IdempotencyKey: same key + same body → original response).
-- For tokens:issue only the key and body hash are kept (the CSV holds raw tokens and lives 15 min in memory only);
-- a retry after the CSV is gone gets 409 even across a Core restart.
CREATE TABLE credential.idempotency (
    scope       text NOT NULL,                               -- e.g. create:<user_id>, issue:<election_id>
    key         text NOT NULL,
    body_hash   char(64) NOT NULL,
    response    jsonb,
    created_at  timestamptz NOT NULL,
    PRIMARY KEY (scope, key)
);

-- Staff accounts, created by the deployment setup script (no account API, threat-model O1).
CREATE TABLE credential.staff_users (
    user_id        text PRIMARY KEY CHECK (user_id ~ '^usr_[0-9A-HJKMNP-TV-Z]{26}$'),
    username       text NOT NULL UNIQUE,
    password_hash  text NOT NULL,
    org_id         text NOT NULL,
    role           text NOT NULL CHECK (role IN ('admin', 'approver', 'auditor'))
);

-- D-30: each organization's ElGamal public key with its proof of knowledge, registered by the deployment setup script
-- (app.scripts.register_trustee); the secret stays with the organization.
CREATE TABLE credential.trustees (
    org_id         text PRIMARY KEY,
    record         jsonb NOT NULL,                           -- {org_id, public_key, proof} as made by the trustee tool
    registered_at  timestamptz NOT NULL DEFAULT now()
);

-- D-30: verified decryption shares of the per-candidate aggregates, one per organization; anyone may submit, the proof
-- against the organization's registered key is the authentication.
CREATE TABLE credential.decryption_shares (
    election_id  text NOT NULL REFERENCES credential.elections ON DELETE CASCADE,
    org_id       text NOT NULL,
    share        jsonb NOT NULL,                             -- {org_id, shares: [{d, c, z}] in candidate_id order}
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (election_id, org_id)
);

RESET ROLE;

-- =====================================================================================================
-- schema ballot (owner pv2_ballot) — no column refers to the credential schema
-- =====================================================================================================
CREATE SCHEMA ballot AUTHORIZATION pv2_ballot;
SET ROLE pv2_ballot;

-- Snapshot table `credential_issuance`: only nullifier_hash = H(browser-generated credential) (D-07).
CREATE TABLE ballot.credential_issuance (
    election_id        text NOT NULL,
    nullifier_hash     char(64) NOT NULL,
    issued_checkpoint  bigint NOT NULL,
    used_checkpoint    bigint,
    PRIMARY KEY (election_id, nullifier_hash)
);

-- Snapshot table `ballot_receipts`: immutable once written.
CREATE TABLE ballot.ballot_receipts (
    election_id          text NOT NULL,
    nullifier_hash       char(64) NOT NULL,
    ballot_hash          char(64) NOT NULL,                  -- H(sealed_ballot raw bytes)
    sealed_ballot        bytea NOT NULL CHECK (octet_length(sealed_ballot) <= 3072),
    receipt_hash         char(64) NOT NULL,
    received_checkpoint  bigint NOT NULL,
    received_bucket      timestamptz NOT NULL,               -- bucketed acceptance time, never exact
    event_id             text NOT NULL UNIQUE CHECK (event_id ~ '^evr_[0-9A-HJKMNP-TV-Z]{26}$'),
    PRIMARY KEY (election_id, nullifier_hash),
    FOREIGN KEY (election_id, nullifier_hash) REFERENCES ballot.credential_issuance
);

-- Register a credential during redeem (called by Core inside the same transaction that marks the token USED).
-- Raises unique_violation if the nullifier is already registered (Core answers NULLIFIER_ALREADY_REGISTERED).
CREATE FUNCTION ballot.register_nullifier(p_election_id text, p_nullifier_hash text, p_checkpoint bigint)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ballot, pg_temp
AS $$
BEGIN
    INSERT INTO ballot.credential_issuance (election_id, nullifier_hash, issued_checkpoint)
    VALUES (p_election_id, p_nullifier_hash, p_checkpoint);
END;
$$;

-- Cast a ballot with a registered credential (core-api POST /v1/ballots).
-- Returns 'CREATED' (stored), 'RETRY' (same ballot_hash already stored: identical retry),
-- 'CONFLICT' (credential already used with another ballot) or 'UNKNOWN' (credential not registered).
-- The window check is done by Core after the idempotency check (core-api), before calling with a fresh ballot.
CREATE FUNCTION ballot.cast_ballot(p_election_id text, p_nullifier_hash text, p_ballot_hash text,
                                   p_sealed_ballot bytea, p_receipt_hash text, p_checkpoint bigint,
                                   p_bucket timestamptz, p_event_id text)
RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ballot, pg_temp
AS $$
DECLARE
    v_used bigint;
    v_existing text;
BEGIN
    SELECT used_checkpoint INTO v_used
      FROM ballot.credential_issuance
     WHERE election_id = p_election_id AND nullifier_hash = p_nullifier_hash
       FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 'UNKNOWN';
    END IF;
    IF v_used IS NOT NULL THEN
        SELECT ballot_hash INTO v_existing
          FROM ballot.ballot_receipts
         WHERE election_id = p_election_id AND nullifier_hash = p_nullifier_hash;
        IF v_existing = p_ballot_hash THEN
            RETURN 'RETRY';
        END IF;
        RETURN 'CONFLICT';
    END IF;
    UPDATE ballot.credential_issuance SET used_checkpoint = p_checkpoint
     WHERE election_id = p_election_id AND nullifier_hash = p_nullifier_hash;
    INSERT INTO ballot.ballot_receipts (election_id, nullifier_hash, ballot_hash, sealed_ballot, receipt_hash,
                                        received_checkpoint, received_bucket, event_id)
    VALUES (p_election_id, p_nullifier_hash, p_ballot_hash, p_sealed_ballot, p_receipt_hash,
            p_checkpoint, p_bucket, p_event_id);
    RETURN 'CREATED';
END;
$$;

RESET ROLE;

-- =====================================================================================================
-- privileges for the Core connection
-- =====================================================================================================
GRANT USAGE ON SCHEMA credential TO pv2_core;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA credential TO pv2_core;

GRANT USAGE ON SCHEMA ballot TO pv2_core;
GRANT SELECT ON ballot.credential_issuance, ballot.ballot_receipts TO pv2_core;   -- views and tally read them
REVOKE ALL ON FUNCTION ballot.register_nullifier(text, text, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION ballot.cast_ballot(text, text, text, bytea, text, bigint, timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ballot.register_nullifier(text, text, bigint) TO pv2_core;
GRANT EXECUTE ON FUNCTION ballot.cast_ballot(text, text, text, bytea, text, bigint, timestamptz, text) TO pv2_core;
-- pv2_core gets no INSERT / UPDATE / DELETE on ballot tables: ballot rows are written only by the two functions
-- above and are never changed afterwards. A superuser can still change or link anything (disclosed, threat-model S2).
