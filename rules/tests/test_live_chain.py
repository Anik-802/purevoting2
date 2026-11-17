"""D-26 / D-27: pin comparison, no credential leaks, and confirmation boundaries."""
from dataclasses import replace

import httpx
import pytest

from purevoting_rules.live_chain import ChainPins, PinnedChain, reported_anchor_block


PINS = ChainPins("7777", "0x" + "ab" * 20, ("http://observer/rpc", "http://commission/rpc"), 2)
CONFIG = {"platform": "other", "interface_version": "1.1.0", "chain_id": "0x1e61",
          "contract_address": "0x" + "AB" * 20, "confirmation_depth": 2,
          "rpc_urls": ["http://untrusted/rpc", "http://commission/rpc", "http://observer/rpc/"]}


def event(block, kind="CHECKPOINT_COMMITTED", event_id="evt_" + "0" * 26):
    return {"event_id": event_id, "type": kind, "election_id": "elc_" + "1" * 26, "checkpoint": 0,
            "anchor": {"status": "ANCHORED", "anchor_id": "anc_" + "1" * 26, "mode": "immediate",
                       "merkle_root": "0" * 64, "tx_hash": "0x" + "a" * 64, "block_number": block}}


def assess(responses, checkpoint, events, pins=PINS):
    trace = []
    def handler(req):
        import json
        body = json.loads(req.content)
        method, host = body["method"], req.url.host
        assert "authorization" not in req.headers and "cookie" not in req.headers and "x-rule-key" not in req.headers
        trace.append((host, method))
        reply = responses.get((host, method))
        if isinstance(reply, httpx.Response):
            return reply
        if reply is None:
            raise httpx.ConnectError("offline", request=req)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": reply})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                      auth=("service", "secret"), headers={"X-Rule-Key": "test"}, cookies={"session": "test"}) as client:
        return PinnedChain(pins, client).assess(checkpoint, events), trace


def test_config_uses_local_pin_order_accepts_additional_advertised_validators():
    p = ChainPins.from_env({"CHAIN_ID": "7777", "CONTRACT_ADDRESS": PINS.contract_address,
                            "PINNED_NODE_URL": "http://observer/rpc/, http://commission/rpc", "CONFIRMATION_DEPTH": "2"})
    assert p == PINS
    assert p.differences(CONFIG) == ()
    assert p.differences(CONFIG | {"confirmation_depth": True, "rpc_urls": ["http://other/"],
                                  "contract_address": "0x" + "cb" * 20, "chain_id": "18"}) == (
        "chain_id", "contract_address", "confirmation_depth", "rpc_urls")
    assert p.differences({k: v for k, v in CONFIG.items() if k != "confirmation_depth"}) == ("confirmation_depth",)
    assert set(p.differences(None)) == set(CONFIG)


@pytest.mark.parametrize("env", [
    {"PINNED_NODE_URL": ""}, {"PINNED_NODE_URL": "file:///tmp/a"},
    {"PINNED_NODE_URL": "http://name:secret@observer/rpc"}, {"CHAIN_ID": "-1"},
    {"CONTRACT_ADDRESS": "0x" + "0" * 40}, {"CONFIRMATION_DEPTH": "-1"},
])
def test_invalid_local_config_stops_startup(env):
    with pytest.raises(ValueError):
        ChainPins.from_env({"CHAIN_ID": "7777", "CONTRACT_ADDRESS": PINS.contract_address,
                            "PINNED_NODE_URL": "http://observer/rpc"} | env)


def test_tries_lagging_observer_then_commission_before_accepting_a_reported_height():
    cp, other = event(5), event(8, "TOKEN_USED", "evr_" + "3" * 26)
    r, trace = assess({("observer", "eth_chainId"): "0x1e61", ("observer", "eth_blockNumber"): "0x9",
                       ("commission", "eth_chainId"): "0x1e61", ("commission", "eth_blockNumber"): "0xa"},
                      cp, [other, cp])
    assert r.checkpoint_deep and r.anchors_deep and r.latest == 10 and r.reason is None
    assert trace == [(host, method) for host in ("observer", "commission") for method in ("eth_chainId", "eth_blockNumber")]


@pytest.mark.parametrize(("block", "depth", "cp_ready", "anchors", "reason"), [
    ("0x0", 0, True, False, "BATCH_NOT_ANCHORED"),
    ("0x1", 2, False, False, "NODE_UNAVAILABLE"),
    ("0x2", 2, True, False, "BATCH_NOT_ANCHORED"),
    ("0x3", 2, True, True, None),
])
def test_depth_boundary_including_genesis(block, depth, cp_ready, anchors, reason):
    cp = event(0)
    r, _ = assess({("observer", "eth_chainId"): "0x1e61", ("observer", "eth_blockNumber"): block},
                  cp, [cp, event(1, "TOKEN_USED", "evr_" + "3" * 26)], replace(PINS, depth=depth))
    assert (r.checkpoint_deep, r.anchors_deep, r.reason) == (cp_ready, anchors, reason)


def test_wrong_chain_never_supplies_a_height_even_if_a_matching_chain_is_behind():
    cp = event(2)
    r, trace = assess({("observer", "eth_chainId"): "0x123", ("observer", "eth_blockNumber"): "0xffff",
                       ("commission", "eth_chainId"): "0x1e61", ("commission", "eth_blockNumber"): "0x2"}, cp, [cp])
    assert r.config_mismatch and r.latest == 2 and not r.checkpoint_deep
    assert ("observer", "eth_blockNumber") not in trace


@pytest.mark.parametrize("fault", [
    "0x00", "bogus", 4, True,
    httpx.Response(302, headers={"Location": "http://untrusted/rpc"}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": True, "result": "0x1e61"}),
    httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}}),
    httpx.Response(200, text="not JSON"),
])
def test_cannot_establish_identity_or_type_correct_height(fault):
    cp = event(1)
    r, trace = assess({("observer", "eth_chainId"): fault,
                       ("commission", "eth_chainId"): "0x1e61", ("commission", "eth_blockNumber"): "bad"}, cp, [cp])
    assert not r.node_available and not r.checkpoint_deep and not r.anchors_deep and r.reason == "NODE_UNAVAILABLE"
    assert all(host in {"observer", "commission"} for host, _ in trace)


@pytest.mark.parametrize("change", [
    {"status": "PENDING"}, {"block_number": True}, {"block_number": -1},
    {"tx_hash": "not-hex"}, {"anchor_id": ""}, {"merkle_root": None}, {"mode": "invalid"},
])
def test_incomplete_anchor_does_not_satisfy_gate(change):
    cp = event(0)
    other = event(0, "TOKEN_USED", "evr_" + "4" * 26)
    other["anchor"].update(change)
    assert reported_anchor_block(other) is None
    r, _ = assess({("observer", "eth_chainId"): "0x1e61", ("observer", "eth_blockNumber"): "0xff"}, cp, [cp, other])
    assert r.checkpoint_deep and not r.anchors_deep and r.reason == "BATCH_NOT_ANCHORED"


def test_no_checkpoint_anchor_evidence_or_cumulative_omission_is_not_ready():
    cp = event(5)
    ok = {("observer", "eth_chainId"): "0x1e61", ("observer", "eth_blockNumber"): "0xff"}
    r, _ = assess(ok, cp, [])
    assert not r.checkpoint_deep and not r.anchors_deep
    cp.pop("anchor")
    r, _ = assess(ok, cp, [cp])
    assert not r.checkpoint_deep and not r.anchors_deep


def test_no_requests_after_first_configured_correct_deep_node():
    cp = event(3)
    r, trace = assess({("observer", "eth_chainId"): "0x1e61", ("observer", "eth_blockNumber"): "0x5"}, cp, [cp])
    assert r.anchors_deep and trace == [("observer", "eth_chainId"), ("observer", "eth_blockNumber")]


def collect_evidence(snapshot, modify=None, heads=None):
    """Serve concrete ABI values while recording exact RPC block/address/auth."""
    import json
    from eth_abi import decode, encode
    from eth_utils import keccak
    from purevoting_rules.hashing import H
    from purevoting_rules.live_chain import _ANCHOR_TOPIC, _READ_ABI
    chain = snapshot["chain"]
    trace = []
    names = {keccak(text=name + "(" + ",".join(inputs) + ")")[:4].hex(): name
             for name, (inputs, outputs) in _READ_ABI.items()}

    def handler(req):
        assert not {"authorization", "cookie", "x-service-key", "x-rule-key"} & set(req.headers)
        body = json.loads(req.content)
        trace.append((req.url.host, body))
        method, params = body["method"], body["params"]
        height = (heads or {}).get(req.url.host, 7)
        confirmed = height - PINS.depth
        visible_anchors = [(aid, item) for aid, item in chain["anchors"].items()
                           if chain["anchor_logs"][aid]["block_number"] <= confirmed]
        if modify:
            override = modify(req.url.host, method, params)
            if override is not None:
                return override
        if method == "eth_chainId":
            result = "0x1e61"
        elif method == "eth_blockNumber":
            result = hex(height)
        elif method == "eth_getLogs":
            assert params[0]["toBlock"] == hex(confirmed) and params[0]["address"] == PINS.contract_address
            result = [{"address": PINS.contract_address, "topics": [_ANCHOR_TOPIC, "0x" + H(snapshot["election_id"]), "0x" + aid],
                       "blockNumber": hex(log["block_number"]), "transactionHash": log["tx_hash"], "removed": False,
                       "data": "0x" + encode(("bytes32", "bytes32", "uint8", "uint32"),
                                                (bytes.fromhex(log["merkle_root"]), bytes.fromhex(log["rejections_root"]),
                                                 0 if log["mode"] == "immediate" else 1, log["event_count"])).hex()}
                      for aid, log in chain["anchor_logs"].items() if log["block_number"] <= confirmed]
        elif method == "eth_call":
            call, block = params
            assert call["to"] == PINS.contract_address and block == hex(confirmed)
            name = names[call["data"][2:10]]
            inputs, outputs = _READ_ABI[name]
            args = decode(inputs, bytes.fromhex(call["data"][10:]))
            state, saved, gov = chain["state"], chain["checkpoint_state"], chain["governance"]
            if name == "orgSigner":
                org = next(org for org in chain["registry"] if H(org) == args[0].hex())
                item = chain["registry"][org]
                values = (item["signer"], item["registered_by"])
            elif name == "operatorOrg":
                values = (bytes.fromhex(chain["operator"]["org_id"]), True)
            elif name == "electionGovernance":
                values = ([bytes.fromhex(org) for org in gov["org_ids"]], gov["approval_threshold"], bytes.fromhex(gov["operator_org_id"]),
                          gov["proposal_ttl_hours"], gov["checkpoint_interval_seconds"])
            elif name == "electionState":
                values = (1, state["window_start"], state["window_end"], state["time_bucket_seconds"], bytes.fromhex(state["config_hash"]),
                          bytes.fromhex(state["roster_hash"]), bytes.fromhex(state["token_set_root"]), bytes.fromhex(state["result_hash"]),
                          state["last_checkpoint"], state["last_core_seq"], bytes.fromhex(state["operator_org_id"]))
            elif name == "electionStateAtCheckpoint":
                assert args[1] == 0
                values = (1, saved["window_end"], bytes.fromhex(saved["roster_hash"]), bytes.fromhex(saved["token_set_root"]),
                          bytes.fromhex(saved["result_hash"]), saved["last_core_seq"])
            elif name == "anchorCount":
                values = (len(visible_anchors),)
            elif name == "anchorAt":
                aid, item = visible_anchors[args[1]]
                values = (bytes.fromhex(aid), bytes.fromhex(item["merkle_root"]), bytes.fromhex(item["rejections_root"]),
                          0 if item["mode"] == "immediate" else 1, item["event_count"], item["bucket_min"], item["bucket_max"])
            else:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000}})
            result = "0x" + encode(outputs, values).hex()
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, auth=("rule", "secret"),
                      headers={"X-Service-Key": "secret", "X-Rule-Key": "secret"}, cookies={"session": "secret"}) as client:
        evidence = PinnedChain(PINS, client).collect(snapshot)
    return evidence, trace


def test_contract_reads_are_independent_key_free_and_use_one_fixed_confirmed_block():
    from test_chain_audit import clean_snapshot
    s = clean_snapshot()
    evidence, trace = collect_evidence(s)
    assert evidence["available"] and evidence["confirmed_block"] == 5
    for key in ("registry", "operator", "governance", "state", "checkpoint_state", "anchors", "anchor_logs"):
        assert evidence[key] == s["chain"][key]
    assert all(host == "observer" for host, _ in trace)


def test_rpc_redirect_and_wrong_chain_fail_over_only_to_local_pin():
    from test_chain_audit import clean_snapshot
    def redirect(host, method, params):
        if host == "observer":
            return httpx.Response(302, headers={"Location": "http://attacker/"})
    evidence, trace = collect_evidence(clean_snapshot(), redirect)
    assert evidence["available"]
    assert {host for host, _ in trace} == {"observer", "commission"}


def test_noncanonical_abi_and_missing_election_never_turn_into_clean_zero_evidence():
    from test_chain_audit import clean_snapshot
    def broken(host, method, params):
        if method == "eth_call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": "0x"})
    evidence, _ = collect_evidence(clean_snapshot(), broken)
    assert not evidence["available"] and evidence["reason"] == "NODE_UNAVAILABLE"


def test_failed_anchor_rpc_preserves_registry_but_exposes_missing_anchor_coverage():
    from test_chain_audit import clean_snapshot
    def unavailable(host, method, params):
        if method == "eth_getLogs":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "error": {"code": -32000}})
    s = clean_snapshot()
    evidence, _ = collect_evidence(s, unavailable)
    assert evidence["available"] and evidence["registry"] == s["chain"]["registry"]
    assert evidence["anchors"] is None and evidence["anchors_reason"] == "NODE_UNAVAILABLE"


def test_pending_proposal_is_omitted_not_compared_with_unconfirmed_chain_state():
    from test_chain_audit import clean_snapshot
    s = clean_snapshot()
    s["events"].append({"event_id": "evt_" + "8" * 26, "payload": {"proposal_id": "prp_" + "8" * 26},
                        "type": "ADMIN_ACTION_EXECUTED", "anchor": {"status": "PENDING"}})
    evidence, _ = collect_evidence(s)
    assert evidence["available"] and evidence["proposals"] == {}
    assert evidence["unconfirmed_proposals"] == ["prp_" + "8" * 26]


def test_anchor_above_confirmed_block_is_not_evidence():
    from test_chain_audit import clean_snapshot
    s = clean_snapshot()
    next(iter(s["chain"]["anchor_logs"].values()))["block_number"] = 6
    evidence, _ = collect_evidence(s)
    assert len(evidence["anchors"]) == 2


def test_collect_falls_back_from_lagging_observer_to_confirmed_commission():
    from test_chain_audit import clean_snapshot
    evidence, trace = collect_evidence(clean_snapshot(), heads={"observer": 4, "commission": 7})
    assert evidence["checkpoint_state"] is not None and evidence["confirmed_block"] == 5
    assert {host for host, _ in trace} == {"observer", "commission"}


def test_collect_keeps_best_partial_bundle_when_all_pins_lag():
    from test_chain_audit import clean_snapshot
    evidence, trace = collect_evidence(clean_snapshot(), heads={"observer": 3, "commission": 4})
    assert evidence["available"] and evidence["confirmed_block"] == 2 and evidence["checkpoint_state"] is None
