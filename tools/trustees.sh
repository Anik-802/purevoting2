#!/bin/sh
# D-30 demo helper: one person plays all three organizations' trustees with the Core image. In a real deployment each
# organization runs `python -m app.scripts.trustee` on its own machine and keeps its secret file; when one person holds
# all three secrets there is no ballot secrecy against that person (contracts/ballot-elgamal.md section 5).
#
#   tools/trustees.sh keygen               make the three keys (once) and register them in Core's database
#   tools/trustees.sh decrypt <election>   after closing: each trustee checks the ballots against the Gateway's
#                                          ledger, then sends its share; the real Gateway needs AUDITOR_KEY (the
#                                          read-only key of .env) in the environment, the mock Gateway needs none
#
# COMPOSE picks the stack, e.g. COMPOSE="docker compose -f docker-compose.yml -f docker-compose.integration.yml".
# Keys go to ./trustee-keys (git-ignored) or TRUSTEE_KEYS_DIR.
set -eu
COMPOSE=${COMPOSE:-docker compose}
KEYS=${TRUSTEE_KEYS_DIR:-./trustee-keys}
ORGS="org_commission org_neutral org_observer"
run() { $COMPOSE run --rm --no-deps -T -v "$KEYS:/keys" -e GATEWAY_AUDITOR_KEY core python -m "$@"; }
export GATEWAY_AUDITOR_KEY="${AUDITOR_KEY:-}"

case "${1:-}" in
  keygen)
    mkdir -p "$KEYS"
    for org in $ORGS; do
      if [ ! -f "$KEYS/$org.secret.json" ]; then
        run app.scripts.trustee keygen --org "$org" --secret "/keys/$org.secret.json" > "$KEYS/$org.public.json"
      fi
      run app.scripts.register_trustee --file "/keys/$org.public.json"
    done
    ;;
  decrypt)
    [ -n "${2:-}" ] || { echo "usage: $0 decrypt <election_id>" >&2; exit 2; }
    for org in $ORGS; do
      run app.scripts.trustee decrypt --core http://core:8000 --gateway http://ledger-gateway:8100 \
        --election "$2" --secret "/keys/$org.secret.json"
    done
    ;;
  *)
    echo "usage: $0 keygen | decrypt <election_id>" >&2
    exit 2
    ;;
esac
