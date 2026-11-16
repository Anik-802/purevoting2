"""Database engine. Every audit-relevant change and its outbox record go through one transaction:

    with engine.begin() as conn:
        ...   # change + outbox insert, committed together (README "Consistency model")
"""
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        # hide_parameters: DB errors must not copy token / nullifier hashes or evr_ IDs into logs (README "PII")
        _engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    return _engine


def ping() -> bool:
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT 1")).scalar() == 1


def schema_current() -> bool:
    """The D-30 tables exist (an older database needs core/db/migrations/001_elgamal.sql)."""
    with get_engine().connect() as conn:
        return conn.execute(text("SELECT to_regclass('credential.decryption_shares') IS NOT NULL")).scalar()
