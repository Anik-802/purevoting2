# PureVoting 2.0

Online one-time-link voting with a multi-party audit ledger and a rule-first AI audit agent.
Design complete (contracts v1.0 frozen); implementation starts in `core/`, `web/`, `ledger/`, `rules/`. Start with `docs/system-design.md`, then your role design in `docs/roles/`.

- `docs/system-design.md` — overall system design: architecture, flows, roles, dev environment (Korean: `system-design.ko.md`)
- `docs/schedule.md` — **schedule: demo 2026-11-19 (everything finished by 2026-11-30)**, milestones and per-owner dates (D-28)
- `docs/decisions.md` — confirmed design decisions D-01–D-29 (Korean original: `decisions.ko.md`)
- `docs/roles/` — role designs: `core-web.md` (Seungmin), `ledger.md` (Harits), `rules-ai.md` (Anik)
- `docs/threat-model.md` — assets, threats, mitigations and claims we must not make
- `docs/test-plan.md` — how defect tests T01–T16 (mandatory: T01, T02, T04, T05, T08, T15) will be run and judged
- `tests/vectors/hash-vectors.json` — shared hash test vectors every module must reproduce (`python tools/make_hash_vectors.py --check`)
- `contracts/` — interface contracts between components (start with `contracts/README.md`)
- `tools/validate_contracts.py` — checks schemas, OpenAPI files and examples (run by CI)
- `.claude/agents/psp1-project-supervisor.md` — project supervisor agent (review only)

```bash
pip install -r tools/requirements.txt
python tools/validate_contracts.py
```

## Public access (D-29)

The stack runs on one host (the demo PC). The compose `web` service is its only entry: the built web plus Core's
`/v1/` on `http://127.0.0.1:${WEB_PORT}` (3000); internal endpoints and API docs are blocked there.

1. Install Tailscale (free Personal plan) on the host (Windows: from an administrator account) and log in; in the admin
   console enable MagicDNS and HTTPS certificates; allow Funnel when the CLI asks. Record `tailscale version`.
2. `tailscale funnel --bg 3000` (everyone) or `tailscale serve --bg 3000` (team members only) → note the
   `https://<host>.<tailnet>.ts.net` address. Public DNS can take up to 10 minutes, so turn it on early.
3. Set `VOTER_WEB_URL` to that address in `.env`, then start (or restart) Core and issue links. Do not change the address
   while an election is open.
4. Afterwards: `tailscale funnel reset` (or `tailscale serve reset`) and check `tailscale funnel status` shows nothing
   (`--bg` settings survive a reboot). Use demo keys and a fake roster only.
5. If staff cannot log in because of outside attempts (the caps are site-wide), turn the Funnel off.
