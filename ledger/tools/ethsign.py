"""Minimal Ethereum signing for demo and test keys (standard library only).

Provides keccak-256, secp256k1 signing with RFC 6979 nonces, EIP-155 legacy transactions and EIP-191 personal_sign of a
32-byte digest. It exists because the ledger scripts must run where pip is not available. It is NOT constant time and
must only be used with demo or test keys. Production keys belong in a hardware or library-backed signer.
"""
import hashlib
import hmac

# ---------------------------------------------------------------- keccak-256 (original Keccak padding 0x01, not SHA3)
_MASK = (1 << 64) - 1


def _round_constants():
    def rc(t):
        if t % 255 == 0:
            return 1
        r = 1
        for _ in range(t % 255):
            r <<= 1
            if r & 0x100:
                r ^= 0x171
        return r & 1
    return [sum(rc(7 * i + j) << ((1 << j) - 1) for j in range(7)) for i in range(24)]


def _rotation_offsets():
    r = [[0] * 5 for _ in range(5)]
    x, y = 1, 0
    for t in range(24):
        r[x][y] = ((t + 1) * (t + 2) // 2) % 64
        x, y = y, (2 * x + 3 * y) % 5
    return r


_RC = _round_constants()
_ROT = _rotation_offsets()


def _rol(v, n):
    n %= 64
    return ((v << n) | (v >> (64 - n))) & _MASK if n else v


def _keccak_f(a):
    for rnd in range(24):
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        a[0][0] ^= _RC[rnd]
    return a


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data) + b"\x01"
    padded += b"\x00" * (-len(padded) % rate)
    padded[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(padded), rate):
        block = padded[off: off + rate]
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(block[8 * i: 8 * i + 8], "little")
        a = _keccak_f(a)
    out = b"".join(a[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


# ---------------------------------------------------------------- secp256k1
P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
G = (GX, GY)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % P == 0:
        return None
    if p == q:
        m = 3 * p[0] * p[0] * pow(2 * p[1], -1, P) % P
    else:
        m = (q[1] - p[1]) * pow(q[0] - p[0], -1, P) % P
    x = (m * m - p[0] - q[0]) % P
    return x, (m * (p[0] - x) - p[1]) % P


def _mul(k, p=G):
    r = None
    while k:
        if k & 1:
            r = _add(r, p)
        p = _add(p, p)
        k >>= 1
    return r


def public_key(priv: int):
    if not 1 <= priv < N:
        raise ValueError("private key out of range")
    return _mul(priv)


def address(priv: int) -> str:
    x, y = public_key(priv)
    return "0x" + keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[12:].hex()


def _rfc6979(priv: int, h1: bytes) -> int:
    x = priv.to_bytes(32, "big")
    h = (int.from_bytes(h1, "big") % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_hash(priv: int, digest: bytes):
    """Returns (r, s, recovery_id) with low s (EIP-2)."""
    z = int.from_bytes(digest, "big")
    k = _rfc6979(priv, digest)
    rx, ry = _mul(k)
    r = rx % N
    s = pow(k, -1, N) * (z + r * priv) % N
    rec = (ry & 1) | (2 if rx >= N else 0)
    if s > N // 2:
        s = N - s
        rec ^= 1
    if r == 0 or s == 0:
        raise ValueError("bad nonce")
    return r, s, rec


# ---------------------------------------------------------------- RLP and transactions
def _int_bytes(v: int) -> bytes:
    return v.to_bytes((v.bit_length() + 7) // 8, "big") if v else b""


def rlp(item) -> bytes:
    if isinstance(item, int):
        item = _int_bytes(item)
    if isinstance(item, (bytes, bytearray)):
        item = bytes(item)
        if len(item) == 1 and item[0] < 0x80:
            return item
        return _len_prefix(len(item), 0x80) + item
    body = b"".join(rlp(i) for i in item)
    return _len_prefix(len(body), 0xC0) + body


def _len_prefix(n: int, base: int) -> bytes:
    if n < 56:
        return bytes([base + n])
    nb = _int_bytes(n)
    return bytes([base + 55 + len(nb)]) + nb


def sign_legacy_tx(priv: int, chain_id: int, nonce: int, gas_price: int, gas: int, to, value: int,
                   data: bytes) -> bytes:
    """EIP-155 signed legacy transaction, ready for eth_sendRawTransaction. to=None creates a contract."""
    to_b = bytes.fromhex(to[2:]) if to else b""
    fields = [nonce, gas_price, gas, to_b, value, data]
    r, s, rec = sign_hash(priv, keccak256(rlp(fields + [chain_id, 0, 0])))
    return rlp(fields + [rec + 35 + 2 * chain_id, r, s])


def personal_sign_digest(priv: int, digest32: bytes) -> bytes:
    """eip191-secp256k1 signature (r || s || v, v = 27 or 28) over a 32-byte digest, as in contracts/hashing.md."""
    if len(digest32) != 32:
        raise ValueError("digest must be 32 bytes")
    r, s, rec = sign_hash(priv, keccak256(b"\x19Ethereum Signed Message:\n32" + digest32))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([27 + rec])
