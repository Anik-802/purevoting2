"""D-30 ElGamal ballots: the shared vectors (tests/vectors/elgamal-vectors.json) and forged ballots."""
import json
import pathlib

import pytest

from app import elgamal as eg

VEC = json.loads((pathlib.Path(__file__).resolve().parents[2] / "tests" / "vectors" / "elgamal-vectors.json").read_text())


def _rng(hexes):
    it = iter(hexes)
    return lambda: int.from_bytes(bytes.fromhex(next(it)), "little")


def test_vectors_reproduce():
    for t in VEC["trustees"]:
        x, record = eg.trustee_keygen(t["org_id"], _rng(t["rng"]))
        assert record == t["record"] and eg.enc_scalar(x).hex() == t["secret"] and eg.verify_trustee_key(record)
    Y = bytes.fromhex(VEC["joint_key"])
    assert eg.joint_key([t["record"] for t in VEC["trustees"]]) == Y
    n, eid, cts = VEC["candidate_count"], VEC["election_id"], []
    for b in VEC["ballots"]:
        raw = eg.encrypt_ballot(Y, eid, b["nullifier_hash"], n, b["choice"], _rng(b["rng"]))
        assert raw.hex() == b["sealed_ballot"]
        cts.append(eg.verify_ballot(raw, Y, eid, b["nullifier_hash"], n))
    agg = eg.aggregate(cts, n)
    assert [{"a": a.hex(), "b": b.hex()} for a, b in agg] == VEC["aggregate"]
    shares = []
    for t, s in zip(VEC["trustees"], VEC["shares"]):
        x = int.from_bytes(bytes.fromhex(t["secret"]), "little")
        share = eg.decryption_share(x, t["org_id"], eid, agg, _rng(s["rng"]))
        assert share == s["share"] and eg.verify_share(share, bytes.fromhex(t["record"]["public_key"]), eid, agg)
        shares.append(share)
    assert eg.combine(agg, shares, len(cts)) == VEC["votes"]


def _keys():
    pairs = [eg.trustee_keygen(o) for o in ("org_a", "org_b", "org_c")]
    return pairs, eg.joint_key([r for _, r in pairs])


def _cheat_bit(Y, ctx, j, m, r):
    """A 0/1 proof made as if m were 0 or 1 when it is not: it cannot verify."""
    A, B = eg.base(r), eg.add(eg.mul(r, Y), eg.base(m))
    w0, w1, c0 = eg.rand_scalar(), eg.rand_scalar(), eg.rand_scalar()
    T, U = [eg.base(w0), eg.base(w1)], [eg.mul(w0, Y), eg.mul(w1, Y)]
    c = eg.hs("bit", *ctx, eg.u16(j), A, B, T[0], U[0], T[1], U[1])
    return [A, B, eg.enc_scalar(c0), eg.enc_scalar(c - c0), eg.enc_scalar(w0 + c0 * r), eg.enc_scalar(w1 + (c - c0) * r)]


def test_forged_ballots_do_not_verify():
    _, Y = _keys()
    eid, nh = "elc_T", "11" * 32
    ok = eg.encrypt_ballot(Y, eid, nh, 3, 1)
    assert eg.verify_ballot(ok, Y, eid, nh, 3) is not None
    assert eg.verify_ballot(ok, Y, eid, "22" * 32, 3) is None          # another credential
    assert eg.verify_ballot(ok, Y, "elc_U", nh, 3) is None             # another election
    assert eg.verify_ballot(ok, Y, eid, nh, 2) is None                 # wrong candidate count
    assert eg.verify_ballot(ok[:-1], Y, eid, nh, 3) is None
    # [5, 0, 0]: a 5 in one ciphertext fails its 0/1 proof whatever the sum proof says
    ctx = (eid.encode(), bytes.fromhex(nh), Y)
    entries = [part for j, m in enumerate([5, 0, 0]) for part in _cheat_bit(Y, ctx, j, m, eg.rand_scalar())]
    assert eg.verify_ballot(eg.MAGIC + eg.u16(3) + b"".join(entries) + bytes(64), Y, eid, nh, 3) is None
    # [1, 1, 1]: every 0/1 proof honest, the sum proof cannot be
    ones = [eg.encrypt_ballot(Y, eid, nh, 3, j) for j in range(3)]
    forged = ones[0][:6] + b"".join(ones[j][6 + 192 * j:6 + 192 * (j + 1)] for j in range(3)) + ones[0][-64:]
    assert eg.verify_ballot(forged, Y, eid, nh, 3) is None
    # identity / small-order points are refused before any arithmetic
    bad = bytearray(ok)
    bad[6:38] = eg.IDENTITY
    assert eg.verify_ballot(bytes(bad), Y, eid, nh, 3) is None


def test_trustee_key_proof_binds_org():
    _, record = eg.trustee_keygen("org_a")
    assert eg.verify_trustee_key(record)
    assert not eg.verify_trustee_key(record | {"org_id": "org_b"})
    assert not eg.verify_trustee_key(record | {"proof": eg.trustee_keygen("org_a")[1]["proof"]})


def test_only_every_share_together_decrypts_the_sum():
    pairs, Y = _keys()
    eid = "elc_T"
    ballots = [eg.verify_ballot(eg.encrypt_ballot(Y, eid, f"{i:064x}", 2, ch), Y, eid, f"{i:064x}", 2)
               for i, ch in enumerate([0, 1, 1, 0, 1])]
    agg = eg.aggregate(ballots, 2)
    shares = [eg.decryption_share(x, r["org_id"], eid, agg) for x, r in pairs]
    assert eg.combine(agg, shares, 5) == [2, 3]
    with pytest.raises(ValueError):
        eg.combine(agg, shares[:2], 5)                                  # two of three do not decrypt
    other = eg.aggregate(ballots[:2], 2)
    assert not eg.verify_share(shares[0], bytes.fromhex(pairs[0][1]["public_key"]), eid, other)


def test_identity_aggregate_still_decrypts():
    """Two ballots whose randomness for candidate 0 adds up to L make A_0 the identity; the tally must not stall."""
    pairs, Y = _keys()
    eid = "elc_T"
    r = eg.rand_scalar()
    seq = iter([r, *[eg.rand_scalar() for _ in range(20)]])
    b1 = eg.encrypt_ballot(Y, eid, "01" * 32, 2, 0, lambda: next(seq))
    seq2 = iter([eg.L - r, *[eg.rand_scalar() for _ in range(20)]])
    b2 = eg.encrypt_ballot(Y, eid, "02" * 32, 2, 1, lambda: next(seq2))
    cts = [eg.verify_ballot(b, Y, eid, n, 2) for b, n in ((b1, "01" * 32), (b2, "02" * 32))]
    agg = eg.aggregate(cts, 2)
    assert agg[0][0] == eg.IDENTITY
    shares = [eg.decryption_share(x, rec["org_id"], eid, agg) for x, rec in pairs]
    assert all(eg.verify_share(s, bytes.fromhex(rec["public_key"]), eid, agg) for s, (_, rec) in zip(shares, pairs))
    assert eg.combine(agg, shares, 2) == [1, 1]
    forged = [dict(s, shares=[dict(s["shares"][0], d=eg.G.hex()), s["shares"][1]]) for s in shares[:1]]
    assert not eg.verify_share(forged[0], bytes.fromhex(pairs[0][1]["public_key"]), eid, agg)


def test_malformed_trustee_records_are_false_not_errors():
    _, record = eg.trustee_keygen("org_a")
    assert not eg.verify_trustee_key(record | {"org_id": 5})
    assert not eg.verify_trustee_key({"org_id": "org_a"})
