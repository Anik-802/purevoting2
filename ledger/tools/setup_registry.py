"""Deploy PureVotingLedger (optional) and set up the on-chain registry from the keys in an env file.

  python3 -I setup_registry.py --env .env --rpc http://127.0.0.1:8541 --bin build/PureVotingLedger.bin \
        [--pinned-node-url http://observer:8545] [--contract 0x...] [--operator org_commission] \
        [--gateway-operated-by org_neutral] [--rule-engine-operated-by org_observer]

Steps (D-21, D-25, D-26, hashing.md "Registry and verification"):
  1. deploy the contract (or use --contract)
  2. each org's own node account registers that org's signer key
  3. operatorOrg, gatewayAccount and ruleEngineSigner are proposed by the operating org's node account and confirmed by
     the other registered orgs' node accounts
  4. read everything back and write CHAIN_ID, CONTRACT_ADDRESS and PINNED_NODE_URL to the env file
Safe to run again: steps already done are skipped; a conflicting entry stops the script. Keys are never printed.
Demo keys only (see ethsign.py).
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
import ethsign  # noqa: E402
from chain import decode, encode, idh, raw_rpc  # noqa: E402

ORGS = ["org_commission", "org_observer", "org_neutral"]
ERRORS = ["NotGateway", "NotRegisteredOrgAccount", "ZeroValue", "OrgAlreadyRegistered", "AccountAlreadyRegistered",
          "SignerAlreadyRegistered", "WrongPendingValue", "OrgNotRegistered"]


def sel(sig):
    return ethsign.keccak256(sig.encode())[:4].hex()


ERR_BY_SEL = {sel(n + "()"): n for n in ERRORS}


class Rpc:
    def __init__(self, url):
        self.url = url

    def __call__(self, method, params=None):
        import urllib.request
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read())
        if "error" in out:
            data = (out["error"].get("data") or "")
            name = ERR_BY_SEL.get(data[2:10]) if isinstance(data, str) else None
            raise RuntimeError(f"{method} failed: " + (f"revert {name}" if name else json.dumps(out["error"])))
        return out["result"]


class Account:
    def __init__(self, key_hex):
        self.priv = int(key_hex, 16)
        self.address = ethsign.address(self.priv)


def wait_receipt(rpc, h, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = rpc("eth_getTransactionReceipt", [h])
        except RuntimeError as e:
            if "indexing is in progress" not in str(e):
                raise
            r = None
        if r:
            return r
        time.sleep(0.4)
    raise RuntimeError("transaction not mined within %d s" % timeout)


def send(rpc, chain_id, acct, to, data, gas_price=0):
    msg = {"from": acct.address, "to": to, "data": "0x" + data.hex(), "gasPrice": hex(gas_price)} if to else \
        {"from": acct.address, "data": "0x" + data.hex(), "gasPrice": hex(gas_price)}
    gas = int(rpc("eth_estimateGas", [msg]), 16)          # a revert surfaces here with its error name
    nonce = int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
    raw = ethsign.sign_legacy_tx(acct.priv, chain_id, nonce, gas_price, gas * 12 // 10 + 21000, to, 0, data)
    r = wait_receipt(rpc, rpc("eth_sendRawTransaction", ["0x" + raw.hex()]))
    if r["status"] != "0x1":
        raise RuntimeError("transaction failed on chain: " + r["transactionHash"])
    return r


def call(rpc, to, sig, types, args, out_types):
    data = "0x" + sel(sig) + encode(types, args)
    return decode(out_types, rpc("eth_call", [{"to": to, "data": data}, "latest"]))


def read_env(path):
    vals, lines = {}, open(path, encoding="utf-8").read().splitlines()
    for ln in lines:
        m = re.match(r"^([A-Z0-9_]+)=(\S*)", ln)
        if m:
            vals[m.group(1)] = m.group(2)
    return vals, lines


def write_env(path, lines, updates):
    done = set()
    for i, ln in enumerate(lines):
        m = re.match(r"^([A-Z0-9_]+)=(\S*)(\s*#.*)?$", ln)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}{m.group(3) or ''}"
            done.add(m.group(1))
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def bytecode(path):
    text = open(path, encoding="utf-8").read().strip()
    if text.startswith("{"):                                   # Hardhat artifact
        text = json.loads(text)["bytecode"]
    return bytes.fromhex(text[2:] if text.startswith("0x") else text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--rpc", required=True)
    ap.add_argument("--bin")
    ap.add_argument("--contract")
    ap.add_argument("--pinned-node-url")
    ap.add_argument("--operator", default="org_commission")
    ap.add_argument("--gateway-operated-by", default="org_neutral")
    ap.add_argument("--rule-engine-operated-by", default="org_observer")
    a = ap.parse_args()
    if bool(a.bin) == bool(a.contract):
        sys.exit("give exactly one of --bin (deploy) or --contract (existing address)")

    env, lines = read_env(a.env)
    rpc = Rpc(a.rpc)
    chain_id = int(rpc("eth_chainId"), 16)
    node = {o: Account(env[f"ORG_{o[4:].upper()}_NODE_ACCOUNT_KEY"]) for o in ORGS}
    signer = {o: Account(env[f"ORG_{o[4:].upper()}_SIGNER_KEY"]) for o in ORGS}
    gateway, rule = Account(env["GATEWAY_ACCOUNT_KEY"]), Account(env["RULE_ENGINE_SIGNER_KEY"])
    step = [0]

    def say(msg):
        step[0] += 1
        print(f"[{step[0]:>2}] {msg}", flush=True)

    print(f"chain id {chain_id}, rpc {a.rpc}")
    # 1. contract
    if a.bin:
        r = send(rpc, chain_id, node[a.gateway_operated_by], None, bytecode(a.bin))
        contract = r["contractAddress"]
        say(f"deployed PureVotingLedger at {contract} (gas {int(r['gasUsed'], 16)})")
    else:
        contract = a.contract
        if rpc("eth_getCode", [contract, "latest"]) in ("0x", None):
            sys.exit(f"no contract code at {contract}")
        say(f"using existing contract {contract}")

    def tx(acct, sig, types, args, what):
        data = bytes.fromhex(sel(sig) + encode(types, args))
        r = send(rpc, chain_id, acct, contract, data)
        say(f"{what} (gas {int(r['gasUsed'], 16)})")

    # 2. organizations
    for o in ORGS:
        cur = call(rpc, contract, "orgSigner(bytes32)", ["bytes32"], [idh(o)], ["address", "address"])
        want = [signer[o].address.lower(), node[o].address.lower()]
        if cur[1] != "0x" + "00" * 20:
            if [c.lower() for c in cur] != want:
                sys.exit(f"{o} is already registered with different keys: refusing to continue")
            say(f"{o} already registered, skipped")
            continue
        tx(node[o], "registerOrgSigner(bytes32,address)", ["bytes32", "address"], [idh(o), signer[o].address],
           f"{o}: node account {node[o].address[:10]}.. registers signer {signer[o].address[:10]}..")

    def confirm_all(proposer, setter_sig, confirm_sig, types, arg, label, getter_sig, getter_out, value_index):
        cur = call(rpc, contract, getter_sig, [], [], getter_out)
        if cur[-1] and cur[value_index].lower() == arg.lower():
            say(f"{label} already confirmed by all, skipped")
            return
        tx(node[proposer], setter_sig, types, [arg], f"{label}: proposed by {proposer}")
        for o in ORGS:
            if o != proposer:
                tx(node[o], confirm_sig, types, [arg], f"{label}: confirmed by {o}")

    # 3. operator, gateway, rule engine
    confirm_all(a.operator, "setOperatorOrg(bytes32)", "confirmOperatorOrg(bytes32)", ["bytes32"], idh(a.operator),
                f"operatorOrg = {a.operator}", "operatorOrg()", ["bytes32", "bool"], 0)
    confirm_all(a.gateway_operated_by, "setGatewayAccount(address)", "confirmGatewayAccount(address)", ["address"],
                gateway.address, f"gatewayAccount = {gateway.address[:10]}.. (operated by {a.gateway_operated_by})",
                "gatewayAccount()", ["address", "bytes32", "bool"], 0)
    confirm_all(a.rule_engine_operated_by, "setRuleEngineSigner(address)", "confirmRuleEngineSigner(address)", ["address"],
                rule.address, f"ruleEngineSigner = {rule.address[:10]}.. (operated by {a.rule_engine_operated_by})",
                "ruleEngineSigner()", ["address", "bytes32", "bool"], 0)

    # 4. read back
    print("\nread back from the contract:")
    ok = True
    for o in ORGS:
        s, by = call(rpc, contract, "orgSigner(bytes32)", ["bytes32"], [idh(o)], ["address", "address"])
        good = s.lower() == signer[o].address.lower() and by.lower() == node[o].address.lower()
        ok &= good
        print(f"  {o:<15} signer {s}  registered by {by}  {'ok' if good else 'MISMATCH'}")
    op = call(rpc, contract, "operatorOrg()", [], [], ["bytes32", "bool"])
    gw = call(rpc, contract, "gatewayAccount()", [], [], ["address", "bytes32", "bool"])
    re_ = call(rpc, contract, "ruleEngineSigner()", [], [], ["address", "bytes32", "bool"])
    good = op[0] == idh(a.operator) and op[1]
    ok &= good
    print(f"  operatorOrg     {a.operator}  confirmed by all: {op[1]}  {'ok' if good else 'MISMATCH'}")
    good = gw[0].lower() == gateway.address.lower() and gw[1] == idh(a.gateway_operated_by) and gw[2]
    ok &= good
    print(f"  gatewayAccount  {gw[0]}  operated by {a.gateway_operated_by}  confirmed by all: {gw[2]}  {'ok' if good else 'MISMATCH'}")
    good = re_[0].lower() == rule.address.lower() and re_[1] == idh(a.rule_engine_operated_by) and re_[2]
    ok &= good
    print(f"  ruleEngineSigner {re_[0]}  operated by {a.rule_engine_operated_by}  confirmed by all: {re_[2]}  {'ok' if good else 'MISMATCH'}")
    if not ok:
        sys.exit("registry does not match the keys in the env file")

    pinned = a.pinned_node_url or a.rpc
    write_env(a.env, lines, {"CHAIN_ID": str(chain_id), "CONTRACT_ADDRESS": contract, "PINNED_NODE_URL": pinned})
    print(f"\nwrote CHAIN_ID={chain_id}, CONTRACT_ADDRESS={contract}, PINNED_NODE_URL={pinned} to {a.env}")
    if not a.pinned_node_url:
        print("note: --pinned-node-url not given, so the rpc URL was used. It must be a node NOT run by the Gateway operator (D-26).")


if __name__ == "__main__":
    main()
