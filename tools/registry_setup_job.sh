#!/bin/sh
# registry-setup job of docker-compose.integration.yml: waits until the node produces blocks, then runs Harits's
# ledger/tools/setup_registry.py on the env file mounted at /work/chain.env (it writes CHAIN_ID, CONTRACT_ADDRESS and
# PINNED_NODE_URL back). Deploys only while the env file has no CONTRACT_ADDRESS; otherwise it reuses that address, and
# setup_registry.py stops if there is no code there (a reset chain: empty CONTRACT_ADDRESS in .env and run again).
set -eu
ENV_FILE=/work/chain.env
RPC="${BOOTSTRAP_RPC_URL:?}"

i=0
until python -I -c '
import json, sys, urllib.request as u
body = b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_blockNumber\",\"params\":[]}"
r = u.urlopen(u.Request(sys.argv[1], body, {"Content-Type": "application/json"}), timeout=3)
sys.exit(0 if int(json.load(r)["result"], 16) > 0 else 1)
' "$RPC" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "no blocks from $RPC after 120 s" >&2
    exit 1
  fi
  sleep 2
done

addr=$(sed -n 's/^CONTRACT_ADDRESS=\(0x[0-9a-fA-F]\{40\}\).*/\1/p' "$ENV_FILE")
if [ -n "$addr" ]; then
  set -- --contract "$addr"
else
  set -- --bin /build/PureVotingLedger.bin
fi
exec python -I /ledger/tools/setup_registry.py --env "$ENV_FILE" --rpc "$RPC" \
  --pinned-node-url "${BOOTSTRAP_PINNED_NODE_URL:?}" "$@"
