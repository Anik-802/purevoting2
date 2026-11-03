"""Tests for tools/ethsign.py. Pure checks run anywhere; node checks need a running local network (RPC port as argv[1])."""
import os
import secrets
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, HERE)
import ethsign as E  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
if PORT:
    sys.argv.pop(1)


class Pure(unittest.TestCase):
    def test_keccak_empty(self):
        self.assertEqual(E.keccak256(b"").hex(), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")

    def test_keccak_is_not_sha3(self):
        import hashlib
        self.assertNotEqual(E.keccak256(b"abc"), hashlib.sha3_256(b"abc").digest())

    def test_keccak_block_boundaries(self):
        for n in (0, 1, 135, 136, 137, 271, 272, 273):
            self.assertEqual(len(E.keccak256(b"a" * n)), 32)
        self.assertNotEqual(E.keccak256(b"a" * 135), E.keccak256(b"a" * 136))

    def test_curve_constants(self):
        self.assertEqual((E.GY ** 2 - E.GX ** 3 - 7) % E.P, 0)      # G is on the curve
        self.assertIsNone(E._mul(E.N))                              # G has order N

    def test_rlp(self):
        self.assertEqual(E.rlp(b"dog").hex(), "83646f67")
        self.assertEqual(E.rlp([b"cat", b"dog"]).hex(), "c88363617483646f67")
        self.assertEqual(E.rlp(0).hex(), "80")
        self.assertEqual(E.rlp(1024).hex(), "820400")

    def test_sign_is_low_s_and_deterministic(self):
        k = int.from_bytes(secrets.token_bytes(32), "big") % (E.N - 1) + 1
        d = E.keccak256(b"x")
        a, b = E.sign_hash(k, d), E.sign_hash(k, d)
        self.assertEqual(a, b)
        self.assertLessEqual(a[1], E.N // 2)


@unittest.skipUnless(PORT, "needs a running node: pass its RPC port as the first argument")
class AgainstNode(unittest.TestCase):
    def rpc(self, method, params=None):
        from chain import rpc
        return rpc(PORT, method, params)

    def test_keccak_matches_the_client(self):
        for data in (b"", b"abc", b"\x00" * 200, secrets.token_bytes(1), secrets.token_bytes(135), secrets.token_bytes(136),
                     secrets.token_bytes(1000)):
            self.assertEqual("0x" + E.keccak256(data).hex(), self.rpc("web3_sha3", ["0x" + data.hex()]))

    def test_signed_tx_is_accepted_and_sender_is_recovered(self):
        priv = int.from_bytes(secrets.token_bytes(32), "big") % (E.N - 1) + 1
        me = E.address(priv)
        chain_id = int(self.rpc("eth_chainId"), 16)
        raw = E.sign_legacy_tx(priv, chain_id, 0, 0, 21000, me, 0, b"")
        h = self.rpc("eth_sendRawTransaction", ["0x" + raw.hex()])
        import time
        for _ in range(60):
            try:
                r = self.rpc("eth_getTransactionReceipt", [h])
            except RuntimeError:
                r = None
            if r:
                break
            time.sleep(0.5)
        self.assertIsNotNone(r)
        self.assertEqual(r["status"], "0x1")
        self.assertEqual(r["from"].lower(), me.lower())            # the client recovered our address from the signature

    def test_personal_sign_recovers_with_ecrecover_precompile(self):
        priv = int.from_bytes(secrets.token_bytes(32), "big") % (E.N - 1) + 1
        digest = E.keccak256(b"pv2 test digest")
        sig = E.personal_sign_digest(priv, digest)
        prefixed = E.keccak256(b"\x19Ethereum Signed Message:\n32" + digest)
        data = "0x" + prefixed.hex() + sig[64:].rjust(32, b"\0").hex() + sig[:32].hex() + sig[32:64].hex()
        out = self.rpc("eth_call", [{"to": "0x0000000000000000000000000000000000000001", "data": data}, "latest"])
        self.assertEqual("0x" + out[-40:], E.address(priv).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
