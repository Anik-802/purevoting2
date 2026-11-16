from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_without_database():
    # no DATABASE_URL in unit tests: the endpoint must answer 503, not crash
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "db_unavailable"}


def test_readyz_node_mode_without_chain_config(monkeypatch):
    # started in the same command as the registry-setup bootstrap: empty chain values must not look healthy
    from app import db
    from app.config import settings
    monkeypatch.setattr(db, "ping", lambda: None)
    monkeypatch.setattr(db, "schema_current", lambda: True)
    monkeypatch.setattr(settings, "creation_check_mode", "node")
    monkeypatch.setattr(settings, "contract_address", "")
    monkeypatch.setattr(settings, "pinned_node_url", "http://node-observer:8545")
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "chain_config_missing"}
    monkeypatch.setattr(settings, "contract_address", "0x" + "11" * 20)
    assert client.get("/readyz").json() == {"status": "ready"}
    monkeypatch.setattr(settings, "pinned_node_url", "")
    assert client.get("/readyz").status_code == 503
    monkeypatch.setattr(settings, "creation_check_mode", "stub")
    assert client.get("/readyz").json() == {"status": "ready"}


def test_readyz_database_without_the_d30_migration(monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(db, "ping", lambda: None)
    monkeypatch.setattr(db, "schema_current", lambda: False)
    monkeypatch.setattr(settings, "creation_check_mode", "stub")
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json() == {"status": "db_migration_missing"}
