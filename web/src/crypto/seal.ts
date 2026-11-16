// D-08 / D-30: the ballot is sealed in the browser with the election's public key. SEALED_BOX_V1: libsodium sealed box
// of {"candidate_id": ...}, opened by the commission's private key after closing. ELGAMAL_ED25519_V1: one ElGamal
// ciphertext per candidate with proofs (elgamal.ts); only the organizations together decrypt, and only the sums.
import sodium from "libsodium-wrappers-sumo";
import type { PublicElection } from "../api/types";
import { encryptBallot } from "./elgamal";
import { base64ToBytes, bytesToBase64 } from "./hashing";

/** plaintext: UTF-8 JSON object with exactly one key "candidate_id"; the tally and R5 compare after parsing, so
 * whitespace does not matter (core-api runTally) */
export const ballotPlaintext = (candidate_id: string) => JSON.stringify({ candidate_id });

/** returns the sealed ballot as standard base64 (core-api submitBallot.sealed_ballot); nullifier_hash = H(credential) */
export async function sealBallot(e: PublicElection, candidate_id: string, nullifier_hash: string): Promise<string> {
  await sodium.ready;
  const pk = base64ToBytes(e.public_key);
  if (e.ballot_scheme === "ELGAMAL_ED25519_V1") {
    // candidates are numbered in candidate_id order (contracts/ballot-elgamal.md)
    const ids = e.candidates.map((c) => c.candidate_id).sort();
    return bytesToBase64(await encryptBallot(pk, e.election_id, nullifier_hash, ids.length, ids.indexOf(candidate_id)));
  }
  if (pk.length !== sodium.crypto_box_PUBLICKEYBYTES) throw new Error("선거 공개키 형식이 올바르지 않아요");
  return bytesToBase64(sodium.crypto_box_seal(new TextEncoder().encode(ballotPlaintext(candidate_id)), pk));
}
