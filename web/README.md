# web/ — Voter web and admin web (owner: Seungmin)

Role design: `docs/roles/core-web.md` (section 4.2). Signing happens in the browser; Core only relays signatures.

## Run

```
cd web
npm install
npm run dev                        # http://localhost:5173, mock API (in memory, any id / password)
VITE_API_MODE=real npm run dev     # same, against Core on 127.0.0.1:8000 (Vite proxies /v1)
npm test                           # shared hash vectors (tests/vectors) against src/crypto/hashing.ts
npm run build                      # type check + bundle; builds default to the real API
```

- `/` entry page, `/vote/<election_id>#t=<token>` voter flow, `/receipt` receipt check, `/admin` admin web.
- `src/api/index.ts` picks `mock.ts` (in-memory stand-in) or `real.ts` (Core client); both export the same functions,
  named after the operationIds of `contracts/core-api.openapi.yaml`, so the screens never change.
- Real mode: the browser makes the credential, seals `{"candidate_id": …}` with the election public key (libsodium
  sealed box, `src/crypto/seal.ts`, loaded only when voting), stores the sealed bytes before sending, and checks Core's
  receipt against its own `receipt_hash` (`src/crypto/hashing.ts`, RFC 8785 + SHA-256). A 429 from the public entry is
  retried (it never reached Core, D-29). Staff log in with Core JWTs (session storage).
- Tally and finalize call Core (`runTally`, `finalizeElection`); finalize is permissive, so the election shows
  FINALIZED only after the Gateway accepts ELECTION_FINALIZED.
- Not in Core yet (shown as notices in real mode): manual open / close (Core follows the window); approval / review
  screens and their signature digests (part B, 11-14 – 11-17); mail delivery.
- Candidate pledges (description) exist only in the mock: `core-api` Candidate has no such field.
- The CSP allows `'wasm-unsafe-eval'` for libsodium's WebAssembly only; string eval stays blocked.
- Voting needs a secure context (`crypto.subtle`, `crypto.randomUUID`): `https://` (Funnel / Serve) or
  `localhost` / `127.0.0.1`. Opened over a LAN IP with `http://` the voter web cannot vote (D-29).
- Browser storage (threat-model S9): the credential (`pv_cred_<H(token)>`) and the sealed ballot
  (`pv_sealed_<H(credential)>`) stay only until Core's receipt matches, then both are deleted; the choice itself is
  never stored in clear. A sealed ballot whose answer was lost is resent unchanged.
- `tests/` runs with Node's type stripping and is not type-checked by `tsc`: modules it imports must use erasable
  syntax only (no enums, namespaces or constructor parameter properties).
- Compose (`docker compose up`): the `web` service builds this folder and serves it with nginx on `127.0.0.1:3000`
  together with Core's `/v1/` (`nginx.conf`, D-29); `npm run dev` proxies `/v1` to Core on 8000 the same way.
- Fonts are self-hosted (`pretendard` package), so pages make no third-party requests and work offline.
- Look: Toss-style blue / grey scale and step flow (voter), KRDS-like navy bar and NEC-style summary tiles (admin);
  tokens in `src/index.css`.
