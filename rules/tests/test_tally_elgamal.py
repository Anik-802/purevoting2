"""Contract v1.2.2 R5 outcomes, including the five mandated edge cases."""
import base64
from copy import deepcopy
import json
from pathlib import Path

import pytest

from purevoting_rules import elgamal as eg
from purevoting_rules.hashing import H
from purevoting_rules.tally_elgamal import audit_elgamal, TALLY_KEYS
from elgamal_test_support import encrypt_ballot, decryption_share


VECTORS = json.loads((Path(__file__).resolve().parents[2] / "tests/vectors/elgamal-vectors.json").read_text(encoding="utf-8"))
ELECTION = VECTORS["election_id"]


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def vector_inputs():
    config = {
        "public_key": b64(bytes.fromhex(VECTORS["joint_key"])),
        "organizations": [{"org_id": t["org_id"], "signer": "0x" + str(i) * 40} for i, t in enumerate(VECTORS["trustees"], 1)],
        "candidates": [{"candidate_id": f"cand_{i}", "name": f"Candidate {i}"} for i in range(VECTORS["candidate_count"])],
        "trustee_keys": deepcopy([t["record"] for t in VECTORS["trustees"]]),
        "decryption_shares": deepcopy([s["share"] for s in VECTORS["shares"]]),
    }
    events = [{"election_id": ELECTION, "checkpoint": 0, "event_id": "created", "type": "ELECTION_CREATED",
               "payload": {"public_key_hash": H(bytes.fromhex(VECTORS["joint_key"]))}}]
    rows = []
    for i, ballot in enumerate(VECTORS["ballots"]):
        raw = bytes.fromhex(ballot["sealed_ballot"])
        payload = {"nullifier_hash": ballot["nullifier_hash"], "ballot_hash": H(raw), "receipt_hash": H(f"receipt{i}")}
        event = {"election_id": ELECTION, "checkpoint": 2, "event_id": f"ballot_{i}", "type": "BALLOT_RECEIVED", "payload": payload}
        events.append(event)
        rows.append({"row_key": ballot["nullifier_hash"], "event_id": event["event_id"], "ballot_hash": H(raw),
                     "receipt_hash": payload["receipt_hash"], "sealed_ballot": b64(raw)})
    events.append({"election_id": ELECTION, "checkpoint": 3, "event_id": "tally", "type": "TALLY_PUBLISHED",
                   "payload": {"final_checkpoint": 2, "invalid_ballot_hashes": [],
                               "results": [{"candidate_id": f"cand_{i}", "votes": v} for i, v in enumerate(VECTORS["votes"])]}})
    return config, events, rows


def run(config, events, rows, is_tally=True):
    return audit_elgamal(config, ELECTION, events, rows, 3, is_tally)


def recompute_fixture_tally(config, events, rows):
    """Test-side consistent tally after replacing selected ballot fixtures."""
    Y = base64.b64decode(config["public_key"])
    valid, invalid = [], []
    for event in (e for e in events if e["type"] == "BALLOT_RECEIVED" and e["checkpoint"] <= 2):
        row = next((r for r in rows if r["event_id"] == event["event_id"]), None)
        ciphertext = None if row is None else eg.verify_ballot(base64.b64decode(row["sealed_ballot"]), Y,
                                                              ELECTION, event["payload"]["nullifier_hash"], len(config["candidates"]))
        (invalid if ciphertext is None else valid).append(event["payload"]["ballot_hash"] if ciphertext is None else ciphertext)
    if valid:
        aggregate = eg.aggregate(valid, len(config["candidates"]))
        shares = [decryption_share(int.from_bytes(bytes.fromhex(t["secret"]), "little"), t["org_id"], ELECTION, aggregate)
                  for t in VECTORS["trustees"]]
        counts = eg.combine(aggregate, shares, len(valid))
    else:
        shares, counts = [], [0] * len(config["candidates"])
    config["decryption_shares"] = shares
    tally = next(e for e in events if e["type"] == "TALLY_PUBLISHED")["payload"]
    tally["invalid_ballot_hashes"] = invalid
    tally["results"] = [{"candidate_id": c["candidate_id"], "votes": v} for c, v in zip(config["candidates"], counts)]


def test_clean_shared_vector_tally_and_no_mutation():
    arguments = vector_inputs()
    original = deepcopy(arguments)
    observed, reason, refs, detail = run(*arguments)
    assert observed == {"key_mismatch": 0, **dict.fromkeys(TALLY_KEYS, 0)}
    assert reason is None and detail is None
    assert {"kind": "ledger_event", "ref": "tally"} in refs
    assert arguments == original


def test_keys_checked_before_tally_and_later_fields_are_null():
    config, events, rows = vector_inputs()
    config["decryption_shares"] = None
    observed, reason, _, _ = run(config, events, rows, is_tally=False)
    assert observed == {"key_mismatch": 0, **dict.fromkeys(TALLY_KEYS)}
    assert reason is None


@pytest.mark.parametrize("kind", ["order", "proof", "joint_key", "commitment"])
def test_key_discrepancies_fail_and_skip_tally(kind):
    config, events, rows = vector_inputs()
    if kind == "order":
        config["trustee_keys"].reverse()
    elif kind == "proof":
        config["trustee_keys"][0]["proof"]["z"] = "00" * 32
    elif kind == "joint_key":
        config["public_key"] = b64(eg.base(30))
        events[0]["payload"]["public_key_hash"] = H(eg.base(30))
    else:
        events[0]["payload"]["public_key_hash"] = "00" * 32
    observed, reason, _, _ = run(config, events, rows)
    assert observed["key_mismatch"] == 1
    assert all(observed[k] is None for k in TALLY_KEYS)
    assert reason is None


@pytest.mark.parametrize("count,detail", [(0, "TALLY_PUBLISHED missing"), (2, "more than one")])
def test_missing_or_multiple_tally_is_other_with_explanation(count, detail):
    config, events, rows = vector_inputs()
    tally = events.pop()
    events.extend(deepcopy(tally) for _ in range(count))
    observed, reason, _, message = run(config, events, rows)
    assert reason == "OTHER" and detail in message
    assert all(observed[k] is None for k in TALLY_KEYS)


def test_null_shares_is_key_not_revealed_after_invalid_set_check():
    config, events, rows = vector_inputs()
    config["decryption_shares"] = None
    observed, reason, _, _ = run(config, events, rows)
    assert reason == "KEY_NOT_REVEALED"
    assert observed["tally_invalid_set_mismatches"] == 0
    assert observed["tally_share_failures"] is None
    assert observed["tally_result_mismatches"] is None


def test_invalid_ballot_proof_is_excluded_and_false_invalid_set_is_detected():
    config, events, rows = vector_inputs()
    raw = bytearray(base64.b64decode(rows[0]["sealed_ballot"]))
    raw[-1] ^= 1
    rows[0]["sealed_ballot"], rows[0]["ballot_hash"] = b64(raw), H(bytes(raw))
    events[1]["payload"]["ballot_hash"] = rows[0]["ballot_hash"]
    recompute_fixture_tally(config, events, rows)
    assert all(v == 0 for v in run(config, events, rows)[0].values())
    events[-1]["payload"]["invalid_ballot_hashes"] = []
    assert run(config, events, rows)[0]["tally_invalid_set_mismatches"] == 1


def test_tampered_decryption_share_fails_and_results_are_not_claimed_checked():
    config, events, rows = vector_inputs()
    config["decryption_shares"][0]["shares"][0]["z"] = "00" * 32
    observed, reason, _, _ = run(config, events, rows)
    assert observed["tally_share_failures"] == 1
    assert observed["tally_result_mismatches"] is None
    assert reason is None


@pytest.mark.parametrize("kind,expected", [("missing", 1), ("duplicate", 1), ("unknown", 1), ("missing_and_unknown", 2)])
def test_share_failures_count_organizations_and_unknown_entries(kind, expected):
    config, events, rows = vector_inputs()
    if kind in {"missing", "missing_and_unknown"}:
        config["decryption_shares"].pop()
    if kind == "duplicate":
        config["decryption_shares"].append(deepcopy(config["decryption_shares"][0]))
    if kind in {"unknown", "missing_and_unknown"}:
        config["decryption_shares"].append(config["decryption_shares"][0] | {"org_id": "org_extra"})
    observed, reason, _, _ = run(config, events, rows)
    assert observed["tally_share_failures"] == expected
    assert observed["tally_result_mismatches"] is None
    assert reason is None


def test_forged_results_are_detected_independently_of_shares():
    config, events, rows = vector_inputs()
    events[-1]["payload"]["results"][0]["votes"] += 1
    observed, reason, _, _ = run(config, events, rows)
    assert observed["tally_result_mismatches"] == 1
    assert observed["tally_share_failures"] == 0
    assert reason is None


def test_unknown_and_duplicated_result_entries_each_count():
    config, events, rows = vector_inputs()
    results = events[-1]["payload"]["results"]
    results.append(deepcopy(results[0]))
    results.append({"candidate_id": "cand_unknown", "votes": 0})
    assert run(config, events, rows)[0]["tally_result_mismatches"] == 3


def test_identity_aggregate_requires_identity_shares_and_reproduces_counts():
    config, events, rows = vector_inputs()
    config["candidates"] = config["candidates"][:2]
    Y = base64.b64decode(config["public_key"])
    seqs = [[19, 23, 29, 31, 37, 41, 43, 47, 53], [eg.L - 19, 59, 61, 67, eg.L - 37, 71, 73, 79, 83]]
    events = [events[0], *events[1:3], events[-1]]
    rows = rows[:2]
    for i, scalars in enumerate(seqs):
        seq = iter(scalars)
        raw = encrypt_ballot(Y, ELECTION, rows[i]["row_key"], 2, i, lambda: next(seq))
        rows[i]["sealed_ballot"], rows[i]["ballot_hash"] = b64(raw), H(raw)
        events[i + 1]["payload"]["ballot_hash"] = H(raw)
    recompute_fixture_tally(config, events, rows)
    assert all(s["d"] == eg.IDENTITY.hex() for share in config["decryption_shares"] for s in share["shares"])
    assert [r["votes"] for r in events[-1]["payload"]["results"]] == [1, 1]
    observed, reason, _, _ = run(config, events, rows)
    assert all(v == 0 for v in observed.values()) and reason is None
    config["decryption_shares"][0]["shares"][0]["d"] = eg.G.hex()
    assert run(config, events, rows)[0]["tally_share_failures"] == 1


@pytest.mark.parametrize("has_invalid_ballot", [False, True])
def test_zero_valid_ballots_needs_no_shares_and_zero_results(has_invalid_ballot):
    config, events, rows = vector_inputs()
    events = [events[0], *([events[1]] if has_invalid_ballot else []), events[-1]]
    rows = rows[:1] if has_invalid_ballot else []
    if rows:
        raw = b"invalid ciphertext"
        rows[0]["sealed_ballot"], rows[0]["ballot_hash"] = b64(raw), H(raw)
        events[1]["payload"]["ballot_hash"] = H(raw)
    recompute_fixture_tally(config, events, rows)
    observed, reason, _, _ = run(config, events, rows)
    assert config["decryption_shares"] == []
    assert all(v == 0 for v in observed.values()) and reason is None
    # Omitted candidates in results are defined as zero by section 6.
    events[-1]["payload"]["results"] = []
    assert run(config, events, rows)[0]["tally_result_mismatches"] == 0
    config["decryption_shares"] = deepcopy([s["share"] for s in VECTORS["shares"]])
    observed, _, _, _ = run(config, events, rows)
    assert observed["tally_share_failures"] == 3 and observed["tally_result_mismatches"] is None


def test_invalid_set_is_a_multiset_not_a_set():
    config, events, rows = vector_inputs()
    events = [events[0], *events[1:3], events[-1]]
    rows = rows[:2]
    for row, event in zip(rows, events[1:3]):
        row["sealed_ballot"], row["ballot_hash"] = b64(b"invalid"), H(b"invalid")
        event["payload"]["ballot_hash"] = H(b"invalid")
    recompute_fixture_tally(config, events, rows)
    assert events[-1]["payload"]["invalid_ballot_hashes"] == [H(b"invalid"), H(b"invalid")]
    assert run(config, events, rows)[0]["tally_invalid_set_mismatches"] == 0
    events[-1]["payload"]["invalid_ballot_hashes"].pop()
    assert run(config, events, rows)[0]["tally_invalid_set_mismatches"] == 1


@pytest.mark.parametrize("kind", ["missing_row", "wrong_nullifier", "wrong_receipt", "wrong_hash", "corrupted_bytes"])
def test_core_invalid_ballot_row_and_hash_criteria_are_preserved(kind):
    config, events, rows = vector_inputs()
    if kind == "missing_row":
        rows.pop(0)
    elif kind == "wrong_nullifier":
        rows[0]["row_key"] = "00" * 32
    elif kind == "wrong_receipt":
        rows[0]["receipt_hash"] = "00" * 32
    elif kind == "wrong_hash":
        rows[0]["ballot_hash"] = "00" * 32
    else:
        rows[0]["sealed_ballot"] = b64(b"changed")
    config["decryption_shares"] = None
    observed, reason, _, _ = run(config, events, rows)
    assert observed["tally_invalid_set_mismatches"] == 1
    assert reason == "KEY_NOT_REVEALED"


def test_ballots_after_final_checkpoint_are_not_included_in_tally():
    config, events, rows = vector_inputs()
    event = deepcopy(events[1])
    event["event_id"], event["checkpoint"] = "after_final", 3
    events.insert(-1, event)
    observed, reason, _, _ = run(config, events, rows)
    assert all(v == 0 for v in observed.values()) and reason is None
