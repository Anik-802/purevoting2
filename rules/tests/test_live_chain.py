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
