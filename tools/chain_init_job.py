"""chain-init job of docker-compose.integration.yml: makes the three PureChain validators on this machine, once.

For each node volume (/nodes/<org>): a random keystore password, one validator account (the Clique signer of that org,
D-03), a node key, the shared genesis and geth init, and a config.toml whose StaticNodes are the other two validators
at their fixed addresses on the chain network, so the nodes peer and rejoin after a restart without a script
(ledger/NETWORK.md sections 1 and 3). The genesis is the one of the kit's gen-genesis.sh with a 1 s period. Keys and
passwords stay in the volumes and are never printed; only the public signer addresses are. A chain that is already
initialised is left unchanged; a partly initialised one stops the job (remove the node volumes and run again).
"""
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

GETH = "/usr/local/bin/geth"
NODES = ["commission", "observer", "neutral"]   # validator 1..3, p2p address <prefix>.11 .. .13
P2P_PORT = 30303

# EIP-4788 beacon roots contract, as in the kit's gen-genesis.sh (Cancun is on from genesis)
BEACON_ROOTS = ("0x000F3df6D732807Ef1319fB7B8bB8522d0Beac02",
                "0x3373fffffffffffffffffffffffffffffffffffffffe14604d57602036146024575f5ffd5b5f35801560495762001fff81"
                "0690815414603c575f5ffd5b62001fff01545f5260205ff35b5f5ffd5b62001fff42064281555f359062001fff015500")

# secp256k1, only to derive each node's enode id (its public key) from the node key
_P = 2**256 - 2**32 - 977
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        m = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P)
    else:
        m = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P)
    x = (m * m - a[0] - b[0]) % _P
    return x, (m * (a[0] - x) - a[1]) % _P


def enode_id(key: bytes) -> str:
    k, q, r = int.from_bytes(key, "big"), _G, None
    while k:
        if k & 1:
            r = _add(r, q)
        q = _add(q, q)
        k >>= 1
    return r[0].to_bytes(32, "big").hex() + r[1].to_bytes(32, "big").hex()


def genesis(chain_id: int, signers: list[str]) -> dict:
    alloc = {a: {"balance": "1000000000000000000000"} for a in signers}
    alloc[BEACON_ROOTS[0]] = {"balance": "0", "code": BEACON_ROOTS[1]}
    forks = ["homestead", "eip150", "eip155", "eip158", "byzantium", "constantinople", "petersburg", "istanbul",
             "berlin", "london"]
    config = {"chainId": chain_id, **{f + "Block": 0 for f in forks}, "shanghaiTime": 0, "cancunTime": 0,
              "zeroBaseFee": True, "clique": {"period": 1, "epoch": 30000}}
    extra = "0x" + "00" * 32 + "".join(a[2:].lower() for a in signers) + "00" * 65
    return {"config": config, "difficulty": "1", "gasLimit": "0x1c9c380", "extradata": extra, "alloc": alloc}


def write_secret(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def main() -> int:
    want = os.environ["PURECHAIN_GETH_SHA256"].lower()
    with open(GETH, "rb") as f:
        got = hashlib.file_digest(f, "sha256").hexdigest()
    if got != want:
        print(f"{GETH} has sha256 {got}, expected {want} (purechain-geth v1.0.1 linux-amd64 of Harits's kit)",
              file=sys.stderr)
        return 1
    chain_id = int(os.environ["PURECHAIN_CHAIN_ID"])
    prefix = os.environ["CHAIN_NET_PREFIX"]
    dirs = [Path("/nodes", n) for n in NODES]

    done = [(d / "address").is_file() and (d / "geth" / "chaindata").is_dir() for d in dirs]
    if all(done):
        # StaticNodes and the genesis hold the values of the first run: a changed setting would leave the nodes unpeered
        for name, want in (("chain_id", str(chain_id)), ("net_prefix", prefix)):
            have = {(d / name).read_text() if (d / name).is_file() else None for d in dirs}
            if have != {want}:
                print(f"{name} of the existing chain is {' '.join(sorted(map(str, have)))}, not {want}: set it back,"
                      " or remove the three node volumes for a new chain", file=sys.stderr)
                return 1
        print("chain already initialised, validators:", " ".join((d / "address").read_text() for d in dirs))
        return 0
    if any(done) or any(any(d.iterdir()) for d in dirs):
        print("node volumes partly initialised: remove the three node volumes and run again", file=sys.stderr)
        return 1

    addrs, keys = [], []
    for d in dirs:
        write_secret(d / "password", secrets.token_hex(32))
        out = subprocess.run([GETH, "--datadir", str(d), "account", "new", "--password", str(d / "password")],
                             capture_output=True, text=True, check=True).stdout
        addrs.append(re.search(r"0x[0-9a-fA-F]{40}", out).group(0))
        keys.append(secrets.token_bytes(32))

    gen = json.dumps(genesis(chain_id, addrs), indent=2)
    peers = [f"enode://{enode_id(k)}@{prefix}.{11 + i}:{P2P_PORT}" for i, k in enumerate(keys)]
    for i, d in enumerate(dirs):
        (d / "genesis.json").write_text(gen)
        subprocess.run([GETH, "--datadir", str(d), "init", str(d / "genesis.json")], capture_output=True, check=True)
        write_secret(d / "geth" / "nodekey", keys[i].hex())
        static = ", ".join(json.dumps(p) for j, p in enumerate(peers) if j != i)
        (d / "config.toml").write_text(f"[Node.P2P]\nStaticNodes = [{static}]\n")
        (d / "chain_id").write_text(str(chain_id))
        (d / "net_prefix").write_text(prefix)
        (d / "address").write_text(addrs[i])
    print(f"chain {chain_id} initialised, validators (commission observer neutral):", " ".join(addrs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
