"""Writes tests/vectors/elgamal-vectors.json from contracts/ballot-elgamal.md (D-30) with a fixed scalar sequence.

Run from the repo root with Core's dependencies: python tools/make_elgamal_vectors.py
The browser (web/tests/elgamal-vectors.test.ts) and Core (core/tests/test_elgamal.py) must reproduce every value.
"""
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
from app import elgamal as eg  # noqa: E402


def scalar_stream(seed: str):
    """scalar i = SHA-512(seed || u32 i) read little-endian, mod L (never 0 in practice)."""
    out, i = [], 0

    def nxt() -> int:
        nonlocal i
        s = int.from_bytes(hashlib.sha512(seed.encode() + i.to_bytes(4, "big")).digest(), "little") % eg.L
        i += 1
        out.append(eg.enc_scalar(s).hex())
        return s
    return nxt, out


def main() -> None:
    election_id = "elc_01JD000000000000000000VECT"
    orgs = ["org_commission", "org_neutral", "org_observer"]
    trustees = []
    for org in orgs:
        rng, used = scalar_stream("trustee:" + org)
        x, record = eg.trustee_keygen(org, rng)
        trustees.append({"org_id": org, "rng": used, "secret": eg.enc_scalar(x).hex(), "record": record})
    Y = eg.joint_key([t["record"] for t in trustees])
    n = 3
    ballots = []
    for i, choice in enumerate([0, 2, 0, 1]):
        nullifier_hash = hashlib.sha256(f"credential-{i}".encode()).hexdigest()
        rng, used = scalar_stream(f"ballot:{i}")
        raw = eg.encrypt_ballot(Y, election_id, nullifier_hash, n, choice, rng)
        assert eg.verify_ballot(raw, Y, election_id, nullifier_hash, n)
        ballots.append({"nullifier_hash": nullifier_hash, "choice": choice, "rng": used, "sealed_ballot": raw.hex()})
    agg = eg.aggregate([eg.verify_ballot(bytes.fromhex(b["sealed_ballot"]), Y, election_id, b["nullifier_hash"], n)
                        for b in ballots], n)
    shares = []
    for t in trustees:
        rng, used = scalar_stream("share:" + t["org_id"])
        share = eg.decryption_share(int.from_bytes(bytes.fromhex(t["secret"]), "little"), t["org_id"], election_id, agg, rng)
        shares.append({"rng": used, "share": share})
    votes = eg.combine(agg, [s["share"] for s in shares], len(ballots))
    assert votes == [2, 1, 1]
    doc = {
        "description": "ElGamal ballot vectors generated from contracts/ballot-elgamal.md by tools/make_elgamal_vectors.py. "
                       "Each rng list is the exact scalar sequence the function consumed. Do not edit by hand.",
        "election_id": election_id, "candidate_count": n, "trustees": trustees, "joint_key": Y.hex(),
        "ballots": ballots, "aggregate": [{"a": a.hex(), "b": b.hex()} for a, b in agg], "shares": shares,
        "votes": votes,
    }
    path = ROOT / "tests" / "vectors" / "elgamal-vectors.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
