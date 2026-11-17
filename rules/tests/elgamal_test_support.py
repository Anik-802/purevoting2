"""Deterministic test-only ElGamal producers, ported from the documented formulas.

Production rules import no Core code and hold no trustee secrets. These fixture
builders are checked against the shared vectors before adversarial use.
"""
from __future__ import annotations

import random
from typing import Callable

from purevoting_rules.elgamal import (L, MAGIC, MAX_CANDIDATES, G, base, mul, add, sub,
                                     hs, u16, total, enc_scalar, _ctx)

Rng = Callable[[], int]
_rng = random.Random(1729)

def rand_scalar() -> int:
    return _rng.randrange(1, L)

def trustee_keygen(org_id: str, rng: Rng = rand_scalar) -> tuple[int, dict]:
    """(secret x, public record {org_id, public_key, proof}); the proof of knowledge of x stops a rogue-key sum."""
    x = rng()
    X = base(x)
    w = rng()
    c = hs("key", org_id.encode("utf-8"), X, base(w))
    return x, {"org_id": org_id, "public_key": X.hex(), "proof": {"c": enc_scalar(c).hex(), "z": enc_scalar(w + c * x).hex()}}

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
