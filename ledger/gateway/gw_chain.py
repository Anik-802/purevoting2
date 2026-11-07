"""JSON-RPC client and contract calls of the Ledger Gateway (standard library only).

Reads of chain-derived data use the confirmed block (latest - D, chain-interface.md "Confirmations"). Writes are
legacy EIP-155 transactions signed with the Gateway account key (ethsign.py) and kept as raw bytes, so a pending
submission can only ever be re-broadcast as the same transaction.
"""
import json
import urllib.request

import gw_abi
import ethsign


class ChainError(Exception):
    """Node unreachable or malformed answer."""


class Revert(Exception):
    def __init__(self, name, data=""):
        super().__init__(name)
        self.name = name
        self.data = data


class ChainClient:
    def __init__(self, url, chain_id, contract, key_hex, depth, gas=3_000_000, timeout=5):
        self.url, self.chain_id, self.contract = url, int(chain_id), contract.lower()
        self.depth, self.gas, self.timeout = int(depth), gas, timeout
        self.priv = int(key_hex, 16) if key_hex else None
        self.address = ethsign.address(self.priv) if self.priv else None

    # ---- raw JSON-RPC ----
    def raw(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except Exception as e:  # network error, timeout, bad JSON
            raise ChainError(f"{method}: {e}") from e

    def rpc(self, method, params=None):
        out = self.raw(method, params)
        if "error" in out:
            raise ChainError(f"{method}: {out['error']}")
        return out["result"]

    # ---- blocks ----
    def latest(self):
        return int(self.rpc("eth_blockNumber"), 16)

    def confirmed_block(self):
        return max(self.latest() - self.depth, 0)

    def block_time(self, number="latest"):
        tag = number if isinstance(number, str) else hex(number)
        blk = self.rpc("eth_getBlockByNumber", [tag, False])
        return int(blk["timestamp"], 16) if blk else None

    # ---- calls ----
    def call(self, name, *args, block="latest"):
        """eth_call of a contract function at 'latest', 'confirmed' or a block number. Raises Revert(name)."""
        tag = hex(self.confirmed_block()) if block == "confirmed" else (block if isinstance(block, str) else hex(block))
        data = "0x" + gw_abi.calldata(name, args).hex()
        frm = self.address or "0x" + "00" * 20
        out = self.raw("eth_call", [{"from": frm, "to": self.contract, "data": data}, tag])
        if "error" in out:
            d = out["error"].get("data") or ""
            if isinstance(d, str) and d.startswith("0x") and len(d) >= 10:
                raise Revert(gw_abi.ERROR_BY_SELECTOR.get(d[2:10], "UnknownRevert"), d)
            raise Revert("UnknownRevert", str(out["error"]))
        outs = gw_abi.FUNCS[name][1]
        res = gw_abi.decode_result(outs, out["result"])
        return res[0] if len(res) == 1 else res

    # ---- transactions ----
    def next_nonce(self):
        return int(self.rpc("eth_getTransactionCount", [self.address, "pending"]), 16)

    def build_tx(self, name, args, nonce):
        """Returns (raw tx bytes, tx hash 0x-hex). Gas price 0 (PureChain is free-gas)."""
        raw = ethsign.sign_legacy_tx(self.priv, self.chain_id, nonce, 0, self.gas, self.contract, 0,
                                     gw_abi.calldata(name, args))
        return raw, "0x" + ethsign.keccak256(raw).hex()

    def send_raw(self, raw):
        """Broadcast (or re-broadcast) a signed transaction. 'already known' and 'nonce too low' answers for a
        transaction that is already in the pool or mined are not errors."""
        out = self.raw("eth_sendRawTransaction", ["0x" + bytes(raw).hex()])
        if "error" in out:
            msg = str(out["error"].get("message", "")).lower()
            if "already known" in msg or "known transaction" in msg or "nonce too low" in msg:
                return None
            raise ChainError("eth_sendRawTransaction: " + str(out["error"]))
        return out["result"]

    def receipt(self, tx_hash):
        try:
            return self.rpc("eth_getTransactionReceipt", [tx_hash])
        except ChainError as e:
            if "indexing is in progress" in str(e):
                return None
            raise

    def is_confirmed(self, block_number):
        return self.latest() >= block_number + self.depth

    def logs(self, topic0, election_b32=None, from_block=0, to_block="latest"):
        topics = [topic0] + ([election_b32] if election_b32 else [])
        tb = to_block if isinstance(to_block, str) else hex(to_block)
        return self.rpc("eth_getLogs", [{"address": self.contract, "fromBlock": hex(from_block), "toBlock": tb,
                                         "topics": topics}])

    def peer_count(self):
        try:
            return int(self.rpc("net_peerCount"), 16)
        except ChainError:
            return None
