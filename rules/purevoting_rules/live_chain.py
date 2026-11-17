"""Pinned EVM reads at a fixed confirmed block, independent of the Gateway.

``assess`` supplies the worker's scheduling gate. ``collect`` reads the actual
contract values used by R4/R5; a reported block height alone proves no finding.
"""
from __future__ import annotations

import os
import re
import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from eth_abi import decode, encode
from eth_utils import keccak

from .hashing import H


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_ANCHOR_ID = re.compile(r"anc_[0-9A-HJKMNP-TV-Z]{26}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TX = re.compile(r"0x[0-9a-fA-F]{64}")
_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,77})")
_DATA = re.compile(r"0x(?:[0-9a-fA-F]{2})*")
_STATUS = ("DRAFT", "ROSTER_FROZEN", "OPEN", "CLOSED", "TALLIED", "FINALIZED", "VOIDED")
_PROPOSAL_STATUS = ("PENDING", "APPROVED", "REJECTED", "EXPIRED")
_ANCHOR_TOPIC = "0x" + keccak(text="AnchorCommitted(bytes32,bytes32,bytes32,bytes32,uint8,uint32)").hex()

# This is the normative read-only ABI, not an import of the Gateway/Core code.
_READ_ABI = {
    "orgSigner": (("bytes32",), ("address", "address")),
    "operatorOrg": ((), ("bytes32", "bool")),
    "electionState": (("bytes32",), ("uint8", "uint64", "uint64", "uint32", "bytes32", "bytes32", "bytes32", "bytes32", "uint64", "uint64", "bytes32")),
    "electionGovernance": (("bytes32",), ("bytes32[]", "uint8", "bytes32", "uint32", "uint32")),
    "electionStateAtCheckpoint": (("bytes32", "uint64"), ("uint8", "uint64", "bytes32", "bytes32", "bytes32", "uint64")),
    "proposalState": (("bytes32",), ("uint8", "bytes32", "uint8", "bool")),
    "anchorCount": (("bytes32",), ("uint256",)),
    "anchorAt": (("bytes32", "uint256"), ("bytes32", "bytes32", "bytes32", "uint8", "uint32", "uint64", "uint64")),
}


class ChainReadUnavailable(Exception):
    """The pinned node did not supply a bounded, well-formed confirmed read."""


def _rpc(http: httpx.Client, url: str, method: str, params: list):
    # Build the request ourselves: client auth, cookies and service-key defaults
    # are intentionally not inherited, even if a caller reuses a service client.
    req = httpx.Request("POST", url, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
    try:
        response = http.send(req, auth=None, follow_redirects=False, stream=True)
        try:
            if response.status_code != 200:
                raise ChainReadUnavailable("RPC HTTP response was not successful")
            data = bytearray()
            for part in response.iter_bytes():
                data.extend(part)
                if len(data) > 8 * 1024 * 1024:
                    raise ChainReadUnavailable("RPC response exceeds the audit bound")
            reply = json.loads(data)
        finally:
            response.close()
        if (not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0"
                or type(reply.get("id")) is not int or reply["id"] != 2
                or "error" in reply or "result" not in reply):
            raise ChainReadUnavailable("Malformed or failed JSON-RPC reply")
        return reply["result"]
    except (httpx.HTTPError, ValueError, RecursionError) as exc:
        raise ChainReadUnavailable("Pinned RPC read failed") from exc


def _call(http: httpx.Client, url: str, contract: str, block: int, name: str, *args):
    inputs, outputs = _READ_ABI[name]
    selector = keccak(text=name + "(" + ",".join(inputs) + ")")[:4]
    payload = "0x" + (selector + encode(inputs, args)).hex()
    value = _rpc(http, url, "eth_call", [{"to": contract, "data": payload}, hex(block)])
    if not isinstance(value, str) or not _DATA.fullmatch(value):
        raise ChainReadUnavailable("Malformed ABI reply")
    try:
        raw = bytes.fromhex(value[2:])
        decoded = decode(outputs, raw, strict=True)
        # Reject trailing data and non-canonical dynamic layouts as well.
        if encode(outputs, decoded) != raw:
            raise ValueError("Non-canonical ABI reply")
        return decoded
    except Exception as exc:
        # eth-abi has multiple decoding exception classes. No external exception
        # or response body (possibly containing secrets) is propagated to reports.
        raise ChainReadUnavailable("Invalid ABI reply") from exc


def _anchor_logs(http: httpx.Client, url: str, contract: str, election_hash: str, block: int) -> dict:
    value = _rpc(http, url, "eth_getLogs", [{"address": contract, "fromBlock": "0x0", "toBlock": hex(block),
                                            "topics": [_ANCHOR_TOPIC, "0x" + election_hash]}])
    if not isinstance(value, list) or len(value) > 4096:
        raise ChainReadUnavailable("Invalid or overlarge anchor log list")
    logs = {}
    for log in value:
        try:
            if (not isinstance(log, dict) or log.get("removed", False) is not False
                    or log["address"].lower() != contract.lower()
                    or len(log["topics"]) != 3 or log["topics"][0].lower() != _ANCHOR_TOPIC
                    or log["topics"][1].lower() != "0x" + election_hash
                    or not _TX.fullmatch(log["topics"][2]) or not _TX.fullmatch(log["transactionHash"])
                    or not _QUANTITY.fullmatch(log["blockNumber"]) or int(log["blockNumber"], 16) > block
                    or not _DATA.fullmatch(log["data"])):
                raise ValueError("Malformed log")
            raw = bytes.fromhex(log["data"][2:])
            types = ("bytes32", "bytes32", "uint8", "uint32")
            result = decode(types, raw, strict=True)
            if encode(types, result) != raw or result[2] not in (0, 1):
                raise ValueError("Malformed anchor event")
            entry = {"block_number": int(log["blockNumber"], 16), "tx_hash": log["transactionHash"].lower(),
                     "merkle_root": result[0].hex(), "rejections_root": result[1].hex(),
                     "mode": ("immediate", "batched")[result[2]], "event_count": result[3]}
            key = log["topics"][2][2:].lower()
            if key in logs and logs[key] != entry:
                raise ValueError("Conflicting anchor logs")
            logs[key] = entry
        except Exception as exc:
            raise ChainReadUnavailable("Malformed confirmed anchor log") from exc
    return logs


def _chain_id(value: str) -> int | None:
    if isinstance(value, str):
        if _QUANTITY.fullmatch(value):
            return int(value, 16)
        if _DECIMAL.fullmatch(value) and int(value) < 2**256:
            return int(value)
    return None


def _url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip() or any(ord(c) <= 32 for c in value):
        raise ValueError("Invalid PINNED_NODE_URL")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            is not None or parts.password is not None or parts.fragment):
        raise ValueError("Invalid PINNED_NODE_URL")
    # Check explicitly; urllib defers validation of nonnumeric/out-of-range ports.
    _ = parts.port
    return value.rstrip("/")


@dataclass(frozen=True)
class ChainPins:
    chain_id: str
    contract_address: str
    urls: tuple[str, ...]
    depth: int

    def __post_init__(self) -> None:
        if _chain_id(self.chain_id) is None:
            raise ValueError("CHAIN_ID must be an EVM chain ID in decimal or hex")
        if (not isinstance(self.contract_address, str) or not _ADDRESS.fullmatch(self.contract_address)
                or int(self.contract_address, 16) == 0):
            raise ValueError("CONTRACT_ADDRESS must be a nonzero EVM address")
        if type(self.depth) is not int or self.depth < 0:
            raise ValueError("CONFIRMATION_DEPTH must be a nonnegative integer")
        if not isinstance(self.urls, (list, tuple)) or not self.urls:
            raise ValueError("Configure at least one PINNED_NODE_URL")
        urls = tuple(_url(v) for v in self.urls)
        if len(urls) != len(set(urls)):
            raise ValueError("Duplicate PINNED_NODE_URL")
        object.__setattr__(self, "urls", urls)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ChainPins:
        env = os.environ if env is None else env
        raw_depth = env.get("CONFIRMATION_DEPTH", "2")
        if not _DECIMAL.fullmatch(raw_depth):
            raise ValueError("CONFIRMATION_DEPTH must be a nonnegative integer")
        return cls(env.get("CHAIN_ID", "").strip(), env.get("CONTRACT_ADDRESS", "").strip(),
                   tuple(i.strip() for i in env.get("PINNED_NODE_URL", "").split(",")), int(raw_depth))

    def differences(self, gateway_info: object) -> tuple[str, ...]:
        """Compare /v1/chain with local pins. Never follow addresses from this response.

        The network list may contain extra validators. Each local URL must occur
        in the advertised set; the order of connection attempts is always local.
        """
        g = gateway_info if isinstance(gateway_info, Mapping) else {}
        listed = g.get("rpc_urls")
        valid_list = isinstance(listed, list) and bool(listed) and all(isinstance(i, str) for i in listed)
        peer_set = {x.rstrip("/") for x in listed} if valid_list else set()
        addr = g.get("contract_address")
        checks = {
            "platform": g.get("platform") == "other",  # D-02 C: PureChain
            "interface_version": g.get("interface_version") == "1.1.0",
            "chain_id": _chain_id(g.get("chain_id")) == _chain_id(self.chain_id),
            "contract_address": isinstance(addr, str) and addr.lower() == self.contract_address.lower(),
            "confirmation_depth": type(g.get("confirmation_depth")) is int and g["confirmation_depth"] == self.depth,
            "rpc_urls": set(self.urls) <= peer_set,
        }
        return tuple(k for k, ok in checks.items() if not ok)


def reported_anchor_block(event: object) -> int | None:
    """Return the reported height only for a complete ANCHORED record."""
    a = event.get("anchor") if isinstance(event, Mapping) else None
    if not isinstance(a, Mapping) or a.get("status") != "ANCHORED":
        return None
    for key, pattern in (("anchor_id", _ANCHOR_ID), ("merkle_root", _HASH), ("tx_hash", _TX)):
        if not isinstance(a.get(key), str) or not pattern.fullmatch(a[key]):
            return None
    if a.get("mode") not in ("immediate", "batched"):
        return None
    b = a.get("block_number")
    return b if type(b) is int and b >= 0 else None


@dataclass(frozen=True)
class ChainVisibility:
    checkpoint_deep: bool
    anchors_deep: bool
    reason: str | None
    latest: int | None
    node_available: bool
    config_mismatch: bool = False


def _height(rpc, url, method) -> int | None:
    # An explicit anonymous request prevents a shared client from attaching
    # Core/Gateway authorization headers/cookies. Redirects are never followed.
    try:
        req = httpx.Request("POST", url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": []})
        response = rpc.send(req, auth=None, follow_redirects=False)
        if response.status_code != 200:
            return None
        value = response.json()
        if (not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
                or type(value.get("id")) is not int or value["id"] != 1 or "error" in value):
            return None
        result = value.get("result")
        return int(result, 16) if isinstance(result, str) and _QUANTITY.fullmatch(result) else None
    except (httpx.HTTPError, ValueError, RecursionError):
        return None


class PinnedChain:
    def __init__(self, pins: ChainPins, http: httpx.Client):
        self.pins, self.http = pins, http

    def collect(self, snapshot: dict, anchor_evidence: dict | None = None) -> dict:
        """Read R4/R5 evidence on one locally pinned node at ``head - D``.

        Every value in the returned bundle comes from the same node and explicit
        block. A missing/reverting optional governance getter does not erase the
        other evidence, but remains missing (it cannot authorize an action).
        Gateway data selects IDs only; neither its roots nor RPC URLs are trusted.
        """
        fallback = {"available": False, "reason": "NODE_UNAVAILABLE", "config_mismatch": False}
        fallback_score = (-1, -1)
        mismatch = False
        for url in self.pins.urls:
            try:
                identity = _rpc(self.http, url, "eth_chainId", [])
                if not isinstance(identity, str) or not _QUANTITY.fullmatch(identity):
                    continue
                height = _rpc(self.http, url, "eth_blockNumber", []) if _chain_id(identity) == _chain_id(self.pins.chain_id) else None
                if _chain_id(identity) != _chain_id(self.pins.chain_id):
                    mismatch = True
                    continue
                if not isinstance(height, str) or not _QUANTITY.fullmatch(height):
                    continue
                block = int(height, 16) - self.pins.depth
                if block < 0:
                    continue
                collected = self._collect_at(url, block, snapshot)
                collected["config_mismatch"] = mismatch
                required_blocks = [reported_anchor_block(event) for event in snapshot.get("events", [])]
                required_anchors = {event.get("anchor", {}).get("anchor_id") for event in snapshot.get("events", [])}
                bundles = anchor_evidence if isinstance(anchor_evidence, dict) else snapshot.get("anchor_evidence", {})
                anchor_records = {a["anchor_id"]: a for a in bundles.get("anchors", [])} if isinstance(bundles, dict) else {}
                for rejection in snapshot.get("rejections", []):
                    aid = rejection.get("anchor_id")
                    required_anchors.add(aid)
                    b = anchor_records.get(aid, {}).get("block_number")
                    required_blocks.append(b if type(b) is int and b >= 0 else None)
                complete = all(collected.get(key) is not None for key in ("state", "governance", "checkpoint_state", "anchors", "anchor_logs"))
                complete &= bool(required_blocks) and all(height is not None and height <= block for height in required_blocks)
                complete &= not collected["unconfirmed_proposals"]
                if complete:
                    complete &= all(isinstance(aid, str) and H(aid) in collected["anchors"] and H(aid) in collected["anchor_logs"]
                                    for aid in required_anchors)
                if complete:
                    return collected
                score = (sum(collected.get(key) is not None for key in ("state", "governance", "checkpoint_state", "anchors", "anchor_logs")), block)
                if score > fallback_score:
                    fallback, fallback_score = collected, score
            except (ChainReadUnavailable, KeyError, IndexError, TypeError, ValueError):
                continue
        fallback["config_mismatch"] = mismatch
        return fallback

    def _collect_at(self, url: str, block: int, snapshot: dict) -> dict:
        def call(name, *args):
            return _call(self.http, url, self.pins.contract_address, block, name, *args)

        election = snapshot["election_id"]
        election_hash = H(election)
        election_id = bytes.fromhex(election_hash)
        events = snapshot.get("events", [])
        created = [e for e in events if e.get("type") == "ELECTION_CREATED"]
        orgs = set()
        for event in created:
            orgs.update(o["org_id"] for o in event["payload"]["organizations"])
        for config in snapshot.get("views", {}).get("election_config") or []:
            orgs.update(o["org_id"] for o in config["organizations"])
        for event in events:
            payload = event.get("payload", {})
            orgs.update(payload[key] for key in ("org_id", "requested_by_org") if isinstance(payload.get(key), str))
        if len(orgs) > 256:
            raise ChainReadUnavailable("Organization audit bound exceeded")
        registry = {}
        for org in sorted(orgs):
            signer, registered_by = call("orgSigner", bytes.fromhex(H(org)))
            registry[org] = {"signer": signer.lower(), "registered_by": registered_by.lower()}
        operator = call("operatorOrg")
        gov = call("electionGovernance", election_id)
        state = call("electionState", election_id)
        if state[0] >= len(_STATUS):
            raise ChainReadUnavailable("Invalid election status")
        result = {
            "available": True, "reason": None, "confirmed_block": block,
            "chain_id": _chain_id(self.pins.chain_id), "contract_address": self.pins.contract_address.lower(),
            "registry": registry, "operator": {"org_id": operator[0].hex(), "confirmed_by_all": operator[1]},
            "governance": {"org_ids": [org.hex() for org in gov[0]], "approval_threshold": gov[1],
                           "operator_org_id": gov[2].hex(), "proposal_ttl_hours": gov[3], "checkpoint_interval_seconds": gov[4]},
            "state": {"status": _STATUS[state[0]], "window_start": state[1], "window_end": state[2],
                      "time_bucket_seconds": state[3], "config_hash": state[4].hex(), "roster_hash": state[5].hex(),
                      "token_set_root": state[6].hex(), "result_hash": state[7].hex(), "last_checkpoint": state[8],
                      "last_core_seq": state[9], "operator_org_id": state[10].hex()},
            "checkpoint_state": None, "proposals": {}, "unconfirmed_proposals": [],
            "anchors": None, "anchor_logs": None, "anchors_reason": "NODE_UNAVAILABLE",
        }
        # All-zero reads are the contract's explicit 'not stored' markers. They
        # are unavailable evidence, never zero-valued proof of a matching state.
        if state[4] == bytes(32):
            result["state"] = None
            result["governance"] = None
        selected = next((event for event in events if event.get("event_id") == snapshot.get("checkpoint_event_id")), None)
        selected_block = reported_anchor_block(selected)
        if selected_block is not None and selected_block <= block:
            saved = call("electionStateAtCheckpoint", election_id, snapshot["checkpoint"])
            if saved[0] >= len(_STATUS):
                raise ChainReadUnavailable("Invalid checkpoint status")
            if saved[1] != 0:
                result["checkpoint_state"] = {"status": _STATUS[saved[0]], "window_end": saved[1],
                                              "roster_hash": saved[2].hex(), "token_set_root": saved[3].hex(),
                                              "result_hash": saved[4].hex(), "last_core_seq": saved[5]}
        proposal_events = {}
        for event in events:
            proposal = event.get("payload", {}).get("proposal_id")
            if isinstance(proposal, str):
                proposal_events.setdefault(proposal, []).append(event)
        if len(proposal_events) > 4096:
            raise ChainReadUnavailable("Proposal audit bound exceeded")
        for proposal, related in sorted(proposal_events.items()):
            # An unconfirmed approval/execution must not be compared to an older
            # confirmed getter and mislabeled as a proven contract discrepancy.
            if any(reported_anchor_block(e) is None or reported_anchor_block(e) > block for e in related):
                result["unconfirmed_proposals"].append(proposal)
                continue
            try:
                status, target, approvals, executed = call("proposalState", bytes.fromhex(H(proposal)))
                if status >= len(_PROPOSAL_STATUS):
                    raise ChainReadUnavailable("Invalid proposal status")
                result["proposals"][proposal] = {"status": _PROPOSAL_STATUS[status], "target_hash": target.hex(),
                                                 "approvals": approvals, "executed": executed}
            except ChainReadUnavailable:
                result["unconfirmed_proposals"].append(proposal)
        try:
            count, = call("anchorCount", election_id)
            if count > 4096:
                raise ChainReadUnavailable("Anchor audit bound exceeded")
            anchors = {}
            for i in range(count):
                a = call("anchorAt", election_id, i)
                if a[3] not in (0, 1) or a[0].hex() in anchors:
                    raise ChainReadUnavailable("Invalid anchor enumeration")
                anchors[a[0].hex()] = {"merkle_root": a[1].hex(), "rejections_root": a[2].hex(),
                                      "mode": ("immediate", "batched")[a[3]], "event_count": a[4],
                                      "bucket_min": a[5], "bucket_max": a[6]}
            logs = _anchor_logs(self.http, url, self.pins.contract_address, election_hash, block)
            result.update(anchors=anchors, anchor_logs=logs, anchors_reason=None)
        except ChainReadUnavailable:
            pass
        return result

    def assess(self, checkpoint_event: dict, events: list[dict]) -> ChainVisibility:
        """Look only at the first complete answer, falling back on lag or RPC errors.

        If none reaches the required height, return the best observed height from
        one correct chain. Never combine an identity from one node with a height
        from another. No answer/wrong chain is unavailable, not an election FAIL.
        The caller must already have fetched all pages and bound them to (e,k).
        """
        checkpoint_block = reported_anchor_block(checkpoint_event)
        if (not isinstance(checkpoint_event, Mapping) or checkpoint_event.get("type") != "CHECKPOINT_COMMITTED"
                or not checkpoint_event.get("event_id") or not isinstance(events, list)
                or not any(e == checkpoint_event for e in events)):
            checkpoint_block = None
        heights = [reported_anchor_block(e) for e in events] if isinstance(events, list) else []
        all_known = bool(heights) and all(b is not None for b in heights)
        max_known = max((b for b in heights if b is not None), default=0)
        last = None
        mismatch = False
        for url in self.pins.urls:
            chain_id = _height(self.http, url, "eth_chainId")
            if chain_id is None:
                continue
            if chain_id != _chain_id(self.pins.chain_id):
                mismatch = True
                continue
            head = _height(self.http, url, "eth_blockNumber")
            if head is None:
                continue
            last = head if last is None else max(last, head)
            if checkpoint_block is not None and head >= max(checkpoint_block, max_known) + self.pins.depth:
                break
        confirmed = last is not None and checkpoint_block is not None and last >= checkpoint_block + self.pins.depth
        anchors = confirmed and all_known and last >= max_known + self.pins.depth
        reason = None if anchors else ("BATCH_NOT_ANCHORED" if confirmed else "NODE_UNAVAILABLE")
        return ChainVisibility(confirmed, anchors, reason, last, last is not None, mismatch)
