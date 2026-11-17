from __future__ import annotations

import csv
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any


def write_reports(output: Path, result: dict[str, Any]) -> None:
    """Write actual simulation results without network requests or JavaScript."""
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (output / "rule_outcomes.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["seed", "case_id", "rule_id", "result", "expected_development_result", "assertion_matches", "implementation_coverage", "inconclusive_reason"])
        for case in result["cases"]:
            for outcome in case["outcomes"]:
                writer.writerow([case["seed"], case["case_id"], outcome["rule_id"], outcome["result"],
                                 case["expected"][outcome["rule_id"]], outcome["matches_expected"],
                                 outcome["coverage"], outcome["inconclusive_reason"] or ""])

    escape = html.escape
    vector_ok = sum(v["passed"] for v in result["hash_vectors"])
    assertion_ok = sum(o["matches_expected"] for c in result["cases"] for o in c["outcomes"])
    n_assertions = sum(len(c["outcomes"]) for c in result["cases"])
    rows = []
    for case in result["cases"]:
        by_rule = {o["rule_id"]: o for o in case["outcomes"]}
        cells = []
        for rule in ("R1", "R2", "R3", "R4", "R5"):
            o = by_rule[rule]
            label = o["result"] + (" *" if o["coverage"] == "partial" else "")
            cells.append(f'<td><span class="status {escape(o["result"].lower())}">{escape(label)}</span></td>')
        matched = all(o["matches_expected"] for o in case["outcomes"])
        rows.append(f'<tr><td>{case["seed"]}</td><td><strong>{escape(case["case_id"])}</strong><small>{escape(case["description"])}</small></td>'
                    + "".join(cells) + f'<td>{"MATCH" if matched else "MISMATCH"}</td></tr>')
    details = []
    for case in result["cases"]:
        if case["seed"] != result["seeds"][0]:
            continue
        blocks = []
        for o in case["outcomes"]:
            missing = ", ".join(o["unimplemented_checks"])
            blocks.append(f'<h4>{escape(o["rule_id"])} · {escape(o["result"])}</h4><p>{escape(o["summary"])}</p>'
                          + (f'<p class="muted">Pending checks: {escape(missing)}</p>' if missing else ""))
        details.append(f'<details><summary>{escape(case["case_id"])} — seed {case["seed"]}</summary>{"".join(blocks)}</details>')
    scheduler_rows = "".join(f'<tr><td>{escape(c["name"])}</td><td>{escape(str(c["decision"]["run"]))}</td>'
                             f'<td>{escape(str(c["decision"]["alert"] or "none"))}</td>'
                             f'<td>{escape(str(c["decision"]["anchor_timeout"]))}</td></tr>'
                             for c in result["scheduler_examples"])
    content = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PureVoting 2 Rules Local Simulation</title>
<style>
:root{{font-family:Arial,Helvetica,sans-serif;color:#172434;background:#f4f6f9;line-height:1.5}}body{{margin:0}}main{{max-width:1250px;margin:auto;padding:36px 28px 60px}}h1{{font-size:32px;line-height:1.15;margin:5px 0 14px}}h2{{font-size:21px;margin:30px 0 14px}}h4{{margin:15px 0 4px}}p{{margin:8px 0 14px}}.eyebrow{{text-transform:uppercase;letter-spacing:.12em;color:#52647a;font-size:12px;font-weight:bold}}.muted,small{{color:#58677a}}small{{display:block;font-size:12px;max-width:410px;font-weight:normal}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}}.stat{{background:white;border:1px solid #d8e0e9;border-radius:8px;padding:18px 22px}}.stat b{{font-size:28px;display:block}}.stat span{{font-size:13px;color:#536278}}table{{border-collapse:collapse;background:white;width:100%;font-size:13px}}th,td{{border:1px solid #dae1e9;padding:11px 12px;text-align:left;vertical-align:middle}}th{{background:#e8eef5;font-size:12px}}.table-wrap{{overflow:auto}}.status{{font-size:11px;font-weight:bold;white-space:nowrap}}.pass{{color:#246443}}.fail{{color:#a33132}}.inconclusive{{color:#8a5c08}}.scope{{border-left:4px solid #8a5c08;background:#fffaf0;padding:14px 18px;font-size:14px}}details{{background:white;border:1px solid #d8e0e9;margin:8px 0;padding:12px 16px}}summary{{cursor:pointer;font-weight:bold}}details p{{font-size:13px}}code{{font-family:Consolas,monospace;font-size:.95em}}footer{{margin-top:30px;font-size:12px;color:#58677a}}@media(max-width:700px){{.stats{{grid-template-columns:1fr}}main{{padding:24px 12px}}}}
</style></head><body><main>
<div class="eyebrow">Development evidence · version {escape(result["version"])}</div>
<h1>PureVoting 2 rules simulation</h1>
<p>Synthetic fixtures and in-process Core/Gateway mocks. No live election, blockchain transaction, or AI model was used.</p>
<p class="muted">Contracts v1.2.2 · implementation base {escape(result["upstream_commit"][:7])} · Python {escape(result["runtime"]["python"])} · seeds {escape(", ".join(map(str,result["seeds"])))}</p>
<div class="stats"><div class="stat"><b>{vector_ok}/{len(result["hash_vectors"])}</b><span>Independent hashing vectors matched</span></div>
<div class="stat"><b>{assertion_ok}/{n_assertions}</b><span>Development outcome assertions matched</span></div>
<div class="stat"><b>{result["findings_stored"]}</b><span>Finding bodies delivered to local mocks</span></div></div>
<div class="scope"><strong>Scope:</strong> R1–R5 have complete implementation coverage. These older fixtures deliberately omit independent chain evidence, so clean R4/R5 results remain INCONCLUSIVE. Complete-evidence TALLY tests are run separately by pytest. These development assertions are authored with the prototype; they are not Harits’s independent experimental labels. Official T01–T16 and production integration remain unverified.</div>
<h2>Observed rule outcomes</h2><p class="muted">FAIL means a rule detected a simulated violation. MATCH means the actual outcome equals that case’s development assertion. These are different measurements.</p>
<div class="table-wrap"><table><thead><tr><th>Seed</th><th>Development case</th><th>R1</th><th>R2</th><th>R3</th><th>R4</th><th>R5</th><th>Assertion</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<h2>Scheduler readiness examples</h2><table><thead><tr><th>Condition</th><th>Run rules</th><th>Alert</th><th>Anchor timeout</th></tr></thead><tbody>{scheduler_rows}</tbody></table>
<h2>Finding explanations and pending checks</h2>{"".join(details)}
<h2>Publication boundary</h2><p>Finding bodies were schema validated, persisted in SQLite, hashed, signed with an ephemeral EIP-191 test signer, registered through an in-process Gateway mock, and submitted to an in-process Core mock. Injected HTTP 503 responses exercised unchanged-body retries. This does not verify a deployed node, on-chain registration, quorum, or real network recovery.</p>
<footer>Run ID: {escape(result["run_id"])} · Created: {escape(result["created_at"])} · Raw results: results.json · Tabular results: rule_outcomes.csv<br>Evidence provenance: synthetic development cases. No production credentials are included.</footer>
</main></body></html>'''
    (output / "report.html").write_text(content, encoding="utf-8")

    counts = Counter(o["result"] for c in result["cases"] for o in c["outcomes"])
    # A compact exact vector graphic for weekly progress; all values come from this run.
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="760" viewBox="0 0 1400 760">
<rect width="1400" height="760" fill="#f4f6f9"/>
<g font-family="Arial, sans-serif" fill="#172434"><text x="60" y="65" font-size="18" fill="#58677a">PUREVOTING 2 · LOCAL DEVELOPMENT SIMULATION</text>
<text x="60" y="125" font-size="44" font-weight="bold">Rules implementation progress</text>
<text x="60" y="168" font-size="22">Synthetic fixtures · in-process mocks · no live chain or AI calls</text>
<rect x="60" y="210" width="400" height="150" fill="white" stroke="#dae1e9"/><rect x="500" y="210" width="400" height="150" fill="white" stroke="#dae1e9"/><rect x="940" y="210" width="400" height="150" fill="white" stroke="#dae1e9"/>
<text x="88" y="280" font-size="48" font-weight="bold">{vector_ok}/{len(result["hash_vectors"])}</text><text x="88" y="327" font-size="21">Independent hashing vectors</text>
<text x="528" y="280" font-size="48" font-weight="bold">{assertion_ok}/{n_assertions}</text><text x="528" y="327" font-size="21">Development assertions</text>
<text x="968" y="280" font-size="48" font-weight="bold">{result["findings_stored"]}</text><text x="968" y="327" font-size="21">Mock-delivered findings</text>
<text x="60" y="420" font-size="27" font-weight="bold">Coverage and limitations</text>
<text x="60" y="463" font-size="23">R1–R5 implemented; these fixtures omit independent chain evidence.</text>
<text x="60" y="506" font-size="23">Actual rule outcomes: {counts['PASS']} PASS · {counts['FAIL']} FAIL · {counts['INCONCLUSIVE']} INCONCLUSIVE</text>
<text x="60" y="549" font-size="21">FAIL is a detected fixture violation. MATCH is a development assertion result.</text>
<text x="60" y="602" font-size="22" font-weight="bold" fill="#8a5c08">Official scenario evaluation and live integration are not completed.</text>
<text x="60" y="652" font-size="18" fill="#58677a">Contracts v1.2.2 · implementation base {escape(result['upstream_commit'][:7])} · Python {escape(result['runtime']['python'])} · {len(result['cases'])} case runs</text>
<text x="60" y="695" font-size="18" fill="#58677a">Recorded results: results.json and rule_outcomes.csv</text></g></svg>'''
    (output / "progress.svg").write_text(svg, encoding="utf-8")
