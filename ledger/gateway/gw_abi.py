"""ABI encoding, function table and revert names of PureVotingLedger (chain-interface.md v1.1). Standard library only.

Selectors are computed with the keccak-256 of ethsign.py, so the Gateway needs no compiler at run time.
ledger/tests/check_interface.py checks the same signatures against the contract; ledger/tests/test_gateway_e2e.py
exercises them on a running network.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import ethsign  # noqa: E402

# name -> (signature types, output types)
FUNCS = {
    # Gateway writes
    "createElection": (["bytes32", "bytes32", "uint64", "uint64", "uint32", "uint32", "bytes32[]", "uint8", "bytes32", "uint32", "uint64"], []),
    "freezeRoster": (["bytes32", "bytes32", "uint64"], []),
    "openElection": (["bytes32", "uint64"], []),
    "closeElection": (["bytes32", "uint64", "bool", "bytes32", "uint64", "uint64", "uint64"], []),
    "setTokenSetRoot": (["bytes32", "bytes32", "bytes32", "uint64"], []),
    "commitCheckpoint": (["bytes32", "uint64", "uint8", "bytes32", "uint64"], []),
    "commitAnchor": (["bytes32", "bytes32", "bytes32", "bytes32", "uint8", "uint32", "uint64", "uint64"], []),
    "publishTally": (["bytes32", "bytes32", "uint64", "uint64"], []),
    # reads
    "electionState": (["bytes32"], ["uint8", "uint64", "uint64", "uint32", "bytes32", "bytes32", "bytes32", "bytes32", "uint64", "uint64", "bytes32"]),
    "electionGovernance": (["bytes32"], ["bytes32[]", "uint8", "bytes32", "uint32", "uint32"]),
    "electionStateAtCheckpoint": (["bytes32", "uint64"], ["uint8", "uint64", "bytes32", "bytes32", "bytes32", "uint64"]),
    "anchorCount": (["bytes32"], ["uint256"]),
    "anchorAt": (["bytes32", "uint256"], ["bytes32", "bytes32", "bytes32", "uint8", "uint32", "uint64", "uint64"]),
    "orgCount": ([], ["uint256"]),
    "orgAt": (["uint256"], ["bytes32"]),
    "orgSigner": (["bytes32"], ["address", "address"]),
    "operatorOrg": ([], ["bytes32", "bool"]),
    "ruleEngineSigner": ([], ["address", "bytes32", "bool"]),
    "gatewayAccount": ([], ["address", "bytes32", "bool"]),
}

ERRORS = [
    "NotGateway", "NotRegisteredOrgAccount", "ZeroValue", "OrgAlreadyRegistered", "AccountAlreadyRegistered",
    "SignerAlreadyRegistered", "WrongPendingValue", "ElectionExists", "ElectionNotFound", "OrgNotRegistered",
    "OperatorOrgMismatch", "DuplicateOrg", "TooFewOrganizations", "InvalidThreshold", "InvalidConfigHash",
    "InvalidWindow", "EmptyRoster", "InvalidStateTransition", "TimeNotReached", "SeqNotIncreasing",
    "CheckpointNotIncreasing", "InvalidReason", "EarlyCloseNotImplemented", "InvalidMode", "InvalidBuckets",
    "AnchorConflict", "AnchorBeyondWindowEnd", "IndexOutOfRange", "InvalidFinalCheckpoint",
    "CloseFinalAlreadyCommitted",
]


def idh(text):
    """idh(id) = sha256(utf8(id)) as 0x-hex (the bytes32 ID of hashing.md)."""
    return "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def hex32(h64):
    """64-hex SHA-256 value from the API as the 0x-hex bytes32 of the contract."""
    return "0x" + h64.lower()


def unhex32(b32):
    return b32[2:] if b32.startswith("0x") else b32


def signature(name):
    return name + "(" + ",".join(FUNCS[name][0]) + ")"


def selector(name):
    return ethsign.keccak256(signature(name).encode())[:4].hex()


ERROR_BY_SELECTOR = {ethsign.keccak256((n + "()").encode())[:4].hex(): n for n in ERRORS}


def _word(t, v):
    if t == "bytes32":
        return bytes.fromhex(v[2:] if isinstance(v, str) else v.hex()).rjust(32, b"\0")
    if t == "address":
        return bytes.fromhex(v[2:]).rjust(32, b"\0")
    if t == "bool":
        return int(bool(v)).to_bytes(32, "big")
    if t.startswith("uint"):
        return int(v).to_bytes(32, "big")
    raise ValueError(t)


def encode_args(types, values):
    if len(types) != len(values):
        raise ValueError("argument count")
    head, tail = b"", b""
    base = 32 * len(types)
    for t, v in zip(types, values):
        if t == "bytes32[]":
            head += (base + len(tail)).to_bytes(32, "big")
            tail += len(v).to_bytes(32, "big") + b"".join(_word("bytes32", x) for x in v)
        else:
            head += _word(t, v)
    return head + tail


def calldata(name, args):
    return bytes.fromhex(selector(name)) + encode_args(FUNCS[name][0], args)


def decode_result(types, data_hex):
    data = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
    out = []
    for i, t in enumerate(types):
        w = data[32 * i: 32 * i + 32]
        if t == "bytes32[]":
            off = int.from_bytes(w, "big")
            n = int.from_bytes(data[off: off + 32], "big")
            out.append(["0x" + data[off + 32 + 32 * k: off + 64 + 32 * k].hex() for k in range(n)])
        elif t == "bytes32":
            out.append("0x" + w.hex())
        elif t == "address":
            out.append("0x" + w[-20:].hex())
        elif t == "bool":
            out.append(int.from_bytes(w, "big") != 0)
        else:
            out.append(int.from_bytes(w, "big"))
    return out


# topic0 of the events the Gateway reads
def topic(event_signature):
    return "0x" + ethsign.keccak256(event_signature.encode()).hex()


TOPIC_STATE_CHANGED = topic("StateChanged(bytes32,uint8,uint64)")
TOPIC_ANCHOR_COMMITTED = topic("AnchorCommitted(bytes32,bytes32,bytes32,bytes32,uint8,uint32)")
TOPIC_ORG_REGISTERED = topic("OrgRegistered(bytes32,address,address)")
