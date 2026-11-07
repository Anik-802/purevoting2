# Gateway M1: temporary choices for the team lead to decide

The Gateway frame is built first (walking skeleton). Each item below is a temporary default. Answer with the option number.

| # | Open point | Default now | Options | Recommendation |
|---|---|---|---|---|
| G1 | Gateway language and stack | Python standard library plus sqlite, no dependencies | 1 keep; 2 FastAPI like Core | 1 for M1 (runs in a plain python image), 2 if the team wants one stack |
| G2 | Request wait | POST /v1/events waits 3.5 s for confirmation, else RETRY_LATER | 1 keep; 2 answer RETRY_LATER at once | 1 (caller timeout is 5 s) |
| G3 | Immediate anchor | one commitAnchor transaction per system event, after its state transaction is confirmed | 1 keep; 2 group events per bucket | 1 for M1, 2 later if the transaction count matters |
| G4 | Contract refusal of a transaction | the transaction is sent anyway and the confirmed revert is mapped (TimeNotReached and NotGateway only answer RETRY_LATER without a rejection) | 1 keep | 1 (chain-interface.md "Confirmations") |
| G5 | Event types outside M1 | REJECTED INVALID_STATE_TRANSITION with detail "not implemented in M1" (consumes the seq) | 1 keep; 2 HTTP 501 | 1 until the week 4 and 5 functions exist |
| G6 | Request validation | required fields, ids, types and payload_hash only; no full JSON-schema check | 1 keep; 2 add jsonschema | 1 for M1 |
| G7 | Alerts to Core | ANCHOR_REJECTED goes to Core POST /internal/v1/audit-alerts with GATEWAY_KEY (CORE_URL, retried in the background); other conditions are logged and shown by /v1/health | decided by contracts v1.1 | none |
| G8 | Voter batch | closes at 50 events or 60 s, at once when the election is CLOSED | 1 keep | 1 |
| G9 | Token state check | duplicate token_hash and nullifier_hash only; revoked tokens need TOKEN_REVOKED (week 4) | 1 keep | 1 |
| G10 | Contract addition | setTokenSetRoot allowed while ROSTER_FROZEN or OPEN; a non-zero proposal id reverts until proposals exist | 1 keep | 1 |
| G11 | Rejections recorded after the last anchor of an election | stay unanchored until the next anchor of that election | 1 keep; 2 commit a rejection-only anchor (event_count 0) a few seconds after a rejection | 2, so no refusal can stay outside the chain |
| G12 | Node RPC host check | Core and the Gateway reach validators by container name or host name. geth answers `403 invalid host specified` unless started with `--http.vhosts "*"` (or the exact names) and `--http.addr 0.0.0.0` inside the compose network. The integration compose must start the validators this way; the local kit needed the same change. | Found when running Core against the Gateway on 2026-11-08 |
| G13 | Batch variable name | The Gateway reads `ANCHOR_BATCH_MAX_EVENTS` (the name in `.env.example`) and falls back to `ANCHOR_BATCH_EVENTS`. | Alignment with `.env.example` |
| G14 | Core verified | `devtools.demo_flow` (create, freeze, issue, open, 3 votes, close, receipts ANCHORED) passed against the real Gateway on a local 3-validator chain, with Core `CREATION_CHECK_MODE=node`. No Gateway change was needed for Core's requests. | 2026-11-08 |
