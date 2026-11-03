"""Reproduce every vector of tests/vectors/hash-vectors.json with ledger/gateway/pv_hash.py.

Run from the repository root:  python -m unittest discover -s ledger/gateway/tests -v
A vector name without a handler fails the test, so new vectors cannot be silently skipped.
"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import pv_hash as pv  # noqa: E402

VECTORS = json.loads((HERE.parents[2] / "tests" / "vectors" / "hash-vectors.json").read_text(encoding="utf-8"))["vectors"]

# Exact-name handlers: input -> expected value.
EXACT = {
    "H_empty": pv.H,
    "H_text": pv.H,
    "canonical": pv.canonical,
    "Hc": pv.Hc,
    "set_root_dedup": pv.set_root,
    "rejections_root": lambda rows: pv.set_root([pv.Hc(r) for r in rows]),
    "rejections_root_none": lambda rows: pv.set_root([pv.Hc(r) for r in rows]),
    "token_hash": pv.H,
    "nullifier_hash": pv.H,
    "ballot_hash": lambda hex_bytes: pv.H(bytes.fromhex(hex_bytes)),
    "receipt_hash": pv.Hc,
    "roster_hash": lambda voter_keys: pv.set_root([pv.H(k) for k in voter_keys]),
    "token_set_root": pv.set_root,
    "config_hash": pv.Hc,
    "ballot_set_root_duplicates": lambda hashes: pv.merkle_root(sorted(hashes)),
    "result_hash": pv.Hc,
    "idh": lambda x: pv.idh(x).hex(),
    "request_digest": lambda a: pv.proposal_digest(*a),
    "approval_digest": lambda a: pv.proposal_digest(*a),
    "finding_record_digest": lambda a: pv.finding_digest(*a),
    "review_digest": lambda a: pv.review_digest(*a),
}

# Prefix handlers for families with numbered or per-type names.
PREFIX = {
    "merkle_root_": pv.merkle_root,
    "leaf_hash_": pv.Hc,
    "payload_hash_": pv.Hc,
    "rejection_leaf_": pv.Hc,
    "target_hash_": pv.Hc,
    "snapshot_hash_": pv.Hc,
}


def handler_for(name):
    if name in EXACT:
        return EXACT[name]
    for prefix, fn in PREFIX.items():
        if name.startswith(prefix):
            return fn
    return None


class HashVectors(unittest.TestCase):
    def test_every_vector_has_a_handler(self):
        missing = [v["name"] for v in VECTORS if handler_for(v["name"]) is None]
        self.assertEqual(missing, [], "vectors without a handler in this test")

    def test_vectors_reproduce(self):
        for v in VECTORS:
            fn = handler_for(v["name"])
            if fn is None:
                continue
            with self.subTest(vector=v["name"]):
                self.assertEqual(fn(v["input"]), v["expected"])

    def test_floats_are_rejected(self):
        with self.assertRaises(ValueError):
            pv.canonical({"x": 1.5})

    def test_canonical_matches_rfc8785_library_when_installed(self):
        try:
            import rfc8785
        except ImportError:
            self.skipTest("rfc8785 not installed (pip install -r tools/requirements.txt)")
        for v in VECTORS:
            if isinstance(v["input"], (dict, list)):
                with self.subTest(vector=v["name"]):
                    self.assertEqual(pv.canonical(v["input"]).encode("utf-8"), rfc8785.dumps(v["input"]))


if __name__ == "__main__":
    unittest.main()
