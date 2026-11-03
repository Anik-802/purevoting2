"""Minimal JSON-RPC and ABI helpers for testing PureVotingLedger on a local PureChain network (standard library only)."""
import hashlib
import json
import re
import subprocess
import time
import urllib.request


def idh(text):
    """idh(id) = sha256(utf8(id)), the bytes32 ID encoding of contracts/hashing.md."""
    return "0x" + hashlib.sha256(text.encode()).hexdigest()


def raw_rpc(port, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def rpc(port, method, params=None):
    out = raw_rpc(port, method, params)
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def _enc_word(t, v):
    if t == "bytes32":
        return bytes.fromhex(v[2:]).rjust(32, b"\0") if isinstance(v, str) else bytes(v).rjust(32, b"\0")
    if t == "address":
        return bytes.fromhex(v[2:]).rjust(32, b"\0")
    if t == "bool":
        return int(bool(v)).to_bytes(32, "big")
    if t.startswith("uint"):
        return int(v).to_bytes(32, "big")
    raise ValueError(t)


def encode(types, values):
    head, tail = b"", b""
    base = 32 * len(types)
    for t, v in zip(types, values):
        if t == "bytes32[]":
            head += (base + len(tail)).to_bytes(32, "big")
            tail += len(v).to_bytes(32, "big") + b"".join(_enc_word("bytes32", x) for x in v)
        else:
            head += _enc_word(t, v)
    return (head + tail).hex()


def decode(types, data_hex):
    data = bytes.fromhex(data_hex[2:])
    out = []
    for i, t in enumerate(types):
        w = data[32 * i: 32 * i + 32]
        if t == "bytes32[]":
            off = int.from_bytes(w, "big")
            n = int.from_bytes(data[off: off + 32], "big")
            out.append(["0x" + data[off + 32 + 32 * k: off + 64 + 32 * k].hex() for k in range(n)])
        elif t == "bytes32":
            out.append("0x" + w.hex())
        elif t == "address":
            out.append("0x" + w[-20:].hex())
        elif t == "bool":
            out.append(int.from_bytes(w, "big") != 0)
        else:
            out.append(int.from_bytes(w, "big"))
    return out


def solc_hashes(solc, sol_path):
    text = subprocess.run([solc, "--evm-version", "shanghai", "--hashes", sol_path], capture_output=True, text=True, check=True).stdout
    fn_sec, err_sec = text.split("Function signatures:")[1].split("Error signatures:")
    fns = {m.group(2).split("(")[0]: m.group(1) for m in re.finditer(r"^([0-9a-f]{8}): (\w+\(.*\))$", fn_sec, re.M)}
    errs = {m.group(1): m.group(2).split("(")[0] for m in re.finditer(r"^([0-9a-f]{8}): (\w+\(.*\))$", err_sec, re.M)}
    return fns, errs


class Contract:
    def __init__(self, port_of, abi, fns, errs, address=None):
        self.port_of, self.abi, self.fns, self.errs, self.address = port_of, abi, fns, errs, address
        self.by_name = {e["name"]: e for e in abi if e["type"] == "function"}

    def _data(self, name, args):
        types = [i["type"] for i in self.by_name[name]["inputs"]]
        return "0x" + self.fns[name] + encode(types, args)

    def call(self, name, *args, sender=None):
        """Read call. Returns the decoded outputs, or the name of the custom error if it reverted."""
        sender = sender or next(iter(self.port_of))
        port = self.port_of[sender]
        out = raw_rpc(port, "eth_call", [{"from": sender, "to": self.address, "data": self._data(name, args)}, "latest"])
        if "error" in out:
            return "revert:" + self.errs.get(out["error"].get("data", "0x")[2:10], out["error"].get("data", out["error"]["message"]))
        outs = [o["type"] for o in self.by_name[name]["outputs"]]
        res = decode(outs, out["result"]) if outs else []
        return res[0] if len(res) == 1 else res

    def send(self, name, *args, sender):
        """State-changing call. Returns ('ok', receipt) or ('revert:ErrorName', None). A dry run first gives the revert reason."""
        port = self.port_of[sender]
        data = self._data(name, args)
        dry = raw_rpc(port, "eth_call", [{"from": sender, "to": self.address, "data": data}, "latest"])
        if "error" in dry:
            d = dry["error"].get("data", "0x")
            return "revert:" + self.errs.get(d[2:10], d), None
        h = rpc(port, "eth_sendTransaction", [{"from": sender, "to": self.address, "data": data, "gas": hex(3_000_000), "gasPrice": "0x0"}])
        for _ in range(120):
            try:
                r = rpc(port, "eth_getTransactionReceipt", [h])
            except RuntimeError as e:
                if "indexing is in progress" not in str(e):
                    raise
                r = None
            if r:
                return ("ok" if r["status"] == "0x1" else "failed-onchain"), r
            time.sleep(0.25)
        raise RuntimeError("no receipt for " + name)


def deploy(port, sender, bin_hex):
    h = rpc(port, "eth_sendTransaction", [{"from": sender, "data": "0x" + bin_hex, "gas": hex(6_000_000), "gasPrice": "0x0"}])
    for _ in range(120):
        try:
            r = rpc(port, "eth_getTransactionReceipt", [h])
        except RuntimeError as e:
            if "indexing is in progress" not in str(e):
                raise
            r = None
        if r:
            assert r["status"] == "0x1", "deployment failed"
            return r["contractAddress"], int(r["gasUsed"], 16)
        time.sleep(0.25)
    raise RuntimeError("deployment not mined")


def block_time(port):
    return int(rpc(port, "eth_getBlockByNumber", ["latest", False])["timestamp"], 16)


def wait_block_time(port, t, timeout=90):
    end = time.time() + timeout
    while block_time(port) < t:
        if time.time() > end:
            raise RuntimeError("block time did not reach %d" % t)
        time.sleep(0.3)
