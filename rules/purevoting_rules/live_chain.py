"""Pinned EVM RPC and v1.1 confirmation-depth readiness for the polling worker.

A deep Gateway-reported block is scheduling evidence, not proof that a
transaction exists, an event was included, or an anchor/root is correct. R4/R5
must retain their independent registry/state/anchor/proof coverage limits.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_ANCHOR_ID = re.compile(r"anc_[0-9A-HJKMNP-TV-Z]{26}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TX = re.compile(r"0x[0-9a-fA-F]{64}")
_QUANTITY = re.compile(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]{0,63})")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,77})")


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
