// D-30: exponential ElGamal ballot over the Ed25519 prime-order group, exactly as contracts/ballot-elgamal.md and
// core/app/elgamal.py (shared vectors in tests/vectors/elgamal-vectors.json). The browser only encrypts and proves;
// nobody holds the whole decryption key.
import sodium from "libsodium-wrappers-sumo";

const L = 2n ** 252n + 27742317777372353535851937790883648493n;
const IDENTITY = Uint8Array.from({ length: 32 }, (_, i) => (i === 0 ? 1 : 0));
const DOMAIN = new TextEncoder().encode("PureVoting/elgamal-ed25519/v1");
const MAGIC = new TextEncoder().encode("PVE1");
export const MAX_CANDIDATES = 15;

export type Rng = () => bigint;

// no import from ./hashing: node --test loads this file directly (tests/elgamal-vectors.test.ts)
const hexToBytes = (hex: string) => Uint8Array.from(hex.match(/../g) ?? [], (b) => parseInt(b, 16));

const mod = (s: bigint) => ((s % L) + L) % L;
const leToBig = (b: Uint8Array) => b.reduceRight((acc, byte) => (acc << 8n) | BigInt(byte), 0n);

export function encScalar(s: bigint): Uint8Array {
  let v = mod(s);
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i++, v >>= 8n) out[i] = Number(v & 0xffn);
  return out;
}

const same = (a: Uint8Array, b: Uint8Array) => a.length === b.length && a.every((x, i) => x === b[i]);
const base = (s: bigint) => (mod(s) === 0n ? IDENTITY : sodium.crypto_scalarmult_ed25519_base_noclamp(encScalar(s)));
const mul = (s: bigint, p: Uint8Array) =>
  mod(s) === 0n || same(p, IDENTITY) ? IDENTITY : sodium.crypto_scalarmult_ed25519_noclamp(encScalar(s), p);
const add = (p: Uint8Array, q: Uint8Array) => sodium.crypto_core_ed25519_add(p, q);
const sub = (p: Uint8Array, q: Uint8Array) => sodium.crypto_core_ed25519_sub(p, q);
const total = (points: Uint8Array[]) => points.reduce(add, IDENTITY);
const u16 = (n: number) => Uint8Array.of(n >> 8, n & 0xff);

const concat = (parts: Uint8Array[]) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((at, p) => (out.set(p, at), at + p.length), 0);
  return out;
};

/** Fiat-Shamir challenge: SHA-512 over length-prefixed parts, read little-endian, reduced mod L */
function hs(label: string, ...parts: Uint8Array[]): bigint {
  const all = [DOMAIN, new TextEncoder().encode(label), ...parts].flatMap((p) => [u16(p.length), p]);
  return mod(leToBig(sodium.crypto_hash_sha512(concat(all))));
}

export function randScalar(): bigint {
  for (;;) {
    const s = mod(leToBig(sodium.randombytes_buf(64)));
    if (s !== 0n) return s;
  }
}

/** the sealed_ballot bytes for candidate index `choice` of n (candidate_id order), bound to the credential */
export async function encryptBallot(publicKey: Uint8Array, electionId: string, nullifierHash: string, n: number,
                                    choice: number, rng: Rng = randScalar): Promise<Uint8Array> {
  await sodium.ready;
  if (n < 1 || n > MAX_CANDIDATES || choice < 0 || choice >= n) throw new Error("후보 번호가 범위를 벗어났어요");
  if (!sodium.crypto_core_ed25519_is_valid_point(publicKey)) throw new Error("선거 공개키 형식이 올바르지 않아요");
  const Y = publicKey;
  const G = base(1n);
  const ctx = [new TextEncoder().encode(electionId), hexToBytes(nullifierHash), Y];
  const out: Uint8Array[] = [MAGIC, u16(n)];
  const As: Uint8Array[] = [], Bs: Uint8Array[] = [];
  let R = 0n;
  for (let j = 0; j < n; j++) {
    const m = j === choice ? 1 : 0, s = 1 - m;
    const r = rng();
    const A = base(r), B = m ? add(mul(r, Y), G) : mul(r, Y);
    const cs = rng(), zs = rng(), w = rng();
    const T: Uint8Array[] = [], U: Uint8Array[] = [];
    T[m] = base(w);
    U[m] = mul(w, Y);
    T[s] = sub(base(zs), mul(cs, A));
    U[s] = sub(mul(zs, Y), mul(cs, s ? sub(B, G) : B));
    const c = hs("bit", ...ctx, u16(j), A, B, T[0], U[0], T[1], U[1]);
    const cc: bigint[] = [], zz: bigint[] = [];
    cc[s] = cs;
    cc[m] = mod(c - cs);
    zz[s] = zs;
    zz[m] = mod(w + cc[m] * r);
    out.push(A, B, encScalar(cc[0]), encScalar(cc[1]), encScalar(zz[0]), encScalar(zz[1]));
    As.push(A);
    Bs.push(B);
    R += r;
  }
  const w = rng();
  const c = hs("sum", ...ctx, total(As), total(Bs), base(w), mul(w, Y));
  out.push(encScalar(c), encScalar(w + c * R));
  return concat(out);
}
