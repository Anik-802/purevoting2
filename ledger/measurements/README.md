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
