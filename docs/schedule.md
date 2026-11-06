# Schedule (from 2026-11-05, decision D-28)

The original plan (system-design section 9) ran 8 weeks. The project now has to show a **full demo on Thu 2026-11-19**
(rehearsal Wed 11-18); **everything is finished by Mon 2026-11-30** (before December). The final report and slides are outside this schedule; the team lead sets them. This page replaces the week table of system-design section 9 and the
"Milestones" tables of `docs/roles/*.md`. Contracts and decisions are unchanged except where section 5 says so.

## 1. Where we are (2026-11-04)

| Owner | Done | Open |
|---|---|---|
| Seungmin (Core, web) | Elections, roster, links, redeem / ballot, outbox, checkpoints, views, internal endpoints, alert kinds per service, findings storage, creation check from the pinned node, outbox-vs-ledger comparison, staff setup, T01 run script, integration compose skeleton (74 tests, CI green) | Tally, finalize, admin actions (ROSTER_AMEND, ELECTION_VOID), review relay, `evidence_check` (always PENDING now), outbox resume, **voter web and admin web (not started)**, scripts for T02 / T04 / T05 / T08 / T15 |
| Harits (contract, Gateway, nodes) | Hash module, private PureChain on own PC (test kit with 4 nodes, RPC 8541–8544), contract 16 / 32 functions with matching selectors, 123 on-chain checks, key generation and registry setup scripts (PRs #1, #2 merged) | **Gateway service (not started)**, finding records, relayed-event indexing, proposals and signature checks, reviews, finalize quorum, void, compose blocks for nodes / deploy / registry / Gateway, measurements (D-27), **T01 label (overdue)** |
| Anik (Rule Engine, AI at zero cost) | Hash and EIP-191 signing, R1–R3, R4 / R5 partly (unimplemented checks listed in `rules/README_SIMULATION.md`), finding store, simulation against fake Core / Gateway | **Rule Engine as a running service against the real Core and Gateway**, R4 / R5 complete, pinned-node reads, compose block, AI Audit Agent (zero cost, in the M2 demo) |

## 2. Milestones

Each milestone ends with an integration run and the supervisor agent's review, recorded as **PASS / CONDITIONAL /
FAIL**. Work on the next milestone continues in parallel; **P1 findings are fixed before the next milestone's run**.
The M2 demo happens on 11-19 whatever the verdict; what is claimed in it follows the supervisor's report.

| # | Date | Milestone | Done when |
|---|---|---|---|
| M1 | **Wed 11-11** | Integration 1: T01 on the real stack | On Seungmin's PC, `docker compose -f docker-compose.yml -f docker-compose.integration.yml up` starts the nodes, contract deploy, registry setup, staff setup (after registry setup), Core, Gateway and Rule Engine; T01 label committed before the run; `tests/scenarios/T01/setup.py --config quick`: creation check PASSED against the real Gateway and observer node, ELECTION_OPENED applied (RETRY_LATER allowed, never REJECTED), receipts ANCHORED, R1–R5 findings recorded on chain and stored in Core. Reported as "T01 (partial R4/R5)" if R4 / R5 are incomplete |
| M2 | **Thu 11-19** | **Demo version** | From a clean `docker compose up`, demo configuration (100 voters, 10-minute window, 60 s bucket and checkpoint). **Election E1 (normal):** admin web: create, roster, freeze, link CSV; voter web: vote, receipt ANCHORED; close, tally, R1–R5 PASS at TALLY, **finalize succeeds** (no FAIL / INCONCLUSIVE, so no review is needed). **Election E2 (faults):** T05 **DB-edit variant** (row added after freeze → R4 / R5 FAIL) and T08 **(a) and (b)** (row changed after its checkpoint was committed and audited → R5 `tables_mismatched` / `row_ledger_mismatches`; sealed bytes changed → `sealed_hash_mismatches`); after tally, **finalize is refused (FINDINGS_UNREVIEWED)**. **Audit report in E2 (D-15 v1.2):** the `audit-agent` compose service writes a report file for each E2 FAIL finding within 10 minutes of the finding being stored, made by the local free model if the 11-13 check passed, otherwise by the template (shown as "not AI"); the demo shows one report and the finding it cites; it is advisory, finalize never depends on it; no report at all → M2 CONDITIONAL. Demo script with a minute-by-minute timetable (change → audited checkpoint → FAIL at the next run → audit report, so the demo never waits for it). **Every web screen demonstrable (part B, same day):** on the mock stack (`docker compose -p pv-demo-b --profile mock up`, fake Gateway, its own project name so its database and the seeded findings never mix with part A; switch rehearsed on 11-18; part A uses `docker compose -f docker-compose.yml -f docker-compose.integration.yml up`), election E3: ROSTER_AMEND and ELECTION_VOID through the approval screen (organization signatures, EIP-191) and the review screen for a FAIL finding, after which finalize succeeds; shown as "chain side from M3" (the real contract checks arrive 11-23 / 11-25); the demo script says the fake checks signatures off chain, its findings come from a seed script (no Rule Engine on the mock stack) and the contract's refusal of an unapproved ROSTER_AMENDED (T05) is shown only from M3. Not on any screen: window change, early close, link reissue (not built) and the audit report screen (future work; the report file is shown). Full T01 PASS (R1–R5 complete); if R4 / R5 are still partial, M2 is CONDITIONAL |
| M3 | **Thu 11-26** | Mandatory scope complete | ROSTER_AMEND and ELECTION_VOID with organization signatures (approval screen → Core relay → contract signature check → execution order); signed reviews of FAIL / INCONCLUSIVE findings (review screen → contract → FINDING_REVIEWED); T01, T02, T04, T05 (all variants), T08 (a)(b)(c), T15 (a)–(d) give their labelled results; every label committed before its first run |

## 3. Plan per owner

Dates are "finished by". A handoff another owner waits for is **bold**. A late handoff is announced the same day with a
new date (section 4).

### Harits — contract, Gateway, nodes

| By | Work |
|---|---|
| **Fri 11-06** | **T01 `expected.json`** and the B1 rule-ID → cause table |
| **Mon 11-09** | **Gateway, M1 minimum:** `POST /v1/events` for every T01 event (ELECTION_CREATED, ROSTER_FROZEN, TOKEN_BATCH_ISSUED, ELECTION_OPENED, CHECKPOINT_COMMITTED, ELECTION_CLOSED; voter checks: window, duplicates, token state; voter-event batching and `commitAnchor`), v1.1 rules (only confirmed blocks count, re-broadcast the same signed transaction, revert → outcome table, `eth_call` pre-check so an early ELECTION_OPENED gets RETRY_LATER), `GET /v1/events` (cursor) and `GET /v1/events/{event_id}`, **`GET /v1/rejections`** with a minimal rejection record (`rejection_leaf`) for every REJECTED and `rejections_root` per hashing.md (R1–R3 read it; usually empty in T01), `GET /v1/chain`, **`GET /v1/organizations`** (Core reads the operator org at creation); **compose blocks**: validators + the observer's pinned node RPC, contract deploy job, registry setup job, ledger-gateway, healthchecks (Seungmin may take over the compose / Dockerfile part, never contract or Gateway logic) |
| **Tue 11-10** | **Finding records:** contract `relayFindingRecord` (checked against `ruleEngineSigner`), `POST /v1/finding-records`, indexing of `FindingRecorded` into FINDING_RECORDED, the lookup Core uses |
| Wed 11-11 | M1 run |
| **Thu 11-12** | Rejection records completed: anchor_id link, `GET /v1/rejections/{id}`, what T04 needs; **T05 and T08 labels** (committed before Seungmin's scripts) |
| **Fri 11-13** | Measurements (mode, P, stop tests, deepest reorg) recorded in D-02 / D-03 / D-27 (D = 2 until then) |
| **Mon 11-16** | Rule Engine reads: `GET /v1/elections/{id}/state`, `/v1/anchors/{id}/leaves`, `/v1/nodes`, `/v1/verify`; ANCHOR_REJECTED alerts; `publishTally` and `finalizeElection` with the full contract condition (R1–R5 at TALLY, every FAIL / INCONCLUSIVE resolved or superseded, none CONFIRMED_ISSUE; only the all-PASS path is exercised before the reviews exist) |
| Wed 11-18 | M2 rehearsal |
| Thu 11-19 | **M2 demo** |
| **Fri 11-20** | **T02, T04, T15 labels** (T15 from Seungmin's injection memo of 11-18) |
| **Mon 11-23** | Proposals: `openProposal`, decisions with approval-signature checks, execution order, `amendRoster`, `voidElection`; ADMIN_ACTION_REQUESTED / APPROVAL_RECORDED / PROPOSAL_CLOSED (APPROVED only after all are stored), indexing of `ProposalDecided`; contract refusal of an unapproved ROSTER_AMENDED (T05). Endpoints and payloads exactly as `contracts/ledger-gateway.openapi.yaml` v1.1 (Seungmin's fake follows the same file); any change goes through `contracts/CHANGELOG.md` and is told to Seungmin the same day |
| **Wed 11-25** | Signed finding reviews (contract, relay, `FindingReviewed` → FINDING_REVIEWED), finalize quorum; T04 Gateway refusals, T15 (d) refusal; same OpenAPI rule as 11-23 |
| Thu 11-26 | M3 run |
| Fri 11-27 – Mon 11-30 | Fixes from the M3 review and the experiment runs |

### Anik — Rule Engine

| By | Work |
|---|---|
| **Mon 11-09** | **Rule Engine service against the real Core, Gateway mock:** discovery on `/internal/v1/elections` and `/checkpoints`, inputs from `/internal/v1/views*`, finding signing, body to Core `POST /internal/v1/findings`, AUDIT_DELAYED / LEDGER_UNAVAILABLE / CHAIN_CONFIG_MISMATCH alerts, `PINNED_NODE_URL` tried in order, items not yet D blocks deep treated as "not there yet"; **compose block** (`rule-engine` slot: healthcheck, `RULE_KEY`, `RULE_ENGINE_SIGNER_KEY`, URLs); a partly implemented rule writes "not implemented" in its `summary` |
| **Tue 11-10** | Same against the **real Gateway** (`POST /v1/finding-records`, then the body to Core) |
| Wed 11-11 | M1 run (T01, partial R4 / R5 allowed) |
| **Thu 11-12** | **Template report generator** (D-15 v1.2, works without any model): polls `GET /internal/v1/findings?has_report=false` with `AGENT_KEY` for FAIL / INCONCLUSIVE findings, collects evidence with the read-only tools, fills hypotheses from the B1 rule-ID → cause table, writes each schema-valid report as a file (keeps its own list of reported findings, since Core report storage is future work); read-only script that checks every evidence ref; **compose block** (`audit-agent` slot: `AGENT_KEY`, `CORE_URL`, `GATEWAY_URL` (most agent tools are Gateway reads), `AI_PROVIDER` / `AI_BASE_URL` / `AI_MODEL`, report volume; `AI_PROVIDER=none` by default; on a Linux host `extra_hosts: host.docker.internal:host-gateway`); **demo PC:** Anik picks the free local runtime and model (Anik's own choice, D-15; only free and local), installs them on Seungmin's PC (the demo PC; a short slot agreed with Seungmin that day) and records RAM / VRAM, free disk and the model size in D-15 |
| **Fri 11-13** | **AI check and go / no-go** (posted to the team; **at most half a day**, skipped with the template kept if R4 / R5 are behind): a free open-weight model run locally behind the same generator, on the demo PC prepared by Anik on 11-12. Test data: the INCONCLUSIVE / FAIL findings of the M1 run (real stack: tool calls and evidence refs resolve) and the FAIL findings of the simulation (cause hypotheses only, since their evidence lives in the fake Core / Gateway), plus a T05 / T08 FAIL if Seungmin's scripts already ran. Go if tool calls work, a report comes within 10 minutes, it validates against `audit-report.schema.json` and the evidence refs of the real-stack reports resolve; **a go is provisional** until 11-17; no-go → the template stays (AI is then future work) |
| **Mon 11-16** | R4 and R5 complete (pinned-node reads of anchors, registry and contract state; snapshots, row-to-ledger match via anchor leaves, sealed-hash check), re-runs, supersession, `previous_finding_id`, R1–R5 at the TALLY checkpoint |
| **Tue 11-17** | **Audit agent integrated** (only after R4 / R5; R4 / R5 come first): the `audit-agent` block merged; the provisional go is **confirmed** only if the local model's report for a real T05 / T08 FAIL finding validates, resolves every evidence ref (read-only script) and arrives within 10 minutes on the demo PC → `AI_PROVIDER` = Anik's runtime, `AI_MODEL` = the pinned model (name and digest in D-15), present on the demo PC before the rehearsal (the demo runs offline); otherwise, or on no-go, the template |
| Wed 11-18 | M2 rehearsal: E2 shows T05 → R4 / R5 FAIL, T08 (a)(b) → R5 FAIL, and an audit report for an E2 FAIL finding |
| Thu 11-19 | **M2 demo** |
| Fri 11-20 – Tue 11-24 | Audit agent fixes from the M2 review only while the 11-25 work below is not at risk |
| **Wed 11-25** | R4 approval-signature checks for admin actions; T02, T04, T15 give the labelled results |
| Thu 11-26 | M3 run |
| Fri 11-27 – Mon 11-30 | Experiment runs and tables (section 5) with Seungmin; fixes; B2 runs only if the go was confirmed on 11-17 |

### Seungmin — Core, web, integration

| By | Work |
|---|---|
| **Thu 11-05** | **D-28 PR** (this page, decisions, test-plan scope line, system-design 9 / roles 8 pointers, D-27 deadline) |
| Fri 11-06 | Public entry (D-29): compose `web` service (nginx: built web + Core `/v1/`, internal paths blocked, rate caps), `WEB_PORT` 3000 as the link base; Tailscale installed on the demo PC, Serve tried from a second device |
| Mon 11-09 | Tally (one ballot per accepted BALLOT_RECEIVED, key reveal at TALLY, `result_hash`, `ballot_set_root`), finalize (applied after ACCEPTED); compose blocks merged as they arrive, staff setup after registry setup |
| **Tue 11-10** | Web base: Vite + TypeScript, JS hashing (RFC 8785, SHA-256) with the shared vectors in CI, libsodium sealed box |
| Wed 11-11 | **M1 run** (integrator) and supervisor review |
| **Fri 11-13** | Voter web (token from the fragment, credential and sealed ballot in the browser, receipt and its check); T05 (DB edit) and T08 (a)(b) scripts |
| Sat 11-14 – Mon 11-16 | Part B against the fake Gateway, in this order: **(a)** E3 seed script (signed findings to the fake and to Core: one FAIL plus R1–R5 at TALLY, since the mock stack has no Rule Engine) and ROSTER_AMEND end to end in Core (create → submit → decisions → execute, re-check at execution); **(b)** review relay; **(c)** ELECTION_VOID. The fake Gateway gains proposals, decisions (EIP-191 signature check with the contract's digest, off chain; `eth-account` added to the Core image's dev requirements), finding reviews, `publishTally` / `finalizeElection` and the finalize quorum. Fake behaviour comes from `contracts/ledger-gateway.openapi.yaml` v1.1, never from what the UI happens to need, so the switch to Harits's Gateway changes no Core code |
| **Tue 11-17** | **Admin web, every screen:** login, create, roster, freeze, link CSV, dashboard (status, counters, alerts, findings), tally, finalize, **approval screen** (recomputes `target_hash` and digest, EIP-191 signing) and **review screen** |
| Wed 11-18 | **M2 rehearsal:** part A (real stack: E1, E2) and part B (mock stack: E3, approvals and reviews); morning run on a clean DB, afternoon supervisor review, evening fixes; demo script with timetable (including where the audit report is shown); images of both demo projects built beforehand and the start rehearsed offline (`up --no-build`); the Funnel address tried on the venue network (on at least 10 minutes early, shown on 11-19 only if it works, D-29); T15 injection memo to Harits |
| Thu 11-19 | **M2 demo** |
| Fri 11-20 | T15 script (first, so its results can be checked against Harits's label; the label is committed before the script's first run) |
| Mon 11-23 | Funnel test (D-29): a device on another network votes end to end through the Funnel address; link order (Funnel → `VOTER_WEB_URL` → Core → links) written in the README |
| **Tue 11-24** | Admin actions and reviews moved from the fake Gateway to the real one (Harits's proposals 11-23; reviews follow on 11-25), differences from the fake fixed on both sides |
| Wed 11-25 | `evidence_check`, outbox resume; T02, T04, T05 (API variant), T08 (c) scripts (the team lead may hand part of the scripts to Harits or Anik) |
| Thu 11-26 | **M3 run** and supervisor review |
| Fri 11-27 – Mon 11-30 | Experiment runs (section 5), Seungmin runs them, Anik builds the tables; fixes; README run steps (`docker compose up` → demo E1 + E2); everything finished by 11-30 |

## 4. Handoffs and what happens when one slips

| Needed by | What | From | By |
|---|---|---|---|
| M1 | T01 label | Harits | 11-06 |
| M1 | Gateway M1 minimum + compose blocks | Harits | 11-09 |
| M1 | Rule Engine service + compose block | Anik | 11-09 (real Gateway 11-10) |
| M1 | Finding records on chain | Harits | 11-10 |
| M2 | T05, T08 labels | Harits | 11-12 |
| M2 | Rule Engine reads, publishTally / finalize | Harits | 11-16 |
| M2 | R4 / R5 complete | Anik | 11-16 |
| M3 | T02, T04, T15 labels | Harits | 11-20 |
| M3 | Proposals + signature checks / reviews | Harits | 11-23 / 11-25 |
| M2 | Template report generator + `audit-agent` compose block | Anik | 11-12 |
| M2 | AI go / no-go (local model or template) | Anik | 11-13 |
| M2 | Audit agent integrated (local model if go) | Anik | 11-17 |

**Fallbacks, decided now (D-28) and switched on by one line in `docs/decisions.md` when needed:**
- A handoff one day late: the owner announces a new date the same day; the others re-order their own list.
- M1 items two days late: M1 runs with R1–R3 only; R4 / R5 move to the M2 rehearsal.
- M2 items two days late: E2 shows one fault (T08 (a)) instead of two; or the admin steps of E1 run as a script against
  the API while the voter web stays.
- Audit agent: the local model not ready on 11-17 → the demo shows the template report ("not AI"); R4 / R5 late on
  11-16 → AI work stops until they are done and the template stays; the template itself late → M2 is CONDITIONAL and
  the report generator moves to 11-24.
- Part B (approval / review screens) not working on 11-18: E3 shows what was finished in the order (a) → (b) → (c), at least ROSTER_AMEND, or the screens run their
  steps with the decisions sent by a script; part A is never delayed for part B.
- Seungmin's Funnel test (11-23, D-29) is dropped first if part B or the Gateway switch (11-24) is late; the demo
  stays local.
- Seungmin may take over integration glue (compose, Dockerfiles, scripts) from either owner, never contract, Gateway or
  rule logic.

## 5. Scope changes (D-28)

| Item | Before | Now |
|---|---|---|
| Optional features (WINDOW_CHANGE, ELECTION_CLOSE_EARLY, TOKEN_REISSUE, direct chain submissions, T03, T06, T07, T09–T14, T16) | Weeks 6–7 "whatever fits" | Not built; future work in the report |
| AI Audit Agent | Week 8 if time allows (recommended: paid Claude API) | **Zero API cost only (D-15 v1.2):** anything that costs money is dropped. **Part of the M2 demo:** template report generator by 11-12, a free open-weight model run locally checked on 11-13 (go / no-go), the agent integrated in compose by 11-17 and shown in E2 on 11-18 / 11-19; no-go → the template report ("not AI") is shown and AI is future work. R4 / R5 (11-16) and Anik's 11-25 work come first. Reports are files; Core report storage (`POST /internal/v1/audit-reports`, EVIDENCE_NOT_FOUND) and the report screen are **future work**, so threat-model A2 and test T12 are not implemented and the report says so |
| Experiment (test-plan) | Every scenario × seeds 1–10 in the evaluation configuration; baselines B0 / B1 / B2 | The 6 mandatory scenarios × seeds 1–3 first, 4–5 if time allows, in the **demo configuration** (about 15–20 min per run, scripted), 11-27 – 11-30; baseline B1; B2 (rules + local AI) only if the go was confirmed on 11-17, with its evidence validity measured offline by Anik's read-only script; the template report is not B2 and gets no AI metrics (its evidence is copied from the finding); B0 described qualitatively. API cost is 0 by construction; tokens, time to report, model digest, quantization, temperature, seed and hardware are recorded. Labels still before the first run. The report states that the evaluation configuration was not run |
| Web in the demo | Admin web of M2: core screens only; approval and review screens at M3 | **Every screen demonstrable at M2** (11-18 / 11-19): approval and review screens run against the fake Gateway in part B and against the real contract from M3 |
| Integration points | Weeks 3, 5, 7 | M1 11-11, M2 11-19, M3 11-26; everything finished by 11-30 |
| D-27 measurements | Due 11-09 | Due 11-13; D = 2 until then |
| Unchanged | Mandatory scope (normal vote, roster amendment, voiding, R1–R5, T01, T02, T04, T05, T08, T15), contracts v1.1, on-chain signature checks (D-11), anonymity rules | — |

## 6. Working rules

- **Daily:** each owner posts "done / next / blocked" once a day; a block that stops someone else is raised within the day.
- **Branches:** small PRs to `main`, merged within a day; `contracts/` changes by PR with the owners @mentioned.
- **Supervisor:** reviews at M1, M2, M3 (half a day before each run is reserved for it).
- **No new features after M3** (11-26); 11-27 – 11-30 only fixes and the experiment runs; everything finished by 11-30.
