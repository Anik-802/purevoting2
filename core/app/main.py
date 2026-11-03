"""Core API (contracts/core-api.openapi.yaml)."""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import creation_check, db, outbox, timeutil
from app.config import settings
from app.errors import Problem, problem_handler, validation_handler
from app.lifecycle import advance
from app.routes_internal import router as internal_router
from app.routes_public import router as public_router
from app.routes_staff import router as staff_router
from app.store import load_election

log = logging.getLogger("core")


def scheduler_tick(only: list[str] | None = None) -> None:
    """Boundary transitions (open, periodic checkpoints, close) for every election that can have one
    (or only the given ones: tests must never move elections they did not create)."""
    with db.get_engine().connect() as conn:
        ids = [r[0] for r in conn.execute(text(
            "SELECT e.election_id FROM credential.elections e WHERE (SELECT status FROM credential.election_history h "
            "WHERE h.election_id = e.election_id ORDER BY checkpoint DESC LIMIT 1) IN ('ROSTER_FROZEN', 'OPEN') "
            "OR (e.creation_check = 'PENDING' AND e.created_block IS NOT NULL)"))]
    for eid in ids:
        if only is not None and eid not in only:
            continue
        try:
            creation_check.check(eid)       # retried until the pinned node answers; reads outside the lock
            with db.get_engine().begin() as conn:
                e = load_election(conn, eid, lock=True)
                advance(conn, e, timeutil.clock.now())
        except Exception:   # noqa: BLE001
            log.exception("scheduler failed for an election")


def _loop(stop: threading.Event, fn, period: float) -> None:
    while not stop.wait(period):
        try:
            fn()
        except Exception:   # noqa: BLE001 — keep the loop alive (DB restart etc.)
            log.exception("background loop error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = threading.Event()
    creation_check.warn_if_stub()
    if settings.run_workers and settings.database_url:
        for fn in (scheduler_tick, outbox.deliver_all):
            threading.Thread(target=_loop, args=(stop, fn, 1.0), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="PureVoting 2.0 — Core API", version="1.0.0", lifespan=lifespan)
app.add_exception_handler(Problem, problem_handler)
app.add_exception_handler(RequestValidationError, validation_handler)
app.include_router(public_router)
app.include_router(staff_router)
app.include_router(internal_router)


@app.get("/healthz")
def healthz():
    """Process is up (used by the docker-compose healthcheck)."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Process is up and the database answers."""
    try:
        db.ping()
    except Exception:  # noqa: BLE001 — any failure means not ready; details stay out of the response
        return JSONResponse(status_code=503, content={"status": "db_unavailable"})
    return {"status": "ready"}
