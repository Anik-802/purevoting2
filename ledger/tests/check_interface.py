"""Checks that PureVotingLedger.sol matches the normative interface in contracts/chain-interface.md.
Every function with the same name must have the same selector and the same signature; functions of the interface
that are not implemented yet and extra functions are listed. Usage:
  python3 -I check_interface.py <repo root> [solc binary]   (stdlib only; solc default: 'solc' on PATH)"""
import json
import os
import re
import subprocess
import sys
import tempfile

root = sys.argv[1]
solc = sys.argv[2] if len(sys.argv) > 2 else "solc"
md = open(os.path.join(root, "contracts", "chain-interface.md"), encoding="utf-8").read()
src = re.search(r"```solidity\n(.*?)```", md, re.S).group(1)


def run(args, text):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.sol")
        open(p, "w").write(text)
        return subprocess.run([solc, "--evm-version", "shanghai", *args, p], capture_output=True, text=True, check=True).stdout


def hashes(text):
    """Selectors of the functions only (solc also lists custom errors in a second section)."""
    section = run(["--hashes"], text).split("Function signatures:")[1].split("Error signatures:")[0]
    out = {}
    for m in re.finditer(r"^([0-9a-f]{8}): (\w+\(.*\))$", section, re.M):
        out[m.group(2).split("(")[0]] = (m.group(1), m.group(2))
    return out


def events(text):
    # the interface cannot be compiled to an ABI by name, so ask solc for the abi JSON and rebuild event signatures
    out = run(["--abi"], text)
    sigs = {}
    for blob in re.findall(r"(\[\{.*?\}\])\s*$", out, re.M | re.S):
        for item in json.loads(blob):
            if item["type"] == "event":
                sigs[item["name"]] = item["name"] + "(" + ",".join(i["type"] for i in item["inputs"]) + ")"
    return sigs


impl_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contracts", "PureVotingLedger.sol")).read()
want, have = hashes(src), hashes(impl_src)
bad = [n for n in have if n in want and have[n] != want[n]]
missing = sorted(n for n in want if n not in have)
extra = sorted(n for n in have if n not in want)
ev_want, ev_have = events(src), events(impl_src)
ev_bad = [n for n in ev_have if n in ev_want and ev_have[n] != ev_want[n]]
print(f"interface functions: {len(want)}, implemented with identical selector: {len([n for n in have if n in want and n not in bad])}")
print("selector mismatches:", bad or "none")
print("not implemented yet:", ", ".join(missing))
print("additions (allowed):", ", ".join(extra))
print("event signature mismatches:", ev_bad or "none", "| interface events implemented:", sorted(set(ev_have) & set(ev_want)))
sys.exit(1 if bad or ev_bad else 0)
