"""Demo: three calls the registry must refuse. Run after setup_registry.py.

  python3 -I demo_failures.py --env .env [--rpc http://127.0.0.1:8541]

Reads CONTRACT_ADDRESS and the keys from the env file written by gen_keys.py / setup_registry.py. The 'stranger' account
is a fresh random key on every run. Prints only the names of the reverts, never a key.
"""
import argparse
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
import ethsign  # noqa: E402
import setup_registry as s  # noqa: E402
from chain import encode, idh  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--rpc", default="http://127.0.0.1:8541")
    a = ap.parse_args()

    env, _ = s.read_env(a.env)
    rpc = s.Rpc(a.rpc)
    chain_id = int(rpc("eth_chainId"), 16)
    contract = env["CONTRACT_ADDRESS"]

    def attempt(label, acct, sig, types, args):
        try:
            s.send(rpc, chain_id, acct, contract, bytes.fromhex(s.sel(sig) + encode(types, args)))
            print(f"{label}: ACCEPTED (unexpected)")
        except RuntimeError as e:
            print(f"{label}: rejected -> {str(e).split('failed: ')[-1]}")

    while True:
        k = int.from_bytes(secrets.token_bytes(32), "big")
        if 1 <= k < ethsign.N:
            break
    stranger = s.Account("0x" + k.to_bytes(32, "big").hex())
    org = s.Account(env["ORG_OBSERVER_NODE_ACCOUNT_KEY"])
    attempt("1 unregistered account sets operatorOrg", stranger, "setOperatorOrg(bytes32)", ["bytes32"], [idh("org_commission")])
    attempt("2 org re-registers with another signer ", org, "registerOrgSigner(bytes32,address)",
            ["bytes32", "address"], [idh("org_observer"), stranger.address])
    attempt("3 confirm a value nobody proposed       ", org, "confirmGatewayAccount(address)", ["address"], [stranger.address])


if __name__ == "__main__":
    main()
