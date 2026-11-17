"""R5 comparisons against independently read, confirmed contract evidence.

The transport stores raw read results in ``snapshot['chain']``. The Gateway
supplies bodies and Merkle paths only; the deciding roots come from anchorAt.
Nothing here performs I/O or imports a Core/Gateway implementation.
"""
from __future__ import annotations

from datetime import datetime

from .hashing import H, h32, leaf_hash, merkle_root, node, rejections_root


_ZERO = "0" * 64
_VOTER = {"TOKEN_USED", "BALLOT_RECEIVED"}
_TRANSITIONS = {"ROSTER_FROZEN": "ROSTER_FROZEN", "ELECTION_OPENED": "OPEN", "ELECTION_CLOSED": "CLOSED",
                "TALLY_PUBLISHED": "TALLIED", "ELECTION_FINALIZED": "FINALIZED", "ELECTION_VOIDED": "VOIDED"}


def _seconds(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("An audit timestamp must include its timezone")
    return int(parsed.timestamp())


def replay_state(events: list[dict], last_core_seq: int) -> dict:
    """Apply the normative chain-interface state derivation in core seq order."""
    ordered = sorted((e for e in events if e.get("source") == "core" and "seq" in e
                      and e["seq"] <= last_core_seq), key=lambda e: e["seq"])
    creates = [e for e in ordered if e["type"] == "ELECTION_CREATED"]
    if len(creates) != 1 or not ordered or ordered[0] != creates[0]:
        raise ValueError("A unique creation event is required for the state replay")
    p = creates[0]["payload"]
    state = {"status": "DRAFT", "window_end": _seconds(p["window_end"]), "roster_hash": _ZERO,
             "token_set_root": _ZERO, "result_hash": _ZERO, "last_core_seq": last_core_seq}
    for event in ordered:
        kind, payload = event["type"], event["payload"]
        if kind in _TRANSITIONS:
            state["status"] = _TRANSITIONS[kind]
        if kind == "WINDOW_CHANGED":
            state["window_end"] = _seconds(payload["new_window_end"])
        elif kind == "ELECTION_CLOSED":
            state["window_end"] = min(state["window_end"], _seconds(payload["closed_at"]))
        elif kind == "ROSTER_FROZEN":
            state["roster_hash"] = payload["roster_hash"]
        elif kind == "ROSTER_AMENDED":
            state["roster_hash"] = payload["roster_hash_after"]
        elif kind in {"TOKEN_BATCH_ISSUED", "TOKEN_REVOKED"}:
            state["token_set_root"] = payload["token_set_root"]
        elif kind == "TALLY_PUBLISHED":
            state["result_hash"] = payload["result_hash"]
    return state


def _path_root(leaf: str, path: list[dict]) -> str:
    h32(leaf)
    if not isinstance(path, list) or len(path) > 64:
        raise ValueError("Invalid Merkle path")
    for step in path:
        if step["position"] == "left":
            leaf = node(step["hash"], leaf)
        elif step["position"] == "right":
            leaf = node(leaf, step["hash"])
        else:
            raise ValueError("Invalid Merkle path direction")
    return leaf


def _event_ref(refs, event):
    refs.append({"kind": "ledger_event", "ref": event["event_id"]})


def _state_checks(s, events, chain, out, refs):
    missing = False
    creates = [e for e in events if e["type"] == "ELECTION_CREATED"]
    selected = next((e for e in events if e["event_id"] == s["checkpoint_event_id"]), None)
    if len(creates) != 1 or selected is None:
        return "OTHER"
    created, p = creates[0], creates[0]["payload"]
    state, gov = chain.get("state"), chain.get("governance")
    if not isinstance(state, dict) or not isinstance(gov, dict):
        missing = True
    else:
        comparisons = [
            state["config_hash"] == p["config_hash"],
            state["window_start"] == _seconds(p["window_start"]),
            state["time_bucket_seconds"] == p["time_bucket_seconds"],
            gov["org_ids"] == [H(o["org_id"]) for o in p["organizations"]],
            [o["org_id"] for o in p["organizations"]] == sorted(o["org_id"] for o in p["organizations"]),
            gov["approval_threshold"] == p["approval_threshold"],
            gov["operator_org_id"] == H(p["operator_org"]),
            gov["proposal_ttl_hours"] == p["proposal_ttl_hours"],
            gov["checkpoint_interval_seconds"] == p["checkpoint_interval_seconds"],
            state["operator_org_id"] == H(p["operator_org"]),
        ]
        out["governance_mismatch"] = sum(not same for same in comparisons)
        if out["governance_mismatch"]:
            _event_ref(refs, created)
    saved = chain.get("checkpoint_state")
    if not isinstance(saved, dict):
        missing = True
    else:
        last = saved["last_core_seq"]
        if type(last) is not int or last < 1:
            raise ValueError("Invalid checkpoint core seq")
        expected = replay_state(events, last)
        out["chain_state_mismatch"] = sum(saved[key] != value for key, value in expected.items())
        # The checkpoint snapshot includes its own core sequence. Checking it
        # closes the otherwise possible attack of choosing an earlier prefix.
        out["chain_state_mismatch"] += int(last != selected["seq"])
        if out["chain_state_mismatch"]:
            _event_ref(refs, selected)
    return "NODE_UNAVAILABLE" if missing else None


def gateway_anchor_omissions(chain: dict, evidence: dict) -> list[str] | None:
    """Find confirmed node anchors absent from a complete Gateway index.

    These are observation candidates, not immediately proven violations: the
    Gateway's node or poller can lag the independently pinned node. The worker
    persists the first observation and repeats this read after its bounded wait
    before supplying ``confirmed_gateway_anchor_omissions`` to the audit.
    ``None`` means the independent bundle or complete index is unavailable.
    """
    if not isinstance(chain, dict) or chain.get("available") is not True:
        return None
    confirmed = chain.get("confirmed_block")
    anchors, logs = chain.get("anchors"), chain.get("anchor_logs")
    if (type(confirmed) is not int or confirmed < 0
            or not isinstance(anchors, dict) or not isinstance(logs, dict)
            or not isinstance(evidence, dict) or evidence.get("complete") is not True
            or not isinstance(evidence.get("anchors"), list)):
        return None
    known = set()
    for record in evidence["anchors"]:
        if not isinstance(record, dict) or not isinstance(record.get("anchor_id"), str):
            return None
        identity = H(record["anchor_id"])
        if identity in known:
            return None
        known.add(identity)
    absent = []
    for identity, anchor in anchors.items():
        log = logs.get(identity)
        if (not isinstance(anchor, dict) or not isinstance(log, dict)
                or type(log.get("block_number")) is not int
                or not 0 <= log["block_number"] <= confirmed):
            continue
        if identity not in known:
            absent.append(identity)
    return sorted(absent)


def _anchor_checks(s, events, rejections, chain, out, refs):
    evidence = s.get("anchor_evidence")
    node_anchors, node_logs = chain.get("anchors"), chain.get("anchor_logs")
    if not isinstance(node_anchors, dict) or not isinstance(node_logs, dict):
        return "NODE_UNAVAILABLE"
    if not isinstance(evidence, dict) or evidence.get("complete") is not True:
        return "BATCH_NOT_ANCHORED"
    records = evidence.get("anchors")
    if not isinstance(records, list):
        return "BATCH_NOT_ANCHORED"
    by_id, conflicting = {}, set()
    for record in records:
        identity = record["anchor_id"]
        if identity in by_id:
            conflicting.add(identity)
        by_id[identity] = record
    bodies = evidence.get("events", {})
    rejected = evidence.get("rejections", {})
    leaves_by_id = evidence.get("leaves", {})
    proofs = evidence.get("proofs", {})
    selected_events = {event["event_id"]: event for event in events}
    selected_rejections = {rejection["rejection_id"]: rejection for rejection in rejections}
    selected_anchor_ids = {event.get("anchor", {}).get("anchor_id") for event in events}
    selected_anchor_ids |= {rejection.get("anchor_id") for rejection in rejections}
    selected_anchor_ids.discard(None)
    confirmed = chain["confirmed_block"]
    # A batch can be anchored after its checkpoint, or straddle checkpoints.
    # Opening just anchors already named by the cumulative event list would let
    # a missing event disappear from both that list and the Core views. Every
    # anchor visible at the independently fixed confirmed block must be opened;
    # only authenticated leaf bodies establish which checkpoint each covers.
    relevant = set(selected_anchor_ids)
    for aid, record in by_id.items():
        block = record.get("block_number")
        if H(aid) in node_anchors or (type(block) is int and block <= confirmed):
            relevant.add(aid)
    for aid, bundle in leaves_by_id.items():
        if (any(leaf.get("event_id") in selected_events for leaf in bundle.get("leaves", []))
                or any(identity in selected_rejections for identity in bundle.get("rejection_ids", []))
                or any(isinstance(bodies.get(leaf.get("event_id")), dict)
                       and bodies[leaf["event_id"]].get("checkpoint", s["checkpoint"] + 1) <= s["checkpoint"]
                       for leaf in bundle.get("leaves", []))
                or any(isinstance(rejected.get(identity), dict)
                       and rejected[identity].get("checkpoint", s["checkpoint"] + 1) <= s["checkpoint"]
                       for identity in bundle.get("rejection_ids", []))):
            relevant.add(aid)
    bad_anchors = conflicting & relevant
    bad_leaf_events, unavailable_events, unavailable_rejections = set(), set(), set()
    missing = set(node_anchors) != set(node_logs)
    missing |= any(type(log.get("block_number")) is not int or not 0 <= log["block_number"] <= confirmed
                   for log in node_logs.values())
    omitted_events, omitted_rejections = set(), set()
    seen_event_anchors, seen_rejection_anchors = {}, {}
    # Even with a fixed independent read, the Gateway's own node or poller can
    # be behind. Only the worker's persisted wait plus a fresh complete index
    # read can promote the same still-absent anchor to a proven discrepancy.
    absent = gateway_anchor_omissions(chain, evidence) or []
    confirmed_absent = s.get("confirmed_gateway_anchor_omissions", [])
    if not isinstance(confirmed_absent, list) or not all(isinstance(aid, str) for aid in confirmed_absent):
        confirmed_absent = []
    confirmed_absent = set(absent).intersection(confirmed_absent)
    missing |= bool(set(absent) - confirmed_absent)
    out["chain_anchor_ids_missing_from_gateway"] = absent
    for aid in relevant:
        record = by_id.get(aid)
        if not isinstance(record, dict):
            missing = True
            continue
        onchain, log = node_anchors.get(H(aid)), node_logs.get(H(aid))
        if (not isinstance(onchain, dict) or not isinstance(log, dict)
                or type(log.get("block_number")) is not int or not 0 <= log["block_number"] <= confirmed):
            # A Gateway block claim is not proof that the anchor exists. Lag or
            # missing confirmed inclusion stays unavailable, not a mismatch.
            missing = True
            continue
        bundle = leaves_by_id.get(aid)
        if not isinstance(bundle, dict) or not isinstance(bundle.get("leaves"), list) or not isinstance(bundle.get("rejection_ids"), list):
            missing = True
            continue
        bad = aid in bad_anchors
        bad |= record.get("election_id") != s["election_id"] or bundle.get("anchor_id") != aid
        bad |= any(record.get(key) != onchain.get(key) for key in ("merkle_root", "rejections_root", "mode", "event_count"))
        bad |= any(log.get(key) != onchain.get(key) for key in ("merkle_root", "rejections_root", "mode", "event_count"))
        bad |= record.get("block_number") != log.get("block_number") or record.get("tx_hash", "").lower() != log.get("tx_hash", "").lower()
        leaves = bundle["leaves"]
        bad |= len(leaves) != onchain["event_count"]
        ids = [leaf["event_id"] for leaf in leaves]
        bad |= len(ids) != len(set(ids))
        computed, times, kinds = [], [], []
        complete_leaves = True
        for leaf in leaves:
            identity = leaf["event_id"]
            event = bodies.get(identity, selected_events.get(identity))
            if not isinstance(event, dict):
                complete_leaves = False
                missing = True
                continue
            if identity in seen_event_anchors and seen_event_anchors[identity] != aid:
                bad = True
                bad_anchors.add(seen_event_anchors[identity])
            seen_event_anchors[identity] = aid
            expected = leaf_hash(event)
            if event.get("checkpoint", s["checkpoint"] + 1) <= s["checkpoint"] and identity not in selected_events:
                omitted_events.add(identity)
            computed.append(expected)
            times.append(_seconds(event["occurred_at"]))
            kinds.append(event["type"])
            metadata = event.get("anchor", {})
            if expected != leaf["leaf_hash"]:
                bad_leaf_events.add(identity)
                bad = True
            if identity in selected_events and leaf_hash(selected_events[identity]) != expected:
                bad_leaf_events.add(identity)
                bad = True
            bad |= event.get("event_id") != identity or event.get("election_id") != s["election_id"]
            if metadata.get("status") == "PENDING":
                # snapshot() may predate collect() and the later Gateway tree.
                # The immutable body/leaf/root checks above still apply, but a
                # stale mutable status cannot prove a contradictory anchor.
                missing = True
                if identity in selected_events:
                    unavailable_events.add(identity)
            else:
                bad |= metadata.get("status") != "ANCHORED" or metadata.get("anchor_id") != aid
                bad |= any(metadata.get(key) != record.get(key) for key in ("merkle_root", "mode", "block_number"))
                bad |= metadata.get("tx_hash", "").lower() != record.get("tx_hash", "").lower()
        if complete_leaves:
            bad |= merkle_root(computed) != onchain["merkle_root"]
            if times:
                bad |= (min(times), max(times)) != (onchain["bucket_min"], onchain["bucket_max"])
            if onchain["mode"] == "immediate":
                bad |= len(leaves) != 1 or any(kind in _VOTER for kind in kinds)
            else:
                bad |= any(kind not in _VOTER for kind in kinds)
                brange = record.get("bucket_range", {})
                if not brange:
                    bad = True
                else:
                    bad |= (_seconds(brange["min"]), _seconds(brange["max"])) != (onchain["bucket_min"], onchain["bucket_max"])
        rejection_ids = bundle["rejection_ids"]
        bad |= len(rejection_ids) != len(set(rejection_ids))
        rejected_bodies = []
        for identity in rejection_ids:
            rejection = rejected.get(identity, selected_rejections.get(identity))
            if not isinstance(rejection, dict):
                missing = True
                continue
            if identity in seen_rejection_anchors and seen_rejection_anchors[identity] != aid:
                bad = True
                bad_anchors.add(seen_rejection_anchors[identity])
            seen_rejection_anchors[identity] = aid
            if rejection.get("checkpoint", s["checkpoint"] + 1) <= s["checkpoint"] and identity not in selected_rejections:
                omitted_rejections.add(identity)
            bad |= rejection.get("rejection_id") != identity or rejection.get("election_id") != s["election_id"]
            bad |= rejection.get("anchor_id") != aid
            if identity in selected_rejections:
                # Ignore only detail: rejection_leaf defines the hashed fields.
                bad |= rejections_root([rejection]) != rejections_root([selected_rejections[identity]])
            rejected_bodies.append(rejection)
        if len(rejected_bodies) == len(rejection_ids):
            computed_rejections = rejections_root(rejected_bodies)
            bad |= computed_rejections != onchain["rejections_root"] or bundle.get("rejections_root") != onchain["rejections_root"]
        if bad:
            bad_anchors.add(aid)
    for identity, event in selected_events.items():
        metadata = event.get("anchor", {})
        aid = metadata.get("anchor_id")
        record = by_id.get(aid)
        onchain = node_anchors.get(H(aid)) if isinstance(aid, str) else None
        log = node_logs.get(H(aid)) if isinstance(aid, str) else None
        proof = proofs.get(identity)
        if (metadata.get("status") != "ANCHORED" or not record or not onchain or not log
                or type(log.get("block_number")) is not int or not 0 <= log["block_number"] <= confirmed
                or not isinstance(proof, dict) or aid not in leaves_by_id):
            unavailable_events.add(identity)
            continue
        bad = seen_event_anchors.get(identity) != aid
        expected = leaf_hash(event)
        if proof.get("leaf_hash") != expected:
            bad_leaf_events.add(identity)
            bad = True
        bad |= proof.get("event_id") != identity or proof.get("anchor_id") != aid
        bad |= proof.get("merkle_root") != onchain["merkle_root"]
        bad |= proof.get("block_number") != log["block_number"] or proof.get("tx_hash", "").lower() != log["tx_hash"].lower()
        try:
            bad |= _path_root(expected, proof["path"]) != onchain["merkle_root"]
        except (KeyError, TypeError, ValueError):
            bad = True
        if bad:
            bad_anchors.add(aid)
            _event_ref(refs, event)
    for identity, rejection in selected_rejections.items():
        aid = rejection.get("anchor_id")
        record = by_id.get(aid)
        log = node_logs.get(H(aid)) if isinstance(aid, str) else None
        if (not record or not log or type(log.get("block_number")) is not int
                or not 0 <= log["block_number"] <= confirmed):
            unavailable_rejections.add(identity)
        elif seen_rejection_anchors.get(identity) != aid:
            if aid in leaves_by_id:
                bad_anchors.add(aid)
                refs.append({"kind": "ledger_rejection", "ref": identity})
            else:
                unavailable_rejections.add(identity)
    out["anchors_mismatch"] = len(bad_anchors) + len(confirmed_absent)
    out["events_hash_mismatch"] = (out.get("events_hash_mismatch") or 0) + len(bad_leaf_events)
    out["events_unanchored"] = len(unavailable_events)
    out["rejections_unanchored"] = len(unavailable_rejections)
    out["cumulative_anchor_events_missing"] = sorted(omitted_events)
    out["cumulative_anchor_rejections_missing"] = sorted(omitted_rejections)
    refs.extend({"kind": "anchor", "ref": aid} for aid in sorted(bad_anchors))
    refs.extend({"kind": "ledger_event", "ref": identity} for identity in sorted(bad_leaf_events))
    if omitted_events or omitted_rejections:
        return "OTHER"
    return "BATCH_NOT_ANCHORED" if missing or unavailable_events or unavailable_rejections else None


def audit_chain(snapshot, events, rejections, out, refs) -> tuple[bool, str | None]:
    """Update R5's chain counters; preserve uncertainty alongside proven faults."""
    for key in ("chain_state_mismatch", "governance_mismatch", "anchors_mismatch"):
        out[key] = None
    chain = snapshot.get("chain")
    if not isinstance(chain, dict) or chain.get("available") is not True:
        return False, "NODE_UNAVAILABLE"
    reasons = []
    try:
        reasons.append(_state_checks(snapshot, events, chain, out, refs))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError):
        reasons.append("OTHER")
    try:
        reasons.append(_anchor_checks(snapshot, events, rejections, chain, out, refs))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError):
        reasons.append("OTHER")
    failed = any(out.get(key) for key in ("chain_state_mismatch", "governance_mismatch", "anchors_mismatch", "events_hash_mismatch"))
    reason = next((item for item in ("NODE_UNAVAILABLE", "BATCH_NOT_ANCHORED", "OTHER") if item in reasons), None)
    return bool(failed), reason
