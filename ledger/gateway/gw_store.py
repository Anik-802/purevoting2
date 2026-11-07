"""SQLite storage of the Ledger Gateway.

Privacy (README: voter-side storage): tables about voter events are WITHOUT ROWID with random primary keys, so the
physical row order carries no arrival order, and no arrival time is stored for them. The database file system
timestamps and the write-ahead log still reveal coarse timing to whoever operates the host (threat-model S2).
"""
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (            -- system events (Core source), in submission order
  event_id TEXT PRIMARY KEY, election_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL,
  checkpoint INTEGER NOT NULL, occurred_at TEXT NOT NULL, submission TEXT NOT NULL, payload_hash TEXT NOT NULL,
  leaf_hash TEXT NOT NULL, received_at TEXT NOT NULL,
  status TEXT NOT NULL,                          -- QUEUED, SENT, DEFERRED, ACCEPTED, REJECTED
  tx_hash TEXT, block_number INTEGER, nonce INTEGER, raw_tx BLOB, precheck TEXT, detail TEXT,
  rejection_id TEXT, anchor_id TEXT
);
CREATE INDEX IF NOT EXISTS ev_el ON events(election_id, seq);
CREATE TABLE IF NOT EXISTS voter_events (      -- TOKEN_USED, BALLOT_RECEIVED
  event_id TEXT PRIMARY KEY, election_id TEXT NOT NULL, type TEXT NOT NULL, checkpoint INTEGER NOT NULL,
  occurred_at TEXT NOT NULL, submission TEXT NOT NULL, payload_hash TEXT NOT NULL, leaf_hash TEXT NOT NULL,
  status TEXT NOT NULL,                          -- PENDING, ACCEPTED, REJECTED
  rejection_id TEXT, anchor_id TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS voter_keys (
  election_id TEXT NOT NULL, kind TEXT NOT NULL, hash TEXT NOT NULL, PRIMARY KEY (election_id, kind, hash)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS election_seq (election_id TEXT PRIMARY KEY, last_assigned INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS rejections (
  rejection_id TEXT PRIMARY KEY, election_id TEXT NOT NULL, event_id TEXT NOT NULL, type TEXT NOT NULL,
  checkpoint INTEGER NOT NULL, reject_code TEXT NOT NULL, detail TEXT, recorded_at TEXT NOT NULL,
  payload_hash TEXT NOT NULL, subject_hash TEXT, leaf TEXT NOT NULL, anchor_id TEXT
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS anchors (
  anchor_id TEXT PRIMARY KEY, election_id TEXT NOT NULL, mode TEXT NOT NULL, merkle_root TEXT NOT NULL,
  event_count INTEGER NOT NULL, rejections_root TEXT NOT NULL, bucket_min INTEGER NOT NULL, bucket_max INTEGER NOT NULL,
  leaves TEXT NOT NULL,                          -- JSON [[event_id, leaf_hash], ...] in tree order
  rejection_ids TEXT NOT NULL,                   -- JSON list
  status TEXT NOT NULL,                          -- SENT, ANCHORED, FAILED
  tx_hash TEXT, block_number INTEGER, anchored_at TEXT, raw_tx BLOB, detail TEXT, seq INTEGER
);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)

    def one(self, sql, args=()):
        with self.lock:
            r = self.db.execute(sql, args).fetchone()
            return dict(r) if r else None

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def run(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    def insert(self, table, row):
        cols = ",".join(row)
        marks = ",".join("?" * len(row))
        with self.lock:
            self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    def update(self, table, key_col, key, fields):
        sets = ",".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", (*fields.values(), key))
