"""HTTP anchor evidence: scope, pagination, late proofs and collection bounds."""
from contextlib import contextmanager
from copy import deepcopy

import httpx
import pytest

from purevoting_rules.engine import evaluate_rule
from purevoting_rules.hashing import H, Hc, leaf_hash, merkle_root
from purevoting_rules.live_inputs import InputError, LiveInputs
from test_complete_tally import complete_tally, _unix


class AnchorAPI:
    def __init__(self):
        self.snapshot = complete_tally()
        self.evidence = deepcopy(self.snapshot["anchor_evidence"])
        self.evidence["anchors"].sort(key=lambda a: (a["block_number"], a["anchor_id"]))
        self.calls = []
        self.errors = {}
        self.change = lambda path, query, data: data

    def handle(self, request):
        assert request.method == "GET"
        assert request.headers["X-Service-Key"] == "only-the-configured-gateway"
        path, query = request.url.path, request.url.params
        self.calls.append((path, dict(query)))
        if path in self.errors:
            return httpx.Response(self.errors[path], json={"detail": "not available"})
        if path == "/v1/anchors":
            assert query["election_id"] == self.snapshot["election_id"]
            start, limit = int(query.get("cursor", 0)), int(query["limit"])
            records = self.evidence["anchors"]
            data = {"items": records[start:start + limit], "next_cursor": str(start + limit) if start + limit < len(records) else None}
        elif path.startswith("/v1/anchors/"):
            data = self.evidence["leaves"][path.split("/")[3]]
        elif path.endswith("/proof"):
            data = self.evidence["proofs"][path.split("/")[3]]
        elif path.startswith("/v1/events/"):
            data = self.evidence["events"][path.split("/")[3]]
        elif path.startswith("/v1/rejections/"):
            data = self.evidence["rejections"][path.split("/")[3]]
        else:
            raise AssertionError(path)
        data = self.change(path, query, deepcopy(data))
        return data if isinstance(data, httpx.Response) else httpx.Response(200, json=data)


@contextmanager
def client(api, **bounds):
    with httpx.Client(base_url="https://gateway.test", headers={"X-Service-Key": "only-the-configured-gateway"},
                      transport=httpx.MockTransport(api.handle)) as http:
        yield LiveInputs(http, http, page_size=7, **bounds)


def read(api, inputs, *, use_chain=True):
    s = api.snapshot
    return inputs.anchor_evidence(s["election_id"], s["checkpoint"], s["events"], s["rejections"],
                                 chain=s["chain"] if use_chain else None)


def extra_confirmed_anchor(api, *, hidden_vote=False, block=500, forged_height=False):
    """An independently confirmed anchor not named by any selected event.

    The hidden-vote case models an accepted TOKEN_USED omitted from both the
    cumulative listing and Core's visible state. Its anchor can be published
    after the selected checkpoint anchor because voter batching is asynchronous.
    """
    template = next(e for e in api.snapshot["events"] if e["type"] == ("TOKEN_USED" if hidden_vote else "TALLY_PUBLISHED"))
    event = deepcopy(template)
    event["event_id"] = ("evr_" if hidden_vote else "evt_") + "Z" * 26
    event["checkpoint"] = 2 if hidden_vote else 5
    if hidden_vote:
        event["payload"] = {"token_hash": api.snapshot["views"]["token_status"][4]["row_key"]}
    else:
        event.update(type="ELECTION_FINALIZED", seq=12, occurred_at="2026-11-03T09:47:00Z", received_at="2026-11-03T09:47:00Z")
        event["payload"] = {"result_hash": template["payload"]["result_hash"]}
    event["payload_hash"] = Hc(event["payload"])
    aid = "anc_" + "Z" * 26
    root = leaf_hash(event)
    tx = "0x" + H("extra confirmed anchor")
    event["anchor"].update(anchor_id=aid, merkle_root=root, tx_hash=tx, block_number=block)
    record = {"anchor_id": aid, "election_id": api.snapshot["election_id"], "mode": event["anchor"]["mode"],
              "merkle_root": root, "rejections_root": H(""), "event_count": 1,
              "tx_hash": tx, "block_number": 1001 if forged_height else block, "anchored_at": event["occurred_at"]}
    if record["mode"] == "batched":
        record["bucket_range"] = {"min": event["occurred_at"], "max": event["occurred_at"]}
    api.evidence["anchors"].append(record)
    api.evidence["anchors"].sort(key=lambda a: (a["block_number"], a["anchor_id"]))
    api.evidence["events"][event["event_id"]] = event
    api.evidence["leaves"][aid] = {"anchor_id": aid, "leaves": [{"event_id": event["event_id"], "leaf_hash": root}],
                                     "rejection_ids": [], "rejections_root": H("")}
    api.snapshot["chain"]["anchors"][H(aid)] = {key: record[key] for key in ("merkle_root", "rejections_root", "mode", "event_count")}
    api.snapshot["chain"]["anchors"][H(aid)].update(bucket_min=_unix(event["occurred_at"]), bucket_max=_unix(event["occurred_at"]))
    api.snapshot["chain"]["anchor_logs"][H(aid)] = {key: record[key] for key in ("merkle_root", "rejections_root", "mode", "event_count", "tx_hash")}
    api.snapshot["chain"]["anchor_logs"][H(aid)]["block_number"] = block
    return event, aid


def future_in_selected_batch(api, *, checkpoint=5):
    """Add a later leaf to an existing selected voter anchor, updating real hashes."""
    event = next(e for e in api.snapshot["events"] if e["type"] == "TOKEN_USED")
    future = deepcopy(event)
    future.update(event_id="evr_" + "Z" * 26, checkpoint=checkpoint)
    aid = event["anchor"]["anchor_id"]
    bodies = sorted([event, future], key=lambda e: (e["occurred_at"], e["event_id"]))
    leaves = [{"event_id": e["event_id"], "leaf_hash": leaf_hash(e)} for e in bodies]
    root = merkle_root(leaf["leaf_hash"] for leaf in leaves)
    event["anchor"]["merkle_root"] = future["anchor"]["merkle_root"] = root
    for e in bodies:
        api.evidence["events"][e["event_id"]] = deepcopy(e)
    api.evidence["leaves"][aid]["leaves"] = leaves
    anchor = next(a for a in api.evidence["anchors"] if a["anchor_id"] == aid)
    anchor.update(merkle_root=root, event_count=2)
    for section in ("anchors", "anchor_logs"):
        api.snapshot["chain"][section][H(aid)].update(merkle_root=root, event_count=2)
    index = next(i for i, leaf in enumerate(leaves) if leaf["event_id"] == event["event_id"])
    proof = api.evidence["proofs"][event["event_id"]]
    proof.update(merkle_root=root, path=[{"position": "right" if index == 0 else "left", "hash": leaves[1 - index]["leaf_hash"]}])
    return event, future, aid


def test_complete_anchor_index_is_paginated_and_all_selected_proofs_verify():
    api = AnchorAPI()
    before = deepcopy(api.snapshot)
    with client(api) as inputs:
        evidence = read(api, inputs)
    assert len([1 for path, _ in api.calls if path == "/v1/anchors"]) == 3
    assert set(evidence["proofs"]) == {e["event_id"] for e in api.snapshot["events"]}
    api.snapshot["anchor_evidence"] = evidence
    assert evaluate_rule("R5", api.snapshot).result == "PASS"
    assert before["events"] == api.snapshot["events"]


def test_unreferenced_anchor_beyond_fixed_confirmed_node_block_is_not_fetched():
    api = AnchorAPI()
    record = deepcopy(api.evidence["anchors"][-1])
    record.update(anchor_id="anc_" + "Z" * 26, block_number=1001)
    api.evidence["anchors"].append(record)
    bad_path = "/v1/anchors/" + record["anchor_id"] + "/leaves"
    api.errors[bad_path] = 503
    with client(api) as inputs:
        evidence = read(api, inputs)
    assert bad_path not in [path for path, _ in api.calls]
    assert record in evidence["anchors"]  # Index is retained for node enumeration checks.
    api.snapshot["anchor_evidence"] = evidence
    assert evaluate_rule("R5", api.snapshot).result == "PASS"


def test_confirmed_future_only_anchor_is_fully_read_before_earlier_checkpoint_can_pass():
    api = AnchorAPI()
    event, aid = extra_confirmed_anchor(api)
    before = deepcopy(api.snapshot["events"])
    with client(api) as inputs:
        evidence = read(api, inputs)
    assert ("/v1/anchors/" + aid + "/leaves", {}) in api.calls
    assert evidence["events"][event["event_id"]] == event
    assert before == api.snapshot["events"]
    api.snapshot["anchor_evidence"] = evidence
    assert evaluate_rule("R5", api.snapshot).result == "PASS"


@pytest.mark.parametrize("missing", ["tree", "body"])
def test_confirmed_anchor_of_unknown_checkpoint_scope_prevents_pass_when_unavailable(missing):
    api = AnchorAPI()
    event, aid = extra_confirmed_anchor(api)
    path = "/v1/anchors/" + aid + "/leaves" if missing == "tree" else "/v1/events/" + event["event_id"]
    api.errors[path] = 503
    with client(api) as inputs:
        evidence = read(api, inputs)
    assert path in [p for p, _ in api.calls]
    api.snapshot["anchor_evidence"] = evidence
    result = evaluate_rule("R5", api.snapshot)
    assert result.result == "INCONCLUSIVE", (result.summary, result.observed)
    assert result.inconclusive_reason == "BATCH_NOT_ANCHORED"


@pytest.mark.parametrize("after_checkpoint", [False, True])
@pytest.mark.parametrize("forged_height", [False, True])
def test_hidden_vote_anchor_is_opened_before_and_after_checkpoint_anchor_even_with_forged_height(after_checkpoint, forged_height):
    api = AnchorAPI()
    checkpoint = next(e for e in api.snapshot["events"] if e["event_id"] == api.snapshot["checkpoint_event_id"])
    block = checkpoint["anchor"]["block_number"] + (10 if after_checkpoint else -1)
    event, aid = extra_confirmed_anchor(api, hidden_vote=True, block=block, forged_height=forged_height)
    assert event["event_id"] not in {e["event_id"] for e in api.snapshot["events"]}
    assert api.snapshot["views"]["token_status"][4]["used_checkpoint"] is None
    with client(api) as inputs, pytest.raises(InputError) as error:
        read(api, inputs)
    assert error.value.kind == "invalid"
    assert ("/v1/anchors/" + aid + "/leaves", {}) in api.calls
    assert ("/v1/events/" + event["event_id"], {}) in api.calls


def test_without_independent_chain_bound_adapter_does_not_trust_gateway_future_height():
    api = AnchorAPI()
    event, aid = extra_confirmed_anchor(api, hidden_vote=True, forged_height=True)
    with client(api) as inputs, pytest.raises(InputError) as error:
        read(api, inputs, use_chain=False)
    assert error.value.kind == "invalid"
    assert ("/v1/anchors/" + aid + "/leaves", {}) in api.calls


def test_cross_checkpoint_batch_reads_future_leaf_without_polluting_cumulative_events():
    api = AnchorAPI()
    _, future, _ = future_in_selected_batch(api)
    original_events = deepcopy(api.snapshot["events"])
    with client(api) as inputs:
        evidence = read(api, inputs)
    assert evidence["events"][future["event_id"]] == future
    assert ("/v1/events/" + future["event_id"], {}) in api.calls
    assert original_events == api.snapshot["events"]
    assert all(e["checkpoint"] <= 4 for e in api.snapshot["events"])
    api.snapshot["anchor_evidence"] = evidence
    outcome = evaluate_rule("R5", api.snapshot)
    assert outcome.result == "PASS", (outcome.summary, outcome.observed)


def test_discovered_omitted_cumulative_event_aborts_instead_of_passing_partial_history():
    api = AnchorAPI()
    future_in_selected_batch(api, checkpoint=2)
    with client(api) as inputs, pytest.raises(InputError) as error:
        read(api, inputs)
    assert (error.value.source, error.value.kind) == ("gateway", "invalid")


@pytest.mark.parametrize("missing", ["proof", "tree", "future_body"])
def test_temporarily_missing_selected_evidence_remains_inconclusive(missing):
    api = AnchorAPI()
    event = next(e for e in api.snapshot["events"] if e["type"] == "TOKEN_USED")
    if missing == "proof":
        path = "/v1/events/" + event["event_id"] + "/proof"
    elif missing == "tree":
        path = "/v1/anchors/" + event["anchor"]["anchor_id"] + "/leaves"
    else:
        _, future, _ = future_in_selected_batch(api)
        path = "/v1/events/" + future["event_id"]
    api.errors[path] = 404
    with client(api) as inputs:
        evidence = read(api, inputs)
    api.snapshot["anchor_evidence"] = evidence
    outcome = evaluate_rule("R5", api.snapshot)
    assert outcome.result == "INCONCLUSIVE", (outcome.summary, outcome.observed)
    assert outcome.inconclusive_reason == "BATCH_NOT_ANCHORED"


def test_unavailable_anchor_index_aborts_collection():
    api = AnchorAPI()
    api.errors["/v1/anchors"] = 503
    with client(api) as inputs, pytest.raises(InputError) as error:
        read(api, inputs)
    assert error.value.kind == "unavailable"


def test_anchor_record_cap_aborts_before_tree_reads():
    api = AnchorAPI()
    with client(api, max_anchor_records=3) as inputs, pytest.raises(InputError):
        read(api, inputs)
    assert all(path == "/v1/anchors" for path, _ in api.calls)


def test_tree_member_cap_aborts_before_extra_body_gets():
    api = AnchorAPI()
    _, future, aid = future_in_selected_batch(api)
    api.evidence["anchors"] = [a for a in api.evidence["anchors"] if a["anchor_id"] == aid]
    with client(api, max_anchor_members=1) as inputs, pytest.raises(InputError):
        read(api, inputs)
    assert "/v1/events/" + future["event_id"] not in [path for path, _ in api.calls]


@pytest.mark.parametrize("bad", ["duplicate_anchor", "wrong_election", "reverse_blocks", "repeat_cursor", "oversized_page", "wrong_tree_id", "wrong_proof_id", "invalid_proof_hash"])
def test_malformed_identity_order_pagination_or_schema_aborts(bad):
    api = AnchorAPI()
    if bad == "duplicate_anchor":
        api.evidence["anchors"].insert(1, deepcopy(api.evidence["anchors"][0]))
    elif bad == "wrong_election":
        api.evidence["anchors"][0]["election_id"] = "elc_" + "Z" * 26
    elif bad == "reverse_blocks":
        api.evidence["anchors"][:2] = reversed(api.evidence["anchors"][:2])
    else:
        def change(path, query, data):
            if path == "/v1/anchors":
                if bad == "repeat_cursor":
                    data["next_cursor"] = "7"
                if bad == "oversized_page":
                    data["items"] = api.evidence["anchors"]
            elif path.endswith("/leaves") and bad == "wrong_tree_id":
                data["anchor_id"] = "anc_" + "Z" * 26
            elif path.endswith("/proof"):
                if bad == "wrong_proof_id":
                    data["event_id"] = "evr_" + "Z" * 26
                if bad == "invalid_proof_hash":
                    data["leaf_hash"] = "not-a-hash"
            return data
        api.change = change
    with client(api) as inputs, pytest.raises(InputError) as error:
        read(api, inputs)
    assert error.value.kind == "invalid"


@pytest.mark.parametrize("bad", ["identity", "election"])
def test_extra_tree_body_cannot_substitute_another_identity_or_election(bad):
    api = AnchorAPI()
    _, future, _ = future_in_selected_batch(api)
    body = api.evidence["events"][future["event_id"]]
    if bad == "identity":
        body["event_id"] = "evr_" + "Y" * 26
    else:
        body["election_id"] = "elc_" + "Y" * 26
    with client(api) as inputs, pytest.raises(InputError) as error:
        read(api, inputs)
    assert error.value.kind == "invalid"
