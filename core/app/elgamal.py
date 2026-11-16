"""Exponential ElGamal over the Ed25519 prime-order group, exactly as contracts/ballot-elgamal.md (decisions D-30).

A ballot is one ciphertext per candidate (candidate_id order) encrypting 0 or 1, a proof for each that it is 0 or 1
and a proof that they add up to 1. Each organization (trustee) keeps its own secret x; the election key is the sum of
their public keys, and only per-candidate aggregates are ever decrypted, each share with a Chaum-Pedersen proof.
Not audited cryptography (threat-model). Every function here must reproduce tests/vectors/elgamal-vectors.json.
"""
import hashlib
import os
from typing import Callable

from nacl import bindings as nb

L = 2 ** 252 + 27742317777372353535851937790883648493   # order of the Ed25519 prime-order subgroup
IDENTITY = bytes([1]) + bytes(31)
MAGIC = b"PVE1"
DOMAIN = b"PureVoting/elgamal-ed25519/v1"
SCALAR, POINT = 32, 32
ENTRY = 2 * POINT + 4 * SCALAR      # A, B, c0, c1, z0, z1
MAX_CANDIDATES = 15                 # 4 + 2 + 15 * 192 + 64 = 2950 bytes, under the 3072-byte sealed_ballot limit

Rng = Callable[[], int]


class InvalidBallot(ValueError):
    pass


# ---- group ----

def enc_scalar(s: int) -> bytes:
    return (s % L).to_bytes(SCALAR, "little")


def dec_scalar(raw: bytes) -> int:
    s = int.from_bytes(raw, "little")
    if len(raw) != SCALAR or s >= L:
        raise InvalidBallot("non-canonical scalar")
    return s


def rand_scalar() -> int:
    while True:
        s = int.from_bytes(os.urandom(64), "little") % L
        if s:
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

def trustee_keygen(org_id: str, rng: Rng = rand_scalar) -> tuple[int, dict]:
    """(secret x, public record {org_id, public_key, proof}); the proof of knowledge of x stops a rogue-key sum."""
    x = rng()
    X = base(x)
    w = rng()
    c = hs("key", org_id.encode("utf-8"), X, base(w))
    return x, {"org_id": org_id, "public_key": X.hex(), "proof": {"c": enc_scalar(c).hex(), "z": enc_scalar(w + c * x).hex()}}


def verify_trustee_key(record: dict) -> bool:
    try:
        X = bytes.fromhex(record["public_key"])
        c, z = dec_scalar(bytes.fromhex(record["proof"]["c"])), dec_scalar(bytes.fromhex(record["proof"]["z"]))
        org = record["org_id"].encode("utf-8")
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    if not valid_point(X):
        return False
    return c == hs("key", org, X, sub(base(z), mul(c, X)))


def joint_key(records: list[dict]) -> bytes:
    """Y = sum of the trustees' public keys, in org_id order."""
    return total([bytes.fromhex(r["public_key"]) for r in sorted(records, key=lambda r: r["org_id"])])


# ---- ballots ----

def _ctx(election_id: str, nullifier_hash: str, Y: bytes) -> tuple[bytes, ...]:
    return election_id.encode("utf-8"), bytes.fromhex(nullifier_hash), Y


def encrypt_ballot(Y: bytes, election_id: str, nullifier_hash: str, n: int, choice: int,
                   rng: Rng = rand_scalar) -> bytes:
    """The sealed_ballot bytes for candidate index `choice` of n (candidate_id order)."""
    if not 1 <= n <= MAX_CANDIDATES or not 0 <= choice < n:
        raise ValueError("candidate index out of range")
    ctx = _ctx(election_id, nullifier_hash, Y)
    out = [MAGIC, u16(n)]
    As, Bs, R = [], [], 0
    for j in range(n):
        m, r = int(j == choice), rng()
        A, B = base(r), add(mul(r, Y), G) if m else mul(r, Y)
        s = 1 - m
        cs, zs, w = rng(), rng(), rng()
        T, U = {m: base(w), s: sub(base(zs), mul(cs, A))}, {m: mul(w, Y), s: sub(mul(zs, Y), mul(cs, sub(B, G) if s else B))}
        c = hs("bit", *ctx, u16(j), A, B, T[0], U[0], T[1], U[1])
        cc = {s: cs, m: (c - cs) % L}
        zz = {s: zs, m: (w + cc[m] * r) % L}
        out += [A, B, enc_scalar(cc[0]), enc_scalar(cc[1]), enc_scalar(zz[0]), enc_scalar(zz[1])]
        As.append(A), Bs.append(B)
        R += r
    w = rng()
    c = hs("sum", *ctx, total(As), total(Bs), base(w), mul(w, Y))
    out += [enc_scalar(c), enc_scalar(w + c * R)]
    return b"".join(out)


def parse_ballot(raw: bytes, n: int) -> list[tuple]:
    """[(A, B, c0, c1, z0, z1) per candidate] + [(c, z)] as the last item; InvalidBallot on any format error."""
    if len(raw) != 6 + n * ENTRY + 2 * SCALAR or raw[:4] != MAGIC or int.from_bytes(raw[4:6], "big") != n:
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
        *entries, (c, z) = parse_ballot(raw, n)
    except InvalidBallot:
        return None
    ctx = _ctx(election_id, nullifier_hash, Y)
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

def decryption_share(x: int, org_id: str, election_id: str, agg: list[tuple[bytes, bytes]],
                     rng: Rng = rand_scalar) -> dict:
    """D_j = x * A_j for every aggregate, each with a proof that log_G X = log_A D."""
    X = base(x)
    shares = []
    for j, (A, _) in enumerate(agg):
        D, w = mul(x, A), rng()
        c = hs("share", election_id.encode("utf-8"), org_id.encode("utf-8"), u16(j), X, A, D, base(w), mul(w, A))
        shares.append({"d": D.hex(), "c": enc_scalar(c).hex(), "z": enc_scalar(w + c * x).hex()})
    return {"org_id": org_id, "shares": shares}


def verify_share(share: dict, X: bytes, election_id: str, agg: list[tuple[bytes, bytes]]) -> bool:
    try:
        items = share["shares"]
        if len(items) != len(agg):
            return False
        for j, ((A, _), item) in enumerate(zip(agg, items)):
            D = bytes.fromhex(item["d"])
            c, z = dec_scalar(bytes.fromhex(item["c"])), dec_scalar(bytes.fromhex(item["z"]))
            # A is Core's own sum and may be the identity (two ballots with r and L - r); then D must be too
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
        M = sub(B, total([bytes.fromhex(s["shares"][j]["d"]) for s in shares]))
        acc = IDENTITY
        for m in range(max_votes + 1):
            if acc == M:
                votes.append(m)
                break
            acc = add(acc, G)
        else:
            raise ValueError("aggregate does not decrypt to a count")
    return votes
