#!/usr/bin/env python3
"""Measure the local PureChain test network for D-02 / D-03 / D-27 (standard library only).

Phases (default about 3.5 minutes, nothing is restarted):
  A  baseline: all validators up        -> block period, in-turn share
  B  validator on --kill-first stopped   -> does the chain continue, longest gap
  C  validator on --kill-second stopped  -> only one validator left, does the chain halt
A watcher polls every node about once per second during all phases and counts how many of the last
--depth blocks change hash between polls (an observed reorg depth).

Run:  python3 -I ledger/tools/measure_chain.py
Afterwards the network is damaged on purpose: run `bash ~/pv/up.sh` to rebuild it.
Limits: the reorg figure is what was observed in this run only, not a proven bound; validators are
stopped with SIGTERM (graceful), not by a crash or a network cut.
"""
import argparse
import json
import os
import signal
import statistics
import subprocess
import time
import urllib.request


def rpc(port, method, params=None, timeout=3):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


def head(port):
    try:
        return int(rpc(port, "eth_blockNumber"), 16)
    except Exception:
        return None


def block(port, n):
    try:
        return rpc(port, "eth_getBlockByNumber", [hex(n), False])
    except Exception:
        return None


class Watcher:
    """Counts blocks whose hash at an already seen height changed (a reorg seen from that node)."""

    def __init__(self, ports, depth):
        self.ports, self.depth = ports, depth
        self.seen = {p: {} for p in ports}
        self.max_reorg = 0
        self.events = []

    def poll(self):
        for p in self.ports:
            h = head(p)
            if h is None:
                continue
            changed = 0
            for n in range(max(1, h - self.depth + 1), h + 1):
                b = block(p, n)
                if not b:
                    continue
                old = self.seen[p].get(n)
                if old and old != b["hash"]:
                    changed += 1
                self.seen[p][n] = b["hash"]
            if changed:
                self.max_reorg = max(self.max_reorg, changed)
                self.events.append({"port": p, "head": h, "replaced": changed, "at": round(time.time(), 1)})


def chain_stats(port, h0, h1):
    """Block timestamp gaps (chain seconds) and the in-turn share (difficulty 2) for blocks h0+1..h1."""
    deltas, inturn, total, prev = [], 0, 0, None
    for n in range(h0, h1 + 1):
        b = block(port, n)
        if not b:
            continue
        t = int(b["timestamp"], 16)
        if prev is not None:
            deltas.append(t - prev)
            total += 1
            inturn += int(b["difficulty"], 16) == 2
        prev = t
    if not deltas:
        return {"blocks": 0}
    return {"blocks": len(deltas), "gap_mean_s": round(statistics.mean(deltas), 2), "gap_min_s": min(deltas),
            "gap_max_s": max(deltas), "in_turn_share": round(inturn / total, 2) if total else None}


def observe(port, seconds, watcher):
    """Watch one node's head for `seconds`; returns blocks gained and the longest wall-clock stall."""
    start = time.time()
    h0 = head(port)
    last, last_t, gaps, next_poll = h0, start, [], start
    while time.time() - start < seconds:
        h, now = head(port), time.time()
        if h is not None and last is not None and h > last:
            gaps.append(now - last_t)
            last, last_t = h, now
        elif h is not None and last is None:
            last, last_t = h, now
        if now >= next_poll:
            watcher.poll()
            next_poll = now + 1.0
        time.sleep(0.2)
    end = time.time()
    tail = end - last_t
    gained = (last - h0) if (h0 is not None and last is not None) else None
    return {"seconds": round(end - start), "start_head": h0, "end_head": last, "blocks_gained": gained,
            "longest_stall_s": round(max(gaps + [tail]), 1), "stalled_at_end_s": round(tail, 1)}


def stop(port):
    pids = subprocess.run(["pgrep", "-f", "--", f"--http.port {port}"], capture_output=True, text=True).stdout.split()
    pids = [int(x) for x in pids if int(x) != os.getpid()]
    for x in pids:
        os.kill(x, signal.SIGTERM)
    t = time.time()
    while time.time() - t < 20 and head(port) is not None:
        time.sleep(0.3)
    return {"port": port, "pids": pids, "stopped": head(port) is None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default="8541,8542,8543", help="validator RPC ports")
    ap.add_argument("--observe-port", type=int, default=8541, help="node that stays up and is watched")
    ap.add_argument("--kill-first", type=int, default=8543)
    ap.add_argument("--kill-second", type=int, default=8542)
    ap.add_argument("--baseline", type=int, default=90)
    ap.add_argument("--one-down", type=int, default=60)
    ap.add_argument("--two-down", type=int, default=45)
    ap.add_argument("--depth", type=int, default=8, help="blocks re-checked per node and poll")
    ap.add_argument("--out", default="/tmp/measure_chain.json")
    a = ap.parse_args()
    ports = [int(x) for x in a.ports.split(",")]

    down = [p for p in ports if head(p) is None]
    if down:
        raise SystemExit(f"nodes not answering on {down}: run bash ~/pv/up.sh first")
    try:
        signers = rpc(a.observe_port, "clique_getSigners")
    except Exception:
        signers = None
    watcher = Watcher(ports, a.depth)
    res = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "validators": len(signers) if signers else None}

    print(f"A  baseline {a.baseline} s, all {len(ports)} validators up ...", flush=True)
    r = observe(a.observe_port, a.baseline, watcher)
    r["chain"] = chain_stats(a.observe_port, r["start_head"], r["end_head"])
    res["A_baseline"] = r

    print(f"B  stopping validator on {a.kill_first}, watching {a.one_down} s ...", flush=True)
    res["B_stop"] = stop(a.kill_first)
    r = observe(a.observe_port, a.one_down, watcher)
    r["chain"] = chain_stats(a.observe_port, r["start_head"], r["end_head"])
    res["B_one_down"] = r

    print(f"C  stopping validator on {a.kill_second}, watching {a.two_down} s ...", flush=True)
    res["C_stop"] = stop(a.kill_second)
    res["C_two_down"] = observe(a.observe_port, a.two_down, watcher)

    res["max_observed_reorg_depth"] = watcher.max_reorg
    res["reorg_events"] = watcher.events[:20]
    with open(a.out, "w") as f:
        json.dump(res, f, indent=2)

    A, B, C = res["A_baseline"], res["B_one_down"], res["C_two_down"]
    print("\n==== result (paste to Sumin) ====")
    print(f"validators: {res['validators']}   confirmation depth D = 2 (Core setting)")
    print(f"A all up:    {A['blocks_gained']} blocks in {A['seconds']} s; chain gap mean {A['chain'].get('gap_mean_s')} s,"
          f" max {A['chain'].get('gap_max_s')} s; longest wall-clock stall {A['longest_stall_s']} s;"
          f" in-turn share {A['chain'].get('in_turn_share')}")
    print(f"             derived: confirmation of a block at D = 2 needs about 2 x {A['chain'].get('gap_mean_s')} s"
          f" = {round(2 * (A['chain'].get('gap_mean_s') or 0), 1)} s after its inclusion")
    print(f"B 1 down:    stopped={res['B_stop']['stopped']}; {B['blocks_gained']} blocks in {B['seconds']} s;"
          f" chain gap max {B['chain'].get('gap_max_s')} s; longest stall {B['longest_stall_s']} s")
    print(f"C 2 down:    stopped={res['C_stop']['stopped']}; {C['blocks_gained']} blocks in {C['seconds']} s;"
          f" stalled at end {C['stalled_at_end_s']} s (halted if blocks stop)")
    print(f"max observed reorg depth (last {a.depth} blocks, all phases): {res['max_observed_reorg_depth']}")
    print(f"details: {a.out}")
    print("\nThe network is now damaged on purpose. Rebuild it with: bash ~/pv/up.sh")


if __name__ == "__main__":
    main()
