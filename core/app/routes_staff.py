"""Staff endpoints (JWT): elections, roster, links, staff views, alerts and findings."""
from fastapi import APIRouter, Depends, Header
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import text

from app import elections, findings, schemas, tally, timeutil
from app.auth import Staff, require_staff
from app.errors import Problem
from app.models import ElectionCreate, IssueIn, RosterIn
from app.routes_internal import alerts_of, view_page
from app.store import claim_idempotency_key, load_election, store_idempotent_response
from app.tx import run, run_election

router = APIRouter(tags=["elections"])
ANY_STAFF = require_staff("admin", "approver", "auditor")
ADMIN = require_staff("admin")


@router.post("/v1/elections", status_code=201)
def create_election(body: ElectionCreate, staff: Staff = Depends(ADMIN),
                    idempotency_key: str | None = Header(default=None, min_length=8, max_length=64)):
    def go(conn):
        now = timeutil.clock.now()
        scope = f"create:{staff.user_id}"
        if idempotency_key:
            prior = claim_idempotency_key(conn, scope, idempotency_key, elections.body_hash(body.model_dump()),
                                          now, ttl=24 * 3600)
            if prior is not None and prior.response is not None:
                return prior.response          # same key + same body within 24 h: the original response
        eid = elections.create(conn, staff, body.model_dump(), now)
        out = elections.to_staff(conn, load_election(conn, eid))
        if idempotency_key:
            store_idempotent_response(conn, scope, idempotency_key, out)
        return out
    return run(go)


@router.get("/v1/elections")
def list_elections(staff: Staff = Depends(ANY_STAFF)):
    def go(conn):
        ids = [r[0] for r in conn.execute(text("SELECT election_id FROM credential.elections ORDER BY election_id"))]
        return {"items": [elections.to_staff(conn, load_election(conn, i)) for i in ids]}
    return run(go)


@router.get("/v1/elections/{election_id}")
def get_election(election_id: str, staff: Staff = Depends(ANY_STAFF)):
    return run(lambda c: elections.to_staff(c, load_election(c, election_id)))


@router.post("/v1/elections/{election_id}:delete", status_code=204)
def delete_election(election_id: str, staff: Staff = Depends(ADMIN)):
    def go(conn):
        e = load_election(conn, election_id, lock=True)
        elections.require_operator_admin(staff, e)
        elections.delete(conn, e)
    run(go)
    return Response(status_code=204)


@router.put("/v1/elections/{election_id}/roster")
def replace_roster(election_id: str, body: RosterIn, staff: Staff = Depends(ADMIN)):
    def go(conn, e, now):
        elections.require_operator_admin(staff, e)
        return elections.replace_roster(conn, e, [v.model_dump() for v in body.voters])
    return run_election(election_id, go)


@router.post("/v1/elections/{election_id}/roster:read")
def read_roster(election_id: str, staff: Staff = Depends(ANY_STAFF)):
    return run(lambda c: elections.read_roster(c, load_election(c, election_id), staff.role != "auditor"))


@router.post("/v1/elections/{election_id}/roster:freeze", status_code=202)
def freeze(election_id: str, staff: Staff = Depends(ADMIN)):
    def go(conn, e, now):
        elections.require_operator_admin(staff, e)
        elections.freeze(conn, e, now)
        return elections.to_staff(conn, e)
    return run_election(election_id, go)


@router.post("/v1/elections/{election_id}/tokens:issue")
def issue_tokens(election_id: str, body: IssueIn, staff: Staff = Depends(ADMIN),
                 idempotency_key: str | None = Header(default=None, min_length=8, max_length=64)):
    if body.delivery == "EMAIL":
        raise Problem(422, "VALIDATION_ERROR", "EMAIL delivery is not available in the MVP; use CSV (D-16)")
    bh = elections.body_hash(body.model_dump())

    def go(conn, e, now):
        elections.require_operator_admin(staff, e)
        if idempotency_key:
            # checked under the election lock, so a concurrent retry gets the first request's CSV, never an empty one
            if claim_idempotency_key(conn, f"issue:{election_id}", idempotency_key, bh, now, ttl=None) is not None:
                cached = elections.issue_cache.get(election_id, idempotency_key)
                if cached is None:
                    raise Problem(409, "CONFLICT", "Idempotency-Key expired; use TOKEN_REISSUE")
                return cached
        rows, _ = elections.issue_tokens(conn, e, now)
        csv_text = elections.to_csv(rows)
        if idempotency_key:
            elections.issue_cache.put(election_id, idempotency_key, csv_text)
        return csv_text
    return PlainTextResponse(run_election(election_id, go), media_type="text/csv")


@router.post("/v1/elections/{election_id}/tally")
def run_tally(election_id: str, staff: Staff = Depends(ADMIN)):
    def go(conn, e, now):
        elections.require_operator_admin(staff, e)
        return tally.run_tally(conn, e, staff, now)
    return run_election(election_id, go)


@router.get("/v1/elections/{election_id}/tally")
def get_tally(election_id: str, staff: Staff = Depends(ANY_STAFF)):
    return run(lambda c: tally.get(c, load_election(c, election_id)))


@router.post("/v1/elections/{election_id}:finalize", status_code=202)
def finalize(election_id: str, staff: Staff = Depends(ADMIN)):
    def go(conn, e, now):
        elections.require_operator_admin(staff, e)
        tally.finalize(conn, e, staff, now)
        return elections.to_staff(conn, e)
    return run_election(election_id, go)


@router.get("/v1/elections/{election_id}/views/{view}", tags=["audit"])
def staff_view(election_id: str, view: str, checkpoint: int | None = None, limit: int = 100,
               cursor: str | None = None, staff: Staff = Depends(ANY_STAFF)):
    return view_page(view, election_id, checkpoint, limit, cursor, reveal_key=True)


@router.get("/v1/elections/{election_id}/audit-alerts", tags=["audit"])
def staff_alerts(election_id: str, staff: Staff = Depends(ANY_STAFF)):
    return alerts_of(election_id)


@router.get("/v1/elections/{election_id}/findings", tags=["audit"])
def staff_findings(election_id: str, rule_id: str | None = None, result: str | None = None,
                   staff: Staff = Depends(ANY_STAFF)):
    """Findings, newest first."""
    for value, enum in ((rule_id, "RuleId"), (result, "RuleResult")):
        if value is not None and value not in schemas.enum("common.schema.json", enum):
            raise Problem(400, "VALIDATION_ERROR", enum)

    def go(conn):
        load_election(conn, election_id)
        sql, params = "SELECT * FROM credential.findings WHERE election_id = :e", {"e": election_id}
        if rule_id:
            sql += " AND body->>'rule_id' = :ru"
            params["ru"] = rule_id
        if result:
            sql += " AND body->>'result' = :re"
            params["re"] = result
        rows = conn.execute(text(sql + " ORDER BY stored_at DESC, finding_id DESC"), params)
        return {"items": [findings.to_stored(r) for r in rows]}
    return run(go)
