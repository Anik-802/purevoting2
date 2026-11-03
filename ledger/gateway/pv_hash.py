"""Hashing, Merkle trees and signature digests of PureVoting 2.0 (contracts/hashing.md, normative).

Standard library only. Every function follows hashing.md exactly; tests/vectors/hash-vectors.json is the check
(see ledger/gateway/tests/test_hash_vectors.py).

canonical() implements RFC 8785 (JCS) for the value types hashing.md allows: objects, arrays, strings, integers,
booleans and null. Floating-point numbers are rejected, as hashing.md forbids them in hashed objects.
"""
import hashlib
import json

ACTION_TYPE = {"ROSTER_AMEND": 1, "TOKEN_REISSUE": 2, "WINDOW_CHANGE": 3, "ELECTION_CLOSE_EARLY": 4, "ELECTION_VOID": 5}


# ---- primitives ----

def H(data):
    """SHA-256 as 64 lowercase hex chars. str is hashed as UTF-8."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical(obj):
    """RFC 8785 canonical JSON text (integers only, no floats)."""
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        raise ValueError("floating-point numbers must not appear in hashed objects (hashing.md)")
    if isinstance(obj, str):
        # JCS string escaping: \" \\ \b \f \n \r \t, other C0 controls as lowercase \u00xx, everything else literal.
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(canonical(x) for x in obj) + "]"
    if isinstance(obj, dict):
        # JCS sorts member names by UTF-16 code units.
        keys = sorted(obj, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(obj[k]) for k in keys) + "}"
    raise TypeError(f"cannot canonicalize {type(obj).__name__}")


def Hc(obj):
    """H(canonical(obj))."""
    return H(canonical(obj).encode("utf-8"))


def merkle_root(leaves):
    """Leaves are 64-hex strings in the given order. Adjacent pairs are hashed left to right with H(l + r) over the
    ASCII hex text; an odd last node is carried up unchanged. One leaf is its own root; an empty list is H("")."""
    level = list(leaves)
    if not level:
        return H("")
    while len(level) > 1:
        nxt = [H(level[i] + level[i + 1]) for i in range(0, len(level) - 1, 2)]
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0]


def set_root(values):
    """merkle_root of the sorted, de-duplicated values."""
    return merkle_root(sorted(set(values)))


# ---- signed digest building blocks (fixed 32-byte parts) ----

def tag(s):
    b = s.encode("ascii")
    if len(b) > 32:
        raise ValueError("tag longer than 32 bytes")
    return b + b"\x00" * (32 - len(b))


def idh(x):
    """sha256(utf8(id)) as 32 raw bytes; the same value is the bytes32 ID used on chain."""
    return hashlib.sha256(x.encode("utf-8")).digest()


def h32(hex64):
    """The 32 raw bytes of a 64-hex SHA-256 value."""
    raw = bytes.fromhex(hex64)
    if len(raw) != 32:
        raise ValueError("expected a 64-hex SHA-256 value")
    return raw


def u(n):
    """Unsigned integer as 32-byte big-endian."""
    return n.to_bytes(32, "big")


# ---- signed digests (hex output; the 32 raw bytes are the message that gets signed) ----

def proposal_digest(proposal_id, action_id, election_id, action_type, target_hash, org_id, decision):
    """Request digest (decision 0, requesting org) or approval digest (1 APPROVE / 2 REJECT, deciding org)."""
    return hashlib.sha256(
        tag("PV2-PROPOSAL-v1") + idh(proposal_id) + idh(action_id) + idh(election_id) + u(ACTION_TYPE[action_type])
        + h32(target_hash) + idh(org_id) + u(decision)).hexdigest()


def finding_digest(finding_id, election_id, rule, result, finding_checkpoint, finding_hash):
    """Finding record digest, signed by the Rule Engine key. rule R1..R5 = 1..5; result PASS 0, FAIL 1, INCONCLUSIVE 2."""
    return hashlib.sha256(
        tag("PV2-FINDING-v1") + idh(finding_id) + idh(election_id) + u(rule) + u(result) + u(finding_checkpoint)
        + h32(finding_hash)).hexdigest()


def review_digest(finding_id, election_id, finding_hash, org_id, decision, note_hash):
    """Review digest, signed by each reviewing org. decision DISMISSED 1, ACCEPTED_RISK 2, CONFIRMED_ISSUE 3."""
    return hashlib.sha256(
        tag("PV2-REVIEW-v1") + idh(finding_id) + idh(election_id) + h32(finding_hash) + idh(org_id) + u(decision)
        + h32(note_hash)).hexdigest()
