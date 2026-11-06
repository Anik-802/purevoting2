"""Unit tests that need no database."""
import json
import re
from pathlib import Path

import pytest

from app import elections, ids
from app.timeutil import ceil_bucket, floor_bucket, iso, parse_iso, system_bucket_start

VECTORS = Path(__file__).resolve().parents[2] / "tests" / "vectors" / "hash-vectors.json"


def test_id_formats():
    assert re.fullmatch(r"elc_[0-9A-HJKMNP-TV-Z]{26}", ids.ulid("elc"))
    assert re.fullmatch(r"evr_[0-9A-HJKMNP-TV-Z]{26}", ids.random_id("evr"))
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", ids.token128())


def test_ulid_sorts_by_time_random_ids_do_not_carry_it():
    a, b = ids.ulid("evt"), ids.ulid("evt")
    assert a[:14] <= b[:14]          # first 10 chars after the prefix are the millisecond time
    assert len({ids.random_id("vtr") for _ in range(100)}) == 100


def test_bucket_rules():
    # voter event on a boundary belongs to the bucket that starts there; a system event to the one that ends there
    assert floor_bucket(120, 60) == 120
    assert system_bucket_start(120, 60) == 60
    assert system_bucket_start(121, 60) == 120
    assert ceil_bucket(121, 60) == 180
    assert iso(parse_iso("2026-11-03T09:00:00Z")) == "2026-11-03T09:00:00Z"


def test_config_hash_matches_shared_vector():
    v = next(x for x in json.loads(VECTORS.read_text(encoding="utf-8"))["vectors"] if x["name"] == "config_hash")
    inp = dict(v["input"])
    inp["window_start"], inp["window_end"] = parse_iso(inp["window_start"]), parse_iso(inp["window_end"])
    assert elections.config_hash(inp) == v["expected"]


def test_link_csv_has_the_contract_columns():
    text = elections.to_csv([{"external_ref": "s1", "name": "Kim", "link": "http://x/vote/elc_1#t=abc"}])
    assert text.splitlines()[0] == "external_ref,name,link"
    assert elections.to_csv([]) == ""


def test_link_csv_neutralises_formulas():
    text = elections.to_csv([{"external_ref": "=1+2", "name": "@SUM(A1)", "link": "http://x/vote/elc_1#t=abc"}])
    assert text.splitlines()[1] == "'=1+2,'@SUM(A1),http://x/vote/elc_1#t=abc"
    tab = elections.to_csv([{"external_ref": "\t=1", "name": "Kim", "link": "http://x"}])
    assert tab.splitlines()[1].startswith("'\t=1,")


def test_retry_later_schedule():
    """RETRY_LATER: every 1 s for the first 30 s, then exponential up to 60 s (contracts v1.1)."""
    from app import outbox
    outbox._backoff.clear()
    t, waits = 1000, []
    while t < 1000 + 300:
        outbox._delay("elc_x", t, retry_later=True)
        nxt = outbox._backoff["elc_x"][0]
        waits.append(nxt - t)
        t = nxt
    assert waits[:30] == [1] * 30 and waits[30:36] == [2, 4, 8, 16, 32, 60] and set(waits[36:]) == {60}
    # Gateway unavailable: exponential from the start; a later RETRY_LATER run starts its own 30 s of 1 s retries
    outbox._backoff.clear()
    for t in (0, 1, 3, 7):
        outbox._delay("elc_y", t)
    assert outbox._backoff["elc_y"][1] == 8
    outbox._delay("elc_y", 15, retry_later=True)
    assert outbox._backoff["elc_y"][:2] == (16, 1)
    outbox._backoff.clear()


def _idh(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def _reader(http):
    from app.chain import NodeReader
    from devtools import fake_gateway
    return NodeReader(urls=["http://testserver/rpc"], contract=fake_gateway.state.contract_address, http=http)


def test_pinned_node_urls_and_decoding():
    """PINNED_NODE_URL is a list; the reader decodes what an EVM node returns for the two getters (fake node encodes)."""
    from fastapi.testclient import TestClient

    from app.chain import NodeUnavailable
    from app.config import Settings
    from devtools import fake_gateway

    assert Settings(pinned_node_url=" http://a:8545 , http://b:8545,").pinned_node_urls == ["http://a:8545", "http://b:8545"]
    fake_gateway.state = fake_gateway.State()
    fake_gateway.state.events["evt_1"] = {
        "type": "ELECTION_CREATED", "election_id": "elc_1", "anchor": {},
        "payload": {"config_hash": "ab" * 32, "window_start": "2026-11-02T10:00:00Z", "window_end": "2026-11-02T12:00:00Z",
                    "time_bucket_seconds": 60, "checkpoint_interval_seconds": 120, "approval_threshold": 2,
                    "organizations": [{"org_id": "org_observer"}, {"org_id": "org_commission"}],
                    "operator_org": "org_commission", "proposal_ttl_hours": 24}}
    r = _reader(TestClient(fake_gateway.app))
    assert r.read_election("elc_1", 100, 3) == (102, None, None)           # 102 < 100 + 3: not confirmed there yet
    latest, s, g = r.read_election("elc_1", 100, 2)
    assert latest == 102 and s["config_hash"] == "ab" * 32 and s["window_end"] - s["window_start"] == 7200
    assert s["time_bucket_seconds"] == 60 and s["operator_org_id"] == g["operator_org_id"] == _idh("org_commission")
    assert g["org_ids"] == [_idh("org_commission"), _idh("org_observer")]
    assert (g["approval_threshold"], g["proposal_ttl_hours"], g["checkpoint_interval_seconds"]) == (2, 24, 120)
    _, s, g = r.read_election("elc_unknown", 100, 2)
    assert int(s["config_hash"], 16) == 0 and g["org_ids"] == []
    fake_gateway.state.node_down = True
    with pytest.raises(NodeUnavailable):
        r.read_election("elc_1", 100, 2)
    fake_gateway.state = fake_gateway.State()


def test_malformed_node_answers_are_unavailable():
    """Anything malformed (JSON-RPC error in a 200, non-hex, offset inside the head, out-of-range values) is
    NodeUnavailable, never an exception that would roll back the caller."""
    import httpx

    from app.chain import NodeUnavailable, _Bad, decode_governance, decode_state

    def answering(result_for):
        class Http:
            def post(self, url, json=None, **kw):
                return httpx.Response(200, json=result_for(json["method"]))
        return _reader(Http())

    block = {"jsonrpc": "2.0", "id": 1, "result": "0x100"}
    for bad in ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "x"}},
                {"jsonrpc": "2.0", "id": 1, "result": None}, {"jsonrpc": "2.0", "id": 1, "result": "0xzz"},
                {"jsonrpc": "2.0", "id": 1, "result": "0x" + "00" * 31}, ["not", "an", "object"]):
        with pytest.raises(NodeUnavailable):
            answering(lambda m, bad=bad: block if m == "eth_blockNumber" else bad).read_election("elc_1", 1, 2)
    with pytest.raises(NodeUnavailable):
        answering(lambda m: {"jsonrpc": "2.0", "id": 1, "result": "latest"}).read_election("elc_1", 1, 2)

    with pytest.raises(_Bad):                      # orgIds offset pointing into the head
        decode_governance([0x20, 2, 0, 24, 60, 0])
    with pytest.raises(_Bad):                      # approvalThreshold beyond uint8
        decode_governance([0xa0, 256, 0, 24, 60, 0])
    with pytest.raises(_Bad):                      # array length beyond the data
        decode_governance([0xa0, 2, 0, 24, 60, 3, 1])
    with pytest.raises(_Bad):                      # timeBucketSeconds beyond uint32
        decode_state([0, 1, 2, 2**32, 0, 0, 0, 0, 0, 0, 0])
    assert decode_governance([0xa0, 2, 0, 24, 60, 1, 7])["org_ids"] == [format(7, "064x")]


def test_gateway_events_reads_every_page():
    import httpx

    from app.gateway import GatewayClient, GatewayUnavailable

    class Pages:
        def __init__(self, pages):
            self.pages, self.cursors = pages, []

        def get(self, path, params=None, headers=None):
            self.cursors.append(params.get("cursor"))
            return httpx.Response(200, json=self.pages[len(self.cursors) - 1])

    http = Pages([{"items": [{"event_id": "a"}], "next_cursor": "c1"}, {"items": [{"event_id": "b"}], "next_cursor": None}])
    assert [e["event_id"] for e in GatewayClient(http=http).events("elc_1")] == ["a", "b"]
    assert http.cursors == [None, "c1"]
    for bad in ({"items": "ab", "next_cursor": None}, {"next_cursor": None}, ["x"]):
        with pytest.raises(GatewayUnavailable):
            GatewayClient(http=Pages([bad])).events("elc_1")
    with pytest.raises(GatewayUnavailable):                     # a listing that never ends
        GatewayClient(http=Pages([{"items": [], "next_cursor": "same"}] * 3)).events("elc_1")
    class Raw:                                                  # deeply nested JSON: RecursionError while parsing
        def get(self, path, params=None, headers=None):
            return httpx.Response(200, content=b'{"items": [' + b"[" * 100000 + b"]" * 100000 + b'], "next_cursor": null}')
    with pytest.raises(GatewayUnavailable):
        GatewayClient(http=Raw()).events("elc_1")
    endless = Pages([{"items": [], "next_cursor": str(i)} for i in range(2000)])
    with pytest.raises(GatewayUnavailable):
        GatewayClient(http=endless).events("elc_1")
    assert len(endless.cursors) == 1000


def test_tally_plaintext_is_compared_after_parsing():
    from app.tally import valid_choice
    ids = {"cand_A", "cand_B"}
    assert valid_choice(b'{"candidate_id":"cand_A"}', ids) == "cand_A"          # browser (JSON.stringify)
    assert valid_choice(b'{"candidate_id": "cand_B"}', ids) == "cand_B"         # Python json.dumps default
    assert valid_choice(b' {\n "candidate_id" : "cand_A" } ', ids) == "cand_A"
    assert valid_choice(b'{"candidate_id":"cand_X"}', ids) is None              # not a candidate
    assert valid_choice(b'{"candidate_id":"cand_A","x":1}', ids) is None        # extra key
    assert valid_choice(b'{"candidate_id":"cand_A","candidate_id":"cand_B"}', ids) is None   # duplicated key
    assert valid_choice(b'{"candidate_id":1}', ids) is None
    assert valid_choice(b'["cand_A"]', ids) is None
    assert valid_choice(b'\xff\xfe', ids) is None
    assert valid_choice(b'{"candidate_id":"cand_A"', ids) is None
