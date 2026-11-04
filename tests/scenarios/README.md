# tests/scenarios/ — fault scenarios and ground-truth labels

One folder per scenario **variant**: `tests/scenarios/<id>/` — each variant with a different expected result is its own
folder (`T04-core`, `T04-injected`, `T08a`, `T08b`, `T08c`, `T15a` … `T15d`, …), containing

- `setup` — how the scenario is prepared and the fault injected (script or step list) — written by **Seungmin**;
- `expected.json` — the ground-truth label — written by **Harits**.

## Labels (`expected.json`)

```json
{
  "scenario": "T05",
  "rules": { "R1": "PASS", "R2": "PASS", "R3": "PASS", "R4": "FAIL", "R5": "FAIL" },
  "observed_nonzero": { "R4": ["unapproved_changes"], "R5": ["tables_mismatched"] },
  "cause_code": "<one value of common.schema.json#/$defs/CauseCode>"
}
```

- A variant that the contract refuses before any finding (e.g. T15d without injection) labels the refusal with
  `"refused_by": "contract"` (or `"core"`, `"gateway"`) and the rules as evaluated afterwards.
- Each rule is exactly one of PASS / FAIL / INCONCLUSIVE (no "and/or"); `observed_nonzero` lists the `observed` keys
  (contracts/README.md "Audit rules") expected to be non-zero.
- **Owner:** Harits for T01, T02, T04, T05, T08, T15 (Anik never writes labels). **Deadline:** `docs/schedule.md` (D-28). Committed **before** the first run and never edited after seeing results.
- The B1 rule-ID → cause mapping table (`docs/test-plan.md`) is written by the same person, together with the labels.
- Mandatory scenarios: T01, T02, T04, T05, T08, T15; the rest are optional.
- Record the commit hash, seed and configuration (evaluation or demo) of every run.
