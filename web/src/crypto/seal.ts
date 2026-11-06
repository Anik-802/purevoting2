// D-08: the ballot {"candidate_id": ...} is sealed in the browser with the election's X25519 public key
// (libsodium sealed box); only the commission's private key opens it, after closing.
import sodium from "libsodium-wrappers";
import { base64ToBytes, bytesToBase64 } from "./hashing";

/** plaintext: UTF-8 JSON object with exactly one key "candidate_id"; the tally and R5 compare after parsing, so
 * whitespace does not matter (core-api runTally) */
export const ballotPlaintext = (candidate_id: string) => JSON.stringify({ candidate_id });

/** returns the sealed ballot as standard base64 (core-api submitBallot.sealed_ballot) */
export async function sealBallot(publicKeyB64: string, candidate_id: string): Promise<string> {
  await sodium.ready;
  const pk = base64ToBytes(publicKeyB64);
  if (pk.length !== sodium.crypto_box_PUBLICKEYBYTES) throw new Error("선거 공개키 형식이 올바르지 않아요");
  return bytesToBase64(sodium.crypto_box_seal(new TextEncoder().encode(ballotPlaintext(candidate_id)), pk));
}
