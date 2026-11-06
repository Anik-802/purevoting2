// contracts/hashing.md primitives for the browser: H, canonical (RFC 8785 JCS), Hc, merkle_root, set_root.
// Every value must match Core, the Gateway and the Rule Engine bit for bit; tests/hash-vectors.test.ts checks the
// shared vectors (tests/vectors/hash-vectors.json).

const enc = new TextEncoder();

const toHex = (buf: ArrayBuffer) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");

/** H(bytes) / H(str) = SHA-256 as 64 lowercase hex chars; strings are hashed as UTF-8 */
export async function H(input: string | Uint8Array): Promise<string> {
  const bytes = typeof input === "string" ? enc.encode(input) : input;
  return toHex(await crypto.subtle.digest("SHA-256", bytes));
}

/**
 * RFC 8785 JSON Canonicalization Scheme. Hashed objects never contain floats (hashing.md), so numbers must be safe
 * integers; strings and integers serialise exactly as ES2015 JSON.stringify, object keys sort by UTF-16 code units.
 */
export function canonical(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new Error(`canonical: non-integer number ${value}`);
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== undefined)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
  }
  throw new Error(`canonical: unsupported ${typeof value}`);
}

export const Hc = (value: unknown) => H(canonical(value));

/** leaves in the given order; odd node carried up unchanged; one leaf = that leaf; empty = H("") */
export async function merkleRoot(leaves: string[]): Promise<string> {
  if (!leaves.length) return H("");
  let level = leaves;
  while (level.length > 1) {
    const next: string[] = [];
    for (let i = 0; i < level.length; i += 2) next.push(i + 1 < level.length ? await H(level[i] + level[i + 1]) : level[i]);
    level = next;
  }
  return level[0];
}

/** merkle_root(sorted ascending, duplicates removed) */
export const setRoot = (items: string[]) => merkleRoot([...new Set(items)].sort());

export const hexToBytes = (hex: string) => Uint8Array.from(hex.match(/../g) ?? [], (b) => parseInt(b, 16));

export const base64ToBytes = (b64: string) => Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));

export const bytesToBase64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes));

/** receipt_hash = Hc({election_id, nullifier_hash, ballot_hash}); the voter computes it before sending */
export const receiptHash = (election_id: string, nullifier_hash: string, ballot_hash: string) =>
  Hc({ election_id, nullifier_hash, ballot_hash });
