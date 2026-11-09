# Chain measurements (D-02 / D-03 / D-27)

Produced by `ledger/tools/measure_chain.py` on a local private PureChain test network (Clique, 3 validators,
configured period 1 s, chainId 424242, confirmation depth D = 2 in Core). Raw data: `chain_2026-11-08.json`.

| Item | Result (2026-11-08, one run) |
|---|---|
| Block period, all validators up | 90 blocks in 90 s; chain timestamp gap mean 1 s, max 1 s; in-turn share 1.0 |
| Confirmation at D = 2 (derived) | about 2 s after inclusion |
| One validator stopped | chain continued: 60 blocks in 60 s; chain gap max 1 s; longest wall-clock stall 1.9 s |
| Two validators stopped | chain halted: 1 block in 45 s, no block for the last 44.9 s |
| Maximum observed reorg depth | 0 (3 nodes polled about once per second, last 8 blocks re-checked, whole run) |

Limits:
- One run on one machine; validators stopped with SIGTERM (graceful), not by a crash or a network cut.
- The reorg depth is an observation in this run, not a proven bound.
- Resume time after a validator restarts was not measured yet.
- A stall of about 1.9 s plus the 2 s confirmation can exceed the Gateway's 3.5 s wait (DECISIONS G2). The Gateway then
  answers RETRY_LATER and Core's retry gets DUPLICATE with the transaction info, which Core treats as success.

## Run 2 (same day, with `--restart`)

Raw data: `chain_2026-11-08_restart.json`. Phases A to C repeated run 1: one validator down kept 60 blocks in 60 s
(longest stall 1.9 s), two down gave 0 blocks in 45 s (halted), maximum observed reorg depth 0.

| Item | Result |
|---|---|
| Chain resumes after the first stopped validator restarts (two validators up) | 1.4 s after the start command |
| Validator down about 2 min (about 100 blocks behind) restarts, peers and catches up | 13.4 s after the start command (RPC up 0.3 s, peered 0.5 s) |

Reading the restart numbers:
- The first restart's "caught up 0.4 s" is not meaningful: the chain was halted, so the node was already at the head.
- The second restart's "chain resumed 1.1 s" is not meaningful: the chain was already running with two validators.
- Peering is manual (`peer.py`, as in `bringup.sh`) because the nodes run with `--nodiscover`. In the compose network the
  validators need static peers or a bootnode, or a restarted container will not rejoin on its own.
