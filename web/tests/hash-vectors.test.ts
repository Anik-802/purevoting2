// Shared hash vectors (tests/vectors/hash-vectors.json) against the browser hashing module.
// Run: npm test (node --test, TypeScript stripped natively by Node >= 22.18).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { H, Hc, canonical, hexToBytes, merkleRoot, setRoot } from "../src/crypto/hashing.ts";

type Vector = { name: string; input: unknown; expected: string };
const { vectors } = JSON.parse(readFileSync(new URL("../../tests/vectors/hash-vectors.json", import.meta.url), "utf-8")) as {
  vectors: Vector[];
};

const byRow = (rows: { row_key: string }[]) => [...rows].sort((a, b) => (a.row_key < b.row_key ? -1 : 1));

// name → how the browser computes it; the signature digests belong to the approval / review screens (later)
const compute: [RegExp, (input: any) => Promise<string> | string][] = [
  [/^(H_empty|H_text|token_hash|nullifier_hash|idh)$/, (s: string) => H(s)],
  [/^canonical$/, (o) => canonical(o)],
  [/^(Hc|leaf_hash_.*|payload_hash_.*|rejection_leaf_\d|receipt_hash|config_hash|target_hash_.*|result_hash)$/, (o) => Hc(o)],
  [/^merkle_root_\d$/, (l: string[]) => merkleRoot(l)],
  [/^(set_root_dedup|rejections_root_none|token_set_root)$/, (l: string[]) => setRoot(l)],
  [/^rejections_root$/, async (leaves: object[]) => setRoot(await Promise.all(leaves.map((o) => Hc(o))))],
  [/^roster_hash$/, async (keys: string[]) => setRoot(await Promise.all(keys.map((k) => H(k))))],
  [/^ballot_hash$/, (hex: string) => H(hexToBytes(hex))],
  [/^ballot_set_root_duplicates$/, (l: string[]) => merkleRoot([...l].sort())],
  [/^snapshot_hash_.*$/, (rows) => Hc(byRow(rows))],
];
const LATER = /^(request_digest|approval_digest|finding_record_digest|review_digest)$/;

for (const v of vectors) {
  const fn = compute.find(([re]) => re.test(v.name))?.[1];
  if (!fn) {
    test(`${v.name} (signature digest: approval / review screens)`, { skip: !LATER.test(v.name) ? false : "later" }, () => {
      assert.fail(`no browser formula for vector ${v.name}`);
    });
    continue;
  }
  test(v.name, async () => assert.equal(await fn(v.input), v.expected));
}
