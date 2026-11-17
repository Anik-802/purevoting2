"""Independent R5 verifier for contracts/ballot-elgamal.md, specification v1.2.2.

Verification formulas and zero-scalar/identity guards are ported from the
project's Core implementation. This module neither imports Core nor contains
trustee secrets, ballot generation or decryption-share generation. External
points/scalars use the contract's canonical lowercase encodings.
"""
from __future__ import annotations

import hashlib
import re

from nacl import bindings as nb

L = 2 ** 252 + 27742317777372353535851937790883648493   # order of the Ed25519 prime-order subgroup
IDENTITY = bytes([1]) + bytes(31)
MAGIC = b"PVE1"
DOMAIN = b"PureVoting/elgamal-ed25519/v1"
SCALAR, POINT = 32, 32
ENTRY = 2 * POINT + 4 * SCALAR      # A, B, c0, c1, z0, z1
_HEX32 = re.compile(r"[0-9a-f]{64}\Z")
MAX_CANDIDATES = 15                 # 4 + 2 + 15 * 192 + 64 = 2950 bytes, under the 3072-byte sealed_ballot limit


class InvalidBallot(ValueError):
    pass


# ---- group ----

def enc_scalar(s: int) -> bytes:
    return (s % L).to_bytes(SCALAR, "little")


def hex32(value: str) -> bytes:
    if not isinstance(value, str) or not _HEX32.fullmatch(value):
        raise InvalidBallot("non-canonical point/scalar encoding")
    return bytes.fromhex(value)


def dec_scalar(raw: bytes) -> int:
    s = int.from_bytes(raw, "little")
    if len(raw) != SCALAR or s >= L:
        raise InvalidBallot("non-canonical scalar")
    return s


def base(s: int) -> bytes:
    s %= L
    return IDENTITY if s == 0 else nb.crypto_scalarmult_ed25519_base_noclamp(enc_scalar(s))


def mul(s: int, p: bytes) -> bytes:
    """s * p for a point of the prime-order subgroup (or the identity)."""
    s %= L
    if s == 0 or p == IDENTITY:
        return IDENTITY
    return nb.crypto_scalarmult_ed25519_noclamp(enc_scalar(s), p)


def add(p: bytes, q: bytes) -> bytes:
    return nb.crypto_core_ed25519_add(p, q)


def sub(p: bytes, q: bytes) -> bytes:
    return nb.crypto_core_ed25519_sub(p, q)


def valid_point(p: bytes) -> bool:
    """Canonical, in the prime-order subgroup and not the identity (libsodium crypto_core_ed25519_is_valid_point)."""
    return len(p) == POINT and bool(nb.crypto_core_ed25519_is_valid_point(p))


G = base(1)


def hs(label: str, *parts: bytes) -> int:
    """Fiat-Shamir challenge: SHA-512 over length-prefixed parts, read little-endian, reduced mod L."""
    h = hashlib.sha512()
    for part in (DOMAIN, label.encode("utf-8"), *parts):
        h.update(len(part).to_bytes(2, "big"))
        h.update(part)
    return int.from_bytes(h.digest(), "little") % L


def u16(n: int) -> bytes:
    return n.to_bytes(2, "big")


def total(points: list[bytes]) -> bytes:
    acc = IDENTITY
    for p in points:
        acc = add(acc, p)
    return acc


# ---- trustee keys ----

def verify_trustee_key(record: dict) -> bool:
    try:
        X = hex32(record["public_key"])
        c, z = dec_scalar(hex32(record["proof"]["c"])), dec_scalar(hex32(record["proof"]["z"]))
        org = record["org_id"].encode("utf-8")
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    if not valid_point(X):
        return False
    return c == hs("key", org, X, sub(base(z), mul(c, X)))


def joint_key(records: list[dict]) -> bytes:
    """Y = sum of the trustees' public keys, in org_id order."""
    return total([hex32(r["public_key"]) for r in sorted(records, key=lambda r: r["org_id"])])


# ---- ballots ----

def _ctx(election_id: str, nullifier_hash: str, Y: bytes) -> tuple[bytes, ...]:
    return election_id.encode("utf-8"), hex32(nullifier_hash), Y


def parse_ballot(raw: bytes, n: int) -> list[tuple]:
    """[(A, B, c0, c1, z0, z1) per candidate] + [(c, z)] as the last item; InvalidBallot on any format error."""
    if type(n) is not int or not 1 <= n <= MAX_CANDIDATES:
        raise InvalidBallot("candidate count")
    if not isinstance(raw, bytes) or len(raw) != 6 + n * ENTRY + 2 * SCALAR or raw[:4] != MAGIC or int.from_bytes(raw[4:6], "big") != n:
        raise InvalidBallot("format")
    entries, at = [], 6
    for _ in range(n):
        A, B = raw[at:at + 32], raw[at + 32:at + 64]
        if not (valid_point(A) and valid_point(B)):
            raise InvalidBallot("point")
        entries.append((A, B, *(dec_scalar(raw[at + 64 + 32 * i:at + 96 + 32 * i]) for i in range(4))))
        at += ENTRY
    entries.append((dec_scalar(raw[at:at + 32]), dec_scalar(raw[at + 32:at + 64])))
    return entries


def verify_ballot(raw: bytes, Y: bytes, election_id: str, nullifier_hash: str, n: int) -> list[tuple[bytes, bytes]] | None:
    """The per-candidate ciphertexts [(A, B)] of a valid ballot, else None."""
    try:
        if not valid_point(Y):
            return None
        *entries, (c, z) = parse_ballot(raw, n)
        ctx = _ctx(election_id, nullifier_hash, Y)
    except (InvalidBallot, TypeError, AttributeError):
        return None
    for j, (A, B, c0, c1, z0, z1) in enumerate(entries):
        T0, U0 = sub(base(z0), mul(c0, A)), sub(mul(z0, Y), mul(c0, B))
        T1, U1 = sub(base(z1), mul(c1, A)), sub(mul(z1, Y), mul(c1, sub(B, G)))
        if (c0 + c1) % L != hs("bit", *ctx, u16(j), A, B, T0, U0, T1, U1):
            return None
    sA, sB = total([e[0] for e in entries]), total([e[1] for e in entries])
    if c != hs("sum", *ctx, sA, sB, sub(base(z), mul(c, sA)), sub(mul(z, Y), mul(c, sub(sB, G)))):
        return None
    return [(e[0], e[1]) for e in entries]


def aggregate(ciphertexts: list[list[tuple[bytes, bytes]]], n: int) -> list[tuple[bytes, bytes]]:
    """Per-candidate sums of valid ballots' ciphertexts."""
    return [(total([b[j][0] for b in ciphertexts]), total([b[j][1] for b in ciphertexts])) for j in range(n)]


# ---- decryption shares ----

def verify_share(share: dict, X: bytes, election_id: str, agg: list[tuple[bytes, bytes]]) -> bool:
    try:
        if not valid_point(X):
            return False
        items = share["shares"]
        if not isinstance(items, list):
            return False
        if len(items) != len(agg):
            return False
        for j, ((A, _), item) in enumerate(zip(agg, items)):
            D = hex32(item["d"])
            c, z = dec_scalar(hex32(item["c"])), dec_scalar(hex32(item["z"]))
            # A is our independently recomputed sum and may be the identity (two ballots with r and L - r); then D must be too
            if not (D == IDENTITY if A == IDENTITY else valid_point(D) and valid_point(A)):
                return False
            T, U = sub(base(z), mul(c, X)), sub(mul(z, A), mul(c, D))
            if c != hs("share", election_id.encode("utf-8"), share["org_id"].encode("utf-8"), u16(j), X, A, D, T, U):
                return False
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    return True


def combine(agg: list[tuple[bytes, bytes]], shares: list[dict], max_votes: int) -> list[int]:
    """Votes per candidate from verified shares: M_j = B_j - sum D_ij = m_j * G, m_j found by search up to max_votes."""
    votes = []
    for j, (_, B) in enumerate(agg):
        M = sub(B, total([hex32(s["shares"][j]["d"]) for s in shares]))
        acc = IDENTITY
        for m in range(max_votes + 1):
            if acc == M:
                votes.append(m)
                break
            acc = add(acc, G)
        else:
            raise ValueError("aggregate does not decrypt to a count")
    return votes
