# PureVoting 2.0

Online one-time-link voting with a multi-party audit ledger and a rule-first AI audit agent.
Design complete (contracts v1.0 frozen); implementation starts in `core/`, `web/`, `ledger/`, `rules/`. Start with `docs/system-design.md`, then your role design in `docs/roles/`.

- `docs/system-design.md` — overall system design: architecture, flows, roles, dev environment (Korean: `system-design.ko.md`)
- `docs/schedule.md` — **schedule: demo 2026-11-19, final 2026-12-04**, milestones and per-owner dates (D-28)
- `docs/decisions.md` — confirmed design decisions D-01–D-28 (Korean original: `decisions.ko.md`)
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
