// Shared ElGamal ballot vectors (tests/vectors/elgamal-vectors.json): the browser must produce the same bytes as Core.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { encryptBallot } from "../src/crypto/elgamal.ts";
import { hexToBytes } from "../src/crypto/hashing.ts";

type Ballot = { nullifier_hash: string; choice: number; rng: string[]; sealed_ballot: string };
const doc = JSON.parse(readFileSync(new URL("../../tests/vectors/elgamal-vectors.json", import.meta.url), "utf-8")) as {
  election_id: string;
  candidate_count: number;
  joint_key: string;
  ballots: Ballot[];
};

const leToBig = (b: Uint8Array) => b.reduceRight((acc, byte) => (acc << 8n) | BigInt(byte), 0n);
const toHex = (b: Uint8Array) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");

for (const [i, b] of doc.ballots.entries()) {
  test(`ballot ${i} (choice ${b.choice})`, async () => {
    const scalars = b.rng.map((h) => leToBig(hexToBytes(h)));
    const rng = () => {
      const s = scalars.shift();
      if (s === undefined) throw new Error("vector rng exhausted");
      return s;
    };
    const raw = await encryptBallot(hexToBytes(doc.joint_key), doc.election_id, b.nullifier_hash, doc.candidate_count, b.choice, rng);
    assert.equal(toHex(raw), b.sealed_ballot);
    assert.equal(scalars.length, 0);
  });
}
