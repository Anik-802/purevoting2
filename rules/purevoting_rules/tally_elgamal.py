"""R5 ElGamal checks from contracts/ballot-elgamal.md section 6 (v1.2.2).

Inputs are complete, checkpoint-bound views and accepted events. This helper
performs no HTTP or Core import. Its non-zero counters demonstrate violations;
the caller retains the common R5 snapshot, row, hash and pinned-chain checks.
"""
from __future__ import annotations

import base64
import binascii
from collections import Counter, defaultdict

from . import elgamal as eg
from .hashing import H


TALLY_KEYS = ("tally_invalid_set_mismatches", "tally_share_failures", "tally_result_mismatches")


def _decode64(value: str) -> bytes:
    raw = base64.b64decode(value, validate=True)
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("noncanonical base64")
    return raw


def _event_ref(event: dict) -> dict[str, str]:
    return {"kind": "ledger_event", "ref": event["event_id"]}


def audit_elgamal(
    config: dict, election_id: str, events: list[dict], rows: list[dict],
    checkpoint: int, is_tally: bool,
) -> tuple[dict, str | None, list[dict], str | None]:
    """Return (observed, unavailable_reason, evidence_refs, detail).

All inputs are read-only. The caller must count every non-zero observed value
as a FAIL, even when another check is unavailable. Missing evidence is reported
explicitly; a null counter never means the corresponding check passed.
    """
    observed = {"key_mismatch": None, **dict.fromkeys(TALLY_KEYS)}
    refs = [{"kind": "view_row", "ref": f"election_config:config@{checkpoint}"}]
    selected = [e for e in events if e.get("election_id") == election_id and e["checkpoint"] <= checkpoint]
    created = [e for e in selected if e["type"] == "ELECTION_CREATED"]
    if len(created) != 1:
        return observed, "OTHER", refs, "Exactly one ELECTION_CREATED is required for ElGamal key verification."
    refs.append(_event_ref(created[0]))
    try:
        organizations = [o["org_id"] for o in config["organizations"]]
        if len(organizations) < 3 or len(set(organizations)) != len(organizations):
            raise ValueError("invalid organization set")
        organization_ids = sorted(organizations)
        candidates = sorted(c["candidate_id"] for c in config["candidates"])
        if not 1 <= len(candidates) <= eg.MAX_CANDIDATES or len(candidates) != len(set(candidates)):
            raise ValueError("invalid candidate set")
    except (KeyError, TypeError, ValueError):
        return observed, "VIEW_UNAVAILABLE", refs, "ElGamal organization or candidate configuration is unavailable or malformed."

    # Each of the four specified key items contributes independently. The
    # record-list item counts once; each invalid proof counts once.
    records = config.get("trustee_keys")
    records = records if isinstance(records, list) else []
    record_ids = [r.get("org_id") if isinstance(r, dict) else None for r in records]
    key_failures = int(record_ids != organization_ids)
    key_failures += sum(not eg.verify_trustee_key(r) for r in records)
    try:
        Y = _decode64(config["public_key"])
    except (KeyError, TypeError, ValueError, binascii.Error):
        Y = b""
    try:
        sum_matches = eg.valid_point(Y) and eg.joint_key(records) == Y
    except (KeyError, TypeError, ValueError, RuntimeError):
        sum_matches = False
    key_failures += int(not sum_matches)
    key_failures += int(len(Y) != 32 or H(Y) != created[0]["payload"]["public_key_hash"])
    observed["key_mismatch"] = key_failures
    if key_failures or not is_tally:
        return observed, None, refs, None

    tallies = [e for e in selected if e["type"] == "TALLY_PUBLISHED" and e["checkpoint"] == checkpoint]
    if len(tallies) != 1:
        detail = ("TALLY_PUBLISHED missing at the TALLY checkpoint" if not tallies
                  else "more than one TALLY_PUBLISHED at the TALLY checkpoint")
        refs.extend(_event_ref(e) for e in tallies)
        return observed, "OTHER", refs, detail
    tally = tallies[0]
    refs.append(_event_ref(tally))
    final_checkpoint = tally["payload"]["final_checkpoint"]
    accepted = [e for e in selected if e["type"] == "BALLOT_RECEIVED" and e["checkpoint"] <= final_checkpoint]
    rows_by_event: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        rows_by_event[row["event_id"]].append(row)
    invalid, valid = [], []
    for event in accepted:
        payload = event["payload"]
        matches = rows_by_event[event["event_id"]]
        ciphertexts = None
        # Duplicate rows are contradictory evidence; no arbitrary row wins.
        if len(matches) == 1:
            row = matches[0]
            if (row["row_key"], row["ballot_hash"], row["receipt_hash"]) == (
                    payload["nullifier_hash"], payload["ballot_hash"], payload["receipt_hash"]):
                try:
                    sealed = _decode64(row["sealed_ballot"])
                    if H(sealed) == payload["ballot_hash"]:
                        ciphertexts = eg.verify_ballot(sealed, Y, election_id, payload["nullifier_hash"], len(candidates))
                except (TypeError, ValueError, binascii.Error):
                    pass
        if ciphertexts is None:
            invalid.append(payload["ballot_hash"])
            refs.append(_event_ref(event))
        else:
            valid.append(ciphertexts)
    actual_invalid = Counter(invalid)
    published_invalid = Counter(tally["payload"]["invalid_ballot_hashes"])
    observed["tally_invalid_set_mismatches"] = sum((actual_invalid - published_invalid).values()) + sum((published_invalid - actual_invalid).values())

    shares = config.get("decryption_shares")
    if shares is None:
        return observed, "KEY_NOT_REVEALED", refs, "ElGamal decryption_shares is null at the TALLY checkpoint."
    if not isinstance(shares, list):
        return observed, "VIEW_UNAVAILABLE", refs, "ElGamal decryption_shares is not an available share list."

    if not valid:
        observed["tally_share_failures"] = len(shares)
        if shares:
            return observed, None, refs, None
        votes: list[int | None] = [0] * len(candidates)
    else:
        aggregate = eg.aggregate(valid, len(candidates))
        by_org: dict[str, list[dict]] = defaultdict(list)
        unknown = 0
        for share in shares:
            if not isinstance(share, dict) or share.get("org_id") not in organization_ids:
                unknown += 1
            else:
                by_org[share["org_id"]].append(share)
        keys = {r["org_id"]: eg.hex32(r["public_key"]) for r in records}
        failures, verified = unknown, []
        for org_id in organization_ids:
            org_shares = by_org[org_id]
            if len(org_shares) != 1 or not eg.verify_share(org_shares[0], keys[org_id], election_id, aggregate):
                failures += 1
            else:
                verified.append(org_shares[0])
        observed["tally_share_failures"] = failures
        if failures:
            return observed, None, refs, None
        # A failed bounded discrete-log search counts for that candidate only.
        # Build the complete 0..|V| table, including the identity for zero.
        counts, point = {}, eg.IDENTITY
        for count in range(len(valid) + 1):
            counts[point] = count
            point = eg.add(point, eg.G)
        votes = []
        for j, (_, B) in enumerate(aggregate):
            decoded = eg.sub(B, eg.total([eg.hex32(s["shares"][j]["d"]) for s in verified]))
            votes.append(counts.get(decoded))

    results = tally["payload"]["results"]
    frequencies = Counter(r["candidate_id"] for r in results)
    # Every entry bearing a duplicated/unknown candidate_id is a violation.
    mismatches = sum(r["candidate_id"] not in candidates or frequencies[r["candidate_id"]] > 1 for r in results)
    published: dict[str, int] = {}
    for result in results:
        published.setdefault(result["candidate_id"], result["votes"])
    mismatches += sum(count is None or count != published.get(candidate, 0) for candidate, count in zip(candidates, votes))
    observed["tally_result_mismatches"] = mismatches
    return observed, None, refs, None
