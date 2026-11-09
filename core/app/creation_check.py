"""Creation check (chain-interface.md "Creation check"): compare the on-chain creation fields, read directly from the
pinned node, with the ELECTION_CREATED payload. Until PASSED, Core delivers no further outbox event of the election and
refuses freeze / issue / admin actions.

Modes (CREATION_CHECK_MODE):
- `node` (default, required for T01): read electionState / electionGovernance from the pinned node (app/chain.py).
  - node's latest block < created_block + CONFIRMATION_DEPTH → PENDING (not confirmed there yet, D-27);
  - no pinned node answers completely for 60 s → LEDGER_UNAVAILABLE alert, still PENDING;
  - configHash == 0 for 60 s after the block was confirmed → MISMATCH "not found";
  - any immutable creation field differs → MISMATCH (field names in the detail).
  Gateway GET /v1/chain is compared with the pinned configuration once per election (CHAIN_CONFIG_MISMATCH alert only).
- `stub` (dev only, until the real Gateway joins the stack): the node is assumed to hold exactly the ELECTION_CREATED
  values. Core logs a warning at startup.

The scheduler runs the check (`check`): network reads happen outside any transaction, and only the result is applied
under the election lock. The 60 s timers live in memory: after a restart they start again, which only delays an alert
or a MISMATCH.
"""
import logging

from sqlalchemy import text

from app import timeutil
from app.chain import NodeUnavailable, get_reader
from app.config import settings
from app.db import get_engine
from app.gateway import GatewayUnavailable, get_gateway
from app.hashing import idh
from app.store import load_election, raise_alert
from app.timeutil import from_dt

log = logging.getLogger("core.creation_check")

GRACE = 60   # seconds: unreachable node → LEDGER_UNAVAILABLE; confirmed but not found → MISMATCH

_since: dict[str, dict[str, int]] = {}   # election_id -> {"unreachable": t, "not_found": t}
_config_checked: set[str] = set()         # elections whose Gateway /v1/chain was compared


def node_config_missing() -> bool:
    """Node mode without the pinned chain configuration: every creation check would stay PENDING."""
    return settings.creation_check_mode != "stub" and (not settings.pinned_node_urls or not settings.contract_address)


def warn_if_stub() -> None:
    if settings.creation_check_mode == "stub":
        log.warning("CREATION_CHECK_MODE=stub: the creation check always passes (dev only; must be 'node' for T01)")
    elif node_config_missing():
        log.warning("CREATION_CHECK_MODE=node but PINNED_NODE_URL or CONTRACT_ADDRESS is empty: "
                    "every creation check stays PENDING")


def expected(e: dict) -> dict:
    """The immutable creation fields as the contract stores them (bytes32 IDs = idh, hex without 0x)."""
    return {"config_hash": e["config_hash"].lower(), "window_start": e["window_start"],
            "window_end": from_dt(e["config_window_end"]), "time_bucket_seconds": e["time_bucket_seconds"],
            "checkpoint_interval_seconds": e["checkpoint_interval_seconds"],
            "org_ids": [idh(o).hex() for o in sorted(org["org_id"] for org in e["organizations"])],
            "approval_threshold": e["approval_threshold"], "operator_org_id": idh(e["operator_org"]).hex(),
            "proposal_ttl_hours": e["proposal_ttl_hours"]}


def compare(e: dict, state: dict, gov: dict) -> list[str]:
    """Names of the creation fields whose on-chain value differs from ELECTION_CREATED (operatorOrgId comes from both
    getters; each copy is compared)."""
    return [k for k, v in expected(e).items() if any(d[k] != v for d in (state, gov) if k in d)]


def _node_result(e: dict, now: int) -> tuple[str, str | None]:
    """(PENDING | PASSED | MISMATCH, detail). Raises NodeUnavailable if no pinned node answers completely."""
    eid = e["election_id"]
    _, state, gov = get_reader().read_election(eid, e["created_block"], settings.confirmation_depth)
    if state is None:
        return "PENDING", None
    if int(state["config_hash"], 16) == 0:
        first = _since.setdefault(eid, {}).setdefault("not_found", now)
        return ("MISMATCH", "not found") if now - first >= GRACE else ("PENDING", None)
    _since.get(eid, {}).pop("not_found", None)
    diff = compare(e, state, gov)
    return ("MISMATCH", "differs: " + ", ".join(diff)) if diff else ("PASSED", None)


def _same_chain_id(a, b: str) -> bool:
    try:
        return int(str(a), 0) == int(b, 0)          # "7777" and "0x1e61" are the same chain
    except ValueError:
        return str(a) == b


def chain_config_diff() -> list[str] | None:
    """Fields of Gateway GET /v1/chain that differ from the pinned configuration; None if the Gateway did not answer."""
    try:
        chain = get_gateway().chain()
    except GatewayUnavailable:
        return None
    diff = []
    if not _same_chain_id(chain.get("chain_id"), settings.chain_id):
        diff.append("chain_id")
    if str(chain.get("contract_address", "")).lower() != settings.contract_address.lower():
        diff.append("contract_address")
    if chain.get("confirmation_depth") is not None and chain["confirmation_depth"] != settings.confirmation_depth:
        diff.append("confirmation_depth")
    # every pinned node must be one of the network's nodes the Gateway lists (same names on the compose network)
    listed = {str(u).rstrip("/") for u in chain.get("rpc_urls") or []}
    if any(u.rstrip("/") not in listed for u in settings.pinned_node_urls):
        diff.append("rpc_urls")
    return diff


def check(election_id: str) -> None:
    """One attempt for an election whose ELECTION_CREATED was ACCEPTED / DUPLICATE (created_block is known)."""
    engine = get_engine()
    with engine.connect() as conn:
        e = load_election(conn, election_id)
    if e["creation_check"] != "PENDING" or e.get("created_block") is None:
        return
    now = timeutil.clock.now()
    config_diff = unreachable = None
    if settings.creation_check_mode == "stub":
        result, detail = "PASSED", None
    else:
        if election_id not in _config_checked:
            config_diff = chain_config_diff()
            if config_diff is not None:
                _config_checked.add(election_id)
        try:
            result, detail = _node_result(e, now)
            _since.get(election_id, {}).pop("unreachable", None)
        except NodeUnavailable as exc:
            result, detail = "PENDING", None
            first = _since.setdefault(election_id, {}).setdefault("unreachable", now)
            if now - first >= GRACE:
                unreachable = f"pinned node unreachable: {exc}"

    with engine.begin() as conn:
        e = load_election(conn, election_id, lock=True)
        if config_diff:
            raise_alert(conn, election_id, 0, "CHAIN_CONFIG_MISMATCH", "core-api", now,
                        "Gateway /v1/chain differs: " + ", ".join(config_diff))
        if unreachable:
            raise_alert(conn, election_id, 0, "LEDGER_UNAVAILABLE", "core-api", now, unreachable)
        if e["creation_check"] != "PENDING" or result == "PENDING":
            return
        _since.pop(election_id, None)
        conn.execute(text("UPDATE credential.elections SET creation_check = :c WHERE election_id = :e"),
                     {"c": result, "e": election_id})
        if result == "MISMATCH":
            raise_alert(conn, election_id, 0, "GOVERNANCE_MISMATCH", "core-api", now, detail)
