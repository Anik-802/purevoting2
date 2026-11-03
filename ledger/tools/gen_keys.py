"""Generate fresh demo keys into an env file (the key lines of .env.example).

  python3 -I gen_keys.py [--env .env] [--force]

Only lines that are empty or still '<set>' are filled; real values are kept unless --force. Other lines and comments are
left untouched. Keys are written to the file only (mode 600), never printed; the printed table shows addresses.
Fresh keys for every deployment (hashing.md "Replay across deployments"). Demo and test keys only.
"""
import argparse
import os
import re
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ethsign  # noqa: E402

KEYS = [
    "ORG_COMMISSION_SIGNER_KEY", "ORG_OBSERVER_SIGNER_KEY", "ORG_NEUTRAL_SIGNER_KEY",
    "RULE_ENGINE_SIGNER_KEY", "GATEWAY_ACCOUNT_KEY",
    "ORG_COMMISSION_NODE_ACCOUNT_KEY", "ORG_OBSERVER_NODE_ACCOUNT_KEY", "ORG_NEUTRAL_NODE_ACCOUNT_KEY",
    "RULE_ENGINE_CHAIN_ACCOUNT_KEY",
]


def new_key() -> str:
    while True:
        k = int.from_bytes(secrets.token_bytes(32), "big")
        if 1 <= k < ethsign.N:
            return "0x" + k.to_bytes(32, "big").hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--force", action="store_true", help="replace keys that already have a real value")
    a = ap.parse_args()

    lines = open(a.env, encoding="utf-8").read().splitlines() if os.path.exists(a.env) else []
    state, seen = {}, set()
    out = []
    for ln in lines:
        m = re.match(r"^([A-Z0-9_]+)=(\S*)(\s*#.*)?$", ln)
        if m and m.group(1) in KEYS:
            name, val, comment = m.group(1), m.group(2), m.group(3) or ""
            seen.add(name)
            if val in ("", "<set>") or a.force:
                val = new_key(); state[name] = "generated"
            else:
                state[name] = "kept"
            ln = f"{name}={val}{comment}"
        out.append(ln)
    for name in KEYS:
        if name not in seen:
            out.append(f"{name}={new_key()}"); state[name] = "generated"

    fd = os.open(a.env, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(a.env, 0o600)

    values = {}
    for ln in out:
        m = re.match(r"^([A-Z0-9_]+)=(\S*)", ln)
        if m:
            values[m.group(1)] = m.group(2)
    keys = [int(values[n], 16) for n in KEYS]
    if len(set(keys)) != len(keys):
        sys.exit("duplicate keys in the env file: every key must be different")
    width = max(len(n) for n in KEYS)
    print(f"{'key':<{width}}  {'address':<42}  state")
    for n, k in zip(KEYS, keys):
        print(f"{n:<{width}}  {ethsign.address(k):<42}  {state[n]}")
    print(f"written to {a.env} (mode 600). Do not commit it.")


if __name__ == "__main__":
    main()
