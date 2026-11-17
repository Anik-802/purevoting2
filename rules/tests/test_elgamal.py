"""Independent shared-vector verification and hostile encoding/proof cases."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from purevoting_rules import elgamal as eg
from elgamal_test_support import trustee_keygen, encrypt_ballot, decryption_share


VECTORS = json.loads((Path(__file__).resolve().parents[2] / "tests/vectors/elgamal-vectors.json").read_text(encoding="utf-8"))


def sequence(hexes):
    source = iter(hexes)
    return lambda: int.from_bytes(bytes.fromhex(next(source)), "little")


def test_shared_vectors_keys_ballots_aggregate_shares_and_votes():
    records = [t["record"] for t in VECTORS["trustees"]]
    assert all(eg.verify_trustee_key(r) for r in records)
    Y = eg.joint_key(records)
    assert Y.hex() == VECTORS["joint_key"]
    ciphertexts = [eg.verify_ballot(bytes.fromhex(b["sealed_ballot"]), Y, VECTORS["election_id"],
                                  b["nullifier_hash"], VECTORS["candidate_count"]) for b in VECTORS["ballots"]]
    assert all(ciphertexts)
    aggregate = eg.aggregate(ciphertexts, VECTORS["candidate_count"])
    assert [{"a": a.hex(), "b": b.hex()} for a, b in aggregate] == VECTORS["aggregate"]
    shares = [s["share"] for s in VECTORS["shares"]]
    for record, share in zip(records, shares):
        assert eg.verify_share(share, bytes.fromhex(record["public_key"]), VECTORS["election_id"], aggregate)
    assert eg.combine(aggregate, shares, len(ciphertexts)) == VECTORS["votes"]


def test_test_only_producers_reproduce_shared_vectors():
    """The edge-case producer is checked against fixed bytes, not merely itself."""
    for t in VECTORS["trustees"]:
        secret, record = trustee_keygen(t["org_id"], sequence(t["rng"]))
        assert eg.enc_scalar(secret).hex() == t["secret"]
        assert record == t["record"]
    Y = bytes.fromhex(VECTORS["joint_key"])
    for ballot in VECTORS["ballots"]:
        assert encrypt_ballot(Y, VECTORS["election_id"], ballot["nullifier_hash"], VECTORS["candidate_count"],
                              ballot["choice"], sequence(ballot["rng"])).hex() == ballot["sealed_ballot"]
    aggregate = [(bytes.fromhex(p["a"]), bytes.fromhex(p["b"])) for p in VECTORS["aggregate"]]
    for trustee, expected in zip(VECTORS["trustees"], VECTORS["shares"]):
        secret = int.from_bytes(bytes.fromhex(trustee["secret"]), "little")
        assert decryption_share(secret, trustee["org_id"], VECTORS["election_id"], aggregate,
                                sequence(expected["rng"])) == expected["share"]


@pytest.mark.parametrize("mutation", ["truncated", "noncanonical_scalar", "identity_point", "other_credential", "other_election", "candidate_count"])
def test_ballot_verification_rejects_invalid_and_rebound_ballots(mutation):
    ballot = VECTORS["ballots"][0]
    raw = bytes.fromhex(ballot["sealed_ballot"])
    Y, eid, nullifier, n = bytes.fromhex(VECTORS["joint_key"]), VECTORS["election_id"], ballot["nullifier_hash"], VECTORS["candidate_count"]
    if mutation == "truncated":
        raw = raw[:-1]
    elif mutation == "noncanonical_scalar":
        raw = raw[:70] + eg.L.to_bytes(32, "little") + raw[102:]
    elif mutation == "identity_point":
        raw = raw[:6] + eg.IDENTITY + raw[38:]
    elif mutation == "other_credential":
        nullifier = "00" * 32
    elif mutation == "other_election":
        eid += "X"
    else:
        n -= 1
    assert eg.verify_ballot(raw, Y, eid, nullifier, n) is None


def test_trustee_proof_binds_organization_and_uses_canonical_hex():
    original = VECTORS["trustees"][0]["record"]
    assert not eg.verify_trustee_key(original | {"org_id": "org_unknown"})
    assert not eg.verify_trustee_key(original | {"public_key": original["public_key"].upper()})
    bad = deepcopy(original)
    bad["proof"]["z"] = eg.L.to_bytes(32, "little").hex()
    assert not eg.verify_trustee_key(bad)
    assert not eg.verify_trustee_key(None)


def test_zero_scalar_and_identity_multiplication_guards():
    assert eg.base(0) == eg.IDENTITY
    assert eg.base(eg.L) == eg.IDENTITY
    assert eg.mul(0, eg.G) == eg.IDENTITY
    assert eg.mul(42, eg.IDENTITY) == eg.IDENTITY
    assert not eg.valid_point(eg.IDENTITY)


def test_tampered_share_is_rejected():
    record = VECTORS["trustees"][0]["record"]
    share = deepcopy(VECTORS["shares"][0]["share"])
    share["shares"][0]["z"] = eg.enc_scalar(eg.dec_scalar(bytes.fromhex(share["shares"][0]["z"])) + 1).hex()
    aggregate = [(bytes.fromhex(p["a"]), bytes.fromhex(p["b"])) for p in VECTORS["aggregate"]]
    assert not eg.verify_share(share, bytes.fromhex(record["public_key"]), VECTORS["election_id"], aggregate)
