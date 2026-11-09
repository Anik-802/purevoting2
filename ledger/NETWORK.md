# Running the ledger network (for the compose files)

Everything below was run on a local 3-validator test network (kit `purechain-geth` v1.0.1, Clique, chainId 424242,
period 1 s). Parts marked "proposed" were not run in containers yet.

## 1. Accounts, genesis, node keys (generated on every fresh machine, never committed)

The kit script `setup_net.sh` does this; the same steps for a compose setup:

1. Create one account per validator (3 validators, one per organization) and one standby:
   `purechain-geth --datadir <dir>/n<i> account new --password <pwfile>`
   The password file and the keystores are secrets of that machine and must not be committed.
2. Build the genesis with the three validator addresses as signers and a 1 s period:
   `bash repo/network/gen-genesis.sh 424242 <addr1> <addr2> <addr3> | sed 's/"period": 5/"period": 1/' > genesis.json`
3. Initialise every node: `purechain-geth --datadir <dir>/n<i> init genesis.json`

## 2. Validator start command (from `start_node.sh`, node number i = 1..3)

    purechain-geth --datadir <dir>/n<i> --networkid 424242 --identity PV-Validator-<i> \
      --unlock <addr_i> --password <pwfile> --allow-insecure-unlock \
      --mine --miner.etherbase <addr_i> --miner.gasprice 0 --txpool.pricelimit 0 \
      --syncmode full --gcmode archive --snapshot=false --nodiscover \
      --port $((30300+i)) --authrpc.port $((8550+i)) \
      --http --http.addr 127.0.0.1 --http.vhosts "*" --http.port $((8540+i)) \
      --http.api eth,net,web3,admin,miner,txpool,clique

- `--http.vhosts "*"` is required: without it geth answers `403 invalid host specified` to any client that does not use
  the host name `localhost` (Core's direct node reads, the Gateway).
- In a container `--http.addr` must be `0.0.0.0` (proposed); publish the RPC port only on the compose network, because
  `admin` and `miner` are in `--http.api` (test network only).
- Gas price is 0 on this network, so the registry and Gateway accounts need no funding.
- The observer's pinned node is the validator of org_observer; `PINNED_NODE_URL` lists the observer's node first, then
  the commission's (D-03).

## 3. How the nodes find each other

`--nodiscover` is set, so nothing finds peers by itself. The kit connects them by hand after starting (`bringup.sh`,
`peer.py <new node RPC port> <existing node RPC port>`): read `admin_nodeInfo` of the other node and call
`admin_addPeer` with its enode. A node that restarts does not rejoin on its own (measured: peering needed after a
restart, then catch-up of about 100 blocks took 13.4 s).

Proposed for compose (not tested): give each node a fixed node key (keep `geth/nodekey` in its volume), put the three
enodes (`enode://<id>@<service name>:<30300+i>`) in `static-nodes.json` of every node, so restarts rejoin without a script.

## 4. Contract, keys, registry (all in `ledger/tools/`)

    solc --evm-version shanghai --optimize --optimize-runs 200 --bin -o out --overwrite ledger/contracts/PureVotingLedger.sol   # solc 0.8.28
    cp .env.example .env
    python3 -I ledger/tools/gen_keys.py --env .env          # fills the empty key lines, mode 600, never prints keys
    python3 -I ledger/tools/setup_registry.py --env .env --rpc http://<validator RPC> --bin out/PureVotingLedger.bin \
        --pinned-node-url <observer node URL>

- `gen_keys.py` writes: ORG_COMMISSION/OBSERVER/NEUTRAL_SIGNER_KEY, RULE_ENGINE_SIGNER_KEY, GATEWAY_ACCOUNT_KEY,
  ORG_COMMISSION/OBSERVER/NEUTRAL_NODE_ACCOUNT_KEY, RULE_ENGINE_CHAIN_ACCOUNT_KEY. Use fresh keys for every deployment.
- `setup_registry.py` deploys the contract (or uses `--contract 0x...` for an existing one), registers the organizations
  and the Gateway and Rule Engine accounts, reads everything back, and is safe to run again.
- CONTRACT_ADDRESS is passed on in the env file: `setup_registry.py` writes `CHAIN_ID`, `CONTRACT_ADDRESS` and
  `PINNED_NODE_URL` into the file given with `--env`. The address depends on the deployer key and nonce, so it is the
  same on a fresh chain with the same keys but changes with new keys. It is not printed as a separate output.
- The Gateway reads the same env file (or the same variables): CHAIN_ID, CONTRACT_ADDRESS, GATEWAY_ACCOUNT_KEY,
  GATEWAY_RPC_URL (default: first PINNED_NODE_URL), PINNED_NODE_URL, CONFIRMATION_DEPTH, CORE_KEY, RULE_KEY, AGENT_KEY,
  AUDITOR_KEY, GATEWAY_KEY, CORE_URL, ANCHOR_BATCH_SECONDS, ANCHOR_BATCH_MAX_EVENTS (see `ledger/gateway/compose-snippet.yml`).
- Order in compose: validators up and peered, then the registry job (needs the nodes mining), then the Gateway.

## 5. Measured behaviour

See `ledger/measurements/README.md`: 1 s blocks, chain continues with one validator down, halts with two, reorg depth 0.
