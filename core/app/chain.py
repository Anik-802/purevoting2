"""Direct reads from the pinned validator node over JSON-RPC (chain-interface.md), without the Gateway.

Only what the creation check needs: the latest block number (a block counts once latest ≥ block + CONFIRMATION_DEPTH,
chain-interface.md "Confirmations") and the getters electionState / electionGovernance. The pinned URLs are tried in
order (PINNED_NODE_URL is comma-separated: the observer's node first, the commission's second, D-03), and one reading
comes entirely from one node at one block: mixing a confirmed node's block number with a lagging node's state could
turn a delay into a final MISMATCH.
"""
import httpx

from app.config import settings
from app.hashing import idh

TIMEOUT = 5.0

# keccak256(signature)[:4], computed with two independent keccak implementations (week-3 review); to be checked against
# Harits's deployed contract when it joins the stack
SEL_ELECTION_STATE = "19dfd414"        # electionState(bytes32)
SEL_ELECTION_GOVERNANCE = "cbce74a6"   # electionGovernance(bytes32)

U8, U32, U64 = 2**8, 2**32, 2**64
MAX_ORGS = 1000


class NodeUnavailable(Exception):
    """No pinned node gave a complete, well-formed answer (network, HTTP or JSON-RPC error, malformed result)."""


class _Bad(Exception):
    """One node's answer is unusable; the reader moves on to the next URL."""


def _words(data) -> list[int]:
    try:
        raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    except (AttributeError, TypeError, ValueError):
        raise _Bad("eth_call result is not hex") from None
    if len(raw) % 32:
        raise _Bad("eth_call result is not whole words")
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def _b32(w: int) -> str:
    return w.to_bytes(32, "big").hex()


def _fits(*pairs) -> None:
    if any(v >= bound for v, bound in pairs):
        raise _Bad("value out of range for its ABI type")


def decode_state(w: list[int]) -> dict:
    if len(w) != 11:
        raise _Bad("electionState: expected 11 words")
    _fits((w[0], U8), (w[1], U64), (w[2], U64), (w[3], U32), (w[8], U64), (w[9], U64))
    return {"status": w[0], "window_start": w[1], "window_end": w[2], "time_bucket_seconds": w[3],
            "config_hash": _b32(w[4]), "roster_hash": _b32(w[5]), "token_set_root": _b32(w[6]),
            "result_hash": _b32(w[7]), "last_checkpoint": w[8], "last_core_seq": w[9],
            "operator_org_id": _b32(w[10])}


def decode_governance(w: list[int]) -> dict:
    # (bytes32[] orgIds, uint8 approvalThreshold, bytes32 operatorOrgId, uint32 proposalTtlHours,
    #  uint32 checkpointIntervalSeconds): head = offset of orgIds + 4 static words (0xa0 bytes), then length + elements
    if len(w) < 6 or w[0] < 0xa0 or w[0] % 32:
        raise _Bad("electionGovernance: bad head")
    start = w[0] // 32
    if start >= len(w) or w[start] > MAX_ORGS or start + 1 + w[start] != len(w):
        raise _Bad("electionGovernance: bad orgIds array")
    _fits((w[1], U8), (w[3], U32), (w[4], U32))
    n = w[start]
    return {"org_ids": [_b32(x) for x in w[start + 1:start + 1 + n]], "approval_threshold": w[1],
            "operator_org_id": _b32(w[2]), "proposal_ttl_hours": w[3], "checkpoint_interval_seconds": w[4]}


class NodeReader:
    def __init__(self, urls: list[str] | None = None, contract: str | None = None, http: httpx.Client | None = None):
        self.urls = urls if urls is not None else settings.pinned_node_urls
        self.contract = contract or settings.contract_address
        self.http = http or httpx.Client(timeout=TIMEOUT)

    def _rpc(self, url: str, method: str, params: list):
        try:
            r = self.http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except httpx.HTTPError as e:
            raise _Bad(type(e).__name__) from None
        if r.status_code != 200:
            raise _Bad(f"HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise _Bad("response is not JSON") from None
        if not isinstance(body, dict) or "error" in body or body.get("result") is None:
            raise _Bad("JSON-RPC error")
        return body["result"]

    def _call(self, url: str, selector: str, election_id: str, block: int) -> list[int]:
        data = "0x" + selector + idh(election_id).hex()
        return _words(self._rpc(url, "eth_call", [{"to": self.contract, "data": data}, hex(block)]))

    def read_election(self, election_id: str, created_block: int, depth: int) -> tuple[int, dict | None, dict | None]:
        """(latest, state, governance) from the first node that answers completely. state / governance are None when
        that node's latest block is below created_block + depth (not confirmed there yet); both getters are read at
        that node's latest block."""
        last = "no pinned node URL configured"
        behind = None                 # a node that answered but has not confirmed the block: try the next one
        for url in self.urls:
            try:
                latest_hex = self._rpc(url, "eth_blockNumber", [])
                try:
                    latest = int(latest_hex, 16)
                except (TypeError, ValueError):
                    raise _Bad("block number is not hex") from None
                if latest < created_block + depth:
                    behind = behind or (latest, None, None)
                    continue
                state = decode_state(self._call(url, SEL_ELECTION_STATE, election_id, latest))
                gov = decode_governance(self._call(url, SEL_ELECTION_GOVERNANCE, election_id, latest))
                return latest, state, gov
            except _Bad as e:
                last = f"{url}: {e}"
        if behind:
            return behind
        raise NodeUnavailable(last)


_reader: NodeReader | None = None


def get_reader() -> NodeReader:
    global _reader
    if _reader is None:
        _reader = NodeReader()
    return _reader


def set_reader(reader: NodeReader | None) -> None:
    global _reader
    _reader = reader
