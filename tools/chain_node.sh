#!/bin/sh
# PureChain validator of docker-compose.integration.yml (start flags from ledger/NETWORK.md section 2): the volume at
# /data was made by chain-init (account, password, node key, genesis, config.toml with the other two validators as
# static peers). The validator key is unlocked for sealing, so the RPC offers only eth, net and web3 (no admin, miner,
# clique or txpool: no signer vote over RPC) and answers only the compose service names and localhost.
#
# Sealing does not start with geth. Validators that start together at the same height (a fresh chain, a restart of the
# host or of Docker, `down` / `up`) and seal at once make different next blocks; each node discards the others' blocks
# until its first sync, and Clique then stops for good ("signed recently, must wait for others"). Seen on fresh starts,
# even with the nodes peered first; the kit avoids it by starting the nodes one after another. So:
# - the validator with SEAL_AFTER=0 seals as soon as it has a peer;
# - the others seal once their chain has grown since start (they synced from a sealing peer), or after SEAL_AFTER
#   rounds of the wait loop (a little over a second each) without growth (the first validator is down: the chain
#   still starts, D-03 one validator may be down);
# - while waiting, a node drops and re-adds its peers over IPC after 5 s and then every 40 s: a block broadcast does
#   not start the sync before the first one, a new handshake with a peer whose chain is heavier does. A simultaneous
#   restart therefore takes about a minute until all three seal again (measured), a single restart a few seconds.
set -eu
D=/data
[ -f "$D/address" ] || { echo "$D/address missing: chain-init has not run for this volume" >&2; exit 1; }
addr=$(cat "$D/address")
geth --config "$D/config.toml" --datadir "$D" --networkid "$(cat "$D/chain_id")" --identity "$NODE_NAME" \
  --unlock "$addr" --password "$D/password" --allow-insecure-unlock \
  --miner.etherbase "$addr" --miner.gasprice 0 --txpool.pricelimit 0 \
  --syncmode full --gcmode archive --snapshot=false --nodiscover --port 30303 \
  --http --http.addr 0.0.0.0 --http.port 8545 --http.api eth,net,web3 \
  --http.vhosts node-commission,node-observer,node-neutral,localhost,127.0.0.1 &
pid=$!
trap 'kill -TERM "$pid"; wait "$pid"; exit $?' TERM INT

js() { timeout 5 geth attach --exec "$1" "$D/geth.ipc" 2>/dev/null || true; }
alive() { kill -0 "$pid" 2>/dev/null || { wait "$pid" || exit $?; exit 1; }; }

until [ -S "$D/geth.ipc" ] && start=$(js eth.blockNumber) && [ -n "$start" ]; do alive; sleep 1; done
after=${SEAL_AFTER:?}
t=0
until [ "$(js "net.peerCount >= 1 && ($after == 0 || eth.blockNumber > $start || $t >= $after)")" = true ]; do
  alive
  t=$((t + 1))
  if [ "$t" -eq 5 ] || [ $((t % 40)) -eq 0 ]; then   # geth redials a dropped peer only after about 35 s
    # removePeer also drops the node from the static set, so it is added back right away
    js 'admin.peers.forEach(function (p) { admin.removePeer(p.enode); admin.addPeer(p.enode) })' >/dev/null
  fi
  sleep 1
done
js 'miner.start()' >/dev/null
[ "$(js eth.mining)" = true ] || { echo "miner.start() failed" >&2; kill -TERM "$pid"; wait "$pid" || true; exit 1; }
echo "sealing from height $(js eth.blockNumber) (height at start $start, waited ${t} s)"
wait "$pid"
