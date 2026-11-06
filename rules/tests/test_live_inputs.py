"""Exercise real client request semantics, pagination and negative evidence boundaries."""
from copy import deepcopy

import httpx
import pytest

from purevoting_rules.engine import evaluate_all
from purevoting_rules.fixtures import development_cases
from purevoting_rules.hashing import snapshot_hash
from purevoting_rules.live_inputs import InputError, LiveInputs


class API:
    def __init__(self):
        self.s = development_cases(1)[0]["snapshot"]
        self.eid = self.s["election_id"]
        self.calls = []
        self.mutate = lambda path, q, data: data

    def handle(self, request):
        assert request.headers["X-Service-Key"] == "example"
        p, q = request.url.path, request.url.params
        self.calls.append((p, dict(q)))
        if p == "/internal/v1/elections":
            data = {"items": [{"election_id": self.eid, "status": "OPEN", "open_checkpoint": 3, "last_committed_checkpoint": 2}]}
        elif p == "/internal/v1/checkpoints":
            assert q["election_id"] == self.eid
            data = {"open_checkpoint": 3, "items": [
                {"checkpoint": e["checkpoint"], "reason": e["payload"]["reason"],
                 "event_id": e["event_id"], "ledger_status": "ACCEPTED"}
                for e in self.s["events"] if e["type"] == "CHECKPOINT_COMMITTED"]}
        else:
            assert q["election_id"] == self.eid
            start, limit = int(q.get("cursor", 0)), int(q["limit"])
            if p.startswith("/internal/v1/views/"):
                name, cp = p.rsplit("/", 1)[1], q["checkpoint"]
                rows = sorted(self.s["history"][cp]["views"][name], key=lambda r: r["row_key"])
                data = {"view": name, "election_id": self.eid, "checkpoint": int(cp),
                        "total_rows": len(rows), "snapshot_hash": None if name == "election_config" else snapshot_hash(rows),
                        "rows": rows[start:start+limit]}
            elif p in ("/v1/events", "/v1/rejections"):
                rows = [e for e in self.s[p[4:]] if e["checkpoint"] <= int(q["checkpoint_max"])]
                rows.sort(key=(lambda r: (r["occurred_at"], r["event_id"])) if p == "/v1/events"
                          else (lambda r: (r["checkpoint"], r["rejection_id"])))
                data = {"items": rows[start:start+limit]}
                if p == "/v1/events":
                    data["last_core_seq"] = self.s["ledger_last_seq"]
            else:
                raise AssertionError("unexpected path")
            data["next_cursor"] = str(start+limit) if start+limit < len(rows) else None
        changed = self.mutate(p, q, deepcopy(data))
        return changed if isinstance(changed, httpx.Response) else httpx.Response(200, json=changed)


@pytest.fixture
def setup():
    api = API()
    with httpx.Client(base_url="http://core.test", headers={"X-Service-Key":"example"},
                      transport=httpx.MockTransport(api.handle)) as core, \
         httpx.Client(base_url="http://gateway.test", headers={"X-Service-Key":"example"},
                      transport=httpx.MockTransport(api.handle)) as gateway:
        yield api, LiveInputs(core, gateway, page_size=9)


def test_real_api_shapes_and_cumulative_history(setup):
    api, client = setup
    [election] = client.elections()
    cps = client.checkpoints(election["election_id"])
    got = client.snapshot(api.eid, 2, cps)
    assert set(got["history"]) == {"0", "1", "2"}
    assert len(got["events"]) == len(api.s["events"])
    assert {"R1":"PASS","R2":"PASS","R3":"PASS","R4":"INCONCLUSIVE","R5":"INCONCLUSIVE"} == {
        r.rule_id: r.result for r in evaluate_all(got)}
    assert any(p == "/v1/events" and "cursor" in q for p, q in api.calls)
    assert any(p.endswith("/views/roster") and "cursor" in q for p, q in api.calls)
    assert {q["checkpoint"] for p, q in api.calls if p.endswith("/views/roster")} == {"0","1","2"}
    assert [q for p, q in api.calls if p == "/v1/rejections"][0]["checkpoint_max"] == "2"


@pytest.mark.parametrize("defect", ["http", "schema", "future", "wrong_election", "duplicate", "cursor",
                                    "watermark", "missing_commit", "max_pages", "missing_cursor"])
def test_untrustworthy_ledger_never_returns_clean_snapshot(setup, defect):
    api, client = setup
    cps = client.checkpoints(api.eid)
    if defect == "max_pages":
        client.max_pages = 1
    def change(p, q, page):
        if p == "/v1/rejections" and defect == "http":
            return httpx.Response(404, text="sensitive-response-body")
        if p == "/v1/events":
            if defect == "schema":
                del page["items"][0]["anchor"]
            elif defect == "future":
                page["items"][0]["checkpoint"] = 5
            elif defect == "wrong_election":
                page["items"][0]["election_id"] = "elc_"+"Z"*26
            elif defect == "duplicate":
                page["items"][1] = page["items"][0]
            elif defect == "cursor":
                page["next_cursor"] = "9"
            elif defect == "watermark" and "cursor" in q:
                page["last_core_seq"] += 1
            elif defect == "missing_commit":
                page["items"] = [e for e in page["items"] if not (e["type"] == "CHECKPOINT_COMMITTED" and e["checkpoint"] == 1)]
            elif defect == "missing_cursor":
                del page["next_cursor"]
        return page
    api.mutate = change
    with pytest.raises(InputError) as err:
        client.snapshot(api.eid, 2, cps)
    assert err.value.source == "gateway"
    assert "sensitive" not in str(err.value)


@pytest.mark.parametrize("defect", ["total", "hash", "scope", "order", "duplicate", "invalid_row",
                                    "invalid_json", "unavailable"])
def test_untrustworthy_core_page_not_an_empty_view(setup, defect):
    api, client = setup
    cps = client.checkpoints(api.eid)
    def change(p, q, page):
        if p.endswith("/views/token_status") and q["checkpoint"] == "2":
            if defect == "total": page["total_rows"] += 1
            if defect == "hash": page["snapshot_hash"] = "f"*64
            if defect == "scope": page["checkpoint"] = 1
            if defect == "order": page["rows"].reverse()
            if defect == "duplicate": page["rows"][1] = page["rows"][0]
            if defect == "invalid_row": del page["rows"][0]["status"]
            if defect == "invalid_json": return httpx.Response(200, content=b"bad-data")
            if defect == "unavailable": return httpx.Response(503)
        return page
    api.mutate = change
    got = client.snapshot(api.eid, 2, cps)
    assert got["views"]["token_status"] is None
    r1 = evaluate_all(got)[0]
    assert (r1.result, r1.inconclusive_reason) == ("INCONCLUSIVE", "VIEW_UNAVAILABLE")


def test_core_rejects_redirect_without_sending_key_to_other_host(setup):
    api, client = setup
    api.mutate = lambda *a: httpx.Response(307, headers={"Location":"http://not-a-service.test/private"})
    with pytest.raises(InputError):
        client.elections()
    assert len(api.calls) == 1


def test_pending_checkpoint_not_read(setup):
    api, client = setup
    cps = client.checkpoints(api.eid)
    cps[-1]["ledger_status"] = "PENDING"
    with pytest.raises(InputError) as err:
        client.snapshot(api.eid, 2, cps)
    assert err.value.kind == "pending"
    assert all(not p.startswith("/v1/") for p, _ in api.calls)


@pytest.mark.parametrize("conflict", ["core_seq", "accepted_and_rejected", "two_terminal_rejections"])
def test_ambiguous_terminal_ledger_outcomes_abort_before_evaluation(setup, conflict):
    api, inputs = setup
    events = api.s["events"]
    if conflict == "core_seq":
        system = [e for e in events if e["source"] == "core" and "seq" in e]
        system[-1]["seq"] = system[0]["seq"]  # distinct event IDs, collision across pages
    else:
        ballot = next(e for e in events if e["type"] == "BALLOT_RECEIVED")
        rej = {"rejection_id": "rej_" + "A" * 26, "event_id": ballot["event_id"],
               "election_id": api.eid, "type": "BALLOT_RECEIVED", "checkpoint": ballot["checkpoint"],
               "recorded_at": ballot["occurred_at"], "reject_code": "ELECTION_NOT_OPEN",
               "payload_hash": ballot["payload_hash"], "subject_hash": ballot["payload"]["nullifier_hash"]}
        api.s["rejections"].append(rej)
        if conflict == "two_terminal_rejections":
            events.remove(ballot)
            api.s["rejections"].append({**rej, "rejection_id": "rej_" + "B" * 26})
    with pytest.raises(InputError) as err:
        inputs.snapshot(api.eid, 2, inputs.checkpoints(api.eid))
    assert (err.value.source, err.value.kind) == ("gateway", "invalid")


def test_rejected_system_seq_gap_allows_new_accepted_submission(setup):
    api, inputs = setup
    opened = next(e for e in api.s["events"] if e["type"] == "ELECTION_OPENED")
    # Terminal refusal; contract permits a new ELECTION_OPENED with a new ID
    # and seq. The corrected event need not have a different payload_hash.
    api.s["rejections"].append({"rejection_id": "rej_" + "C" * 26, "event_id": "evt_" + "C" * 26,
                                "election_id": api.eid, "type": opened["type"], "checkpoint": opened["checkpoint"],
                                "recorded_at": opened["occurred_at"], "reject_code": "INVALID_STATE_TRANSITION",
                                "payload_hash": opened["payload_hash"], "subject_hash": None})
    gap = opened["seq"]
    for event in api.s["events"]:
        if event.get("source") == "core" and event.get("seq", -1) >= gap:
            event["seq"] += 1
    api.s["ledger_last_seq"] += 1
    snap = inputs.snapshot(api.eid, 2, inputs.checkpoints(api.eid))
    assert len(snap["rejections"]) == 1 and gap not in {e.get("seq") for e in snap["events"]}
