"""R5 common and retained sealed-box checks, with independent encrypted inputs."""
import base64
from copy import deepcopy

from nacl.public import PrivateKey, SealedBox
import pytest

from purevoting_rules.fixtures import _Factory, _base, _commit_payload, _rehash, _sync_current_commit
from purevoting_rules.hashing import H, ballot_set_root, config_hash, receipt_hash, result_hash
from purevoting_rules.integrity_local import audit_local


def run(s):
    out, refs = {}, []
    failed, reason, detail = audit_local(s, s["events"], s["rejections"], out, refs)
    return failed, reason, out, refs, detail


def sealed_tally(plaintexts=None):
    f = _Factory(1701)
    s = _base(f)
    config = s["views"]["election_config"][0]
    cid = config["candidates"][0]["candidate_id"]
    key = PrivateKey(bytes(range(1, 33)))
    config["public_key"] = base64.b64encode(bytes(key.public_key)).decode()
    config["private_key"] = base64.b64encode(bytes(key)).decode()
    config["ballot_scheme"] = "SEALED_BOX_V1"
    config["config_hash"] = config_hash({**config, "public_key_hash": H(bytes(key.public_key))})
    created = next(e for e in s["events"] if e["type"] == "ELECTION_CREATED")
    created["payload"].update(config_hash=config["config_hash"], public_key_hash=H(bytes(key.public_key)))
    _rehash(created)
    default = ('{"candidate_id":"' + cid + '"}').encode()
    plaintexts = plaintexts or [default, default]
    ballot_events = [e for e in s["events"] if e["type"] == "BALLOT_RECEIVED"]
    for row, event, plaintext in zip(s["views"]["ballot_receipts"], ballot_events, plaintexts):
        raw = SealedBox(key.public_key).encrypt(plaintext)
        row.update(ballot_hash=H(raw), sealed_ballot=base64.b64encode(raw).decode())
        row["receipt_hash"] = receipt_hash(s["election_id"], row["row_key"], row["ballot_hash"])
        event["payload"].update(ballot_hash=row["ballot_hash"], receipt_hash=row["receipt_hash"])
        _rehash(event)
    _sync_current_commit(s)
    s["events"].append(f.event("ELECTION_CLOSED", 3, "2026-11-03T09:30:00Z",
                                {"closed_at": "2026-11-03T09:30:00Z", "reason": "SCHEDULED"}, 8))
    config["status"] = "CLOSED"
    close = f.event("CHECKPOINT_COMMITTED", 3, "2026-11-03T09:30:00Z", _commit_payload(s["views"], 3, "CLOSE_FINAL"), 9)
    s["events"].append(close)
    s["history"]["3"] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(close)}
    config["status"] = "TALLIED"
    tally = {"final_checkpoint": 3, "ballot_count": 2, "invalid_ballots": 0, "excluded_ballots": 0,
             "invalid_ballot_hashes": [], "results": [{"candidate_id": c["candidate_id"], "votes": 2 if c["candidate_id"] == cid else 0}
                                                      for c in config["candidates"]],
             "ballot_set_root": ballot_set_root(r["ballot_hash"] for r in s["views"]["ballot_receipts"])}
    tally["result_hash"] = result_hash({**tally, "election_id": s["election_id"]})
    s["events"].append(f.event("TALLY_PUBLISHED", 4, "2026-11-03T09:45:00Z", tally, 10))
    commit = f.event("CHECKPOINT_COMMITTED", 4, "2026-11-03T09:45:00Z", _commit_payload(s["views"], 4, "TALLY"), 11)
    s["events"].append(commit)
    s.update(checkpoint=4, checkpoint_event_id=commit["event_id"], ledger_last_seq=11)
    s["history"]["4"] = {"views": deepcopy(s["views"]), "checkpoint_event": deepcopy(commit)}
    return s


def test_clean_common_checks_and_genuine_legacy_decryption():
    s = sealed_tally()
    failed, reason, out, _, _ = run(s)
    assert (failed, reason) == (False, None)
    assert out["key_mismatch"] == out["tally_result_mismatches"] == out["tally_result_hash_mismatches"] == 0
    assert out["tally_share_failures"] is None  # Not applicable to this ballot scheme.


@pytest.mark.parametrize("plaintext,valid", [
    (' { "candidate_id" : "{cid}" } ', True),
    ('{"candidate_id":"{cid}","candidate_id":"{cid}"}', False),
    ('{"candidate_id":"{cid}","extra":0}', False),
    ('{"candidate_id":"unknown"}', False),
    ('["{cid}"]', False),
    ('{"candidate_id":NaN}', False),
])
def test_legacy_json_parsing_matches_spec(plaintext, valid):
    cid = _base(_Factory(1701))["views"]["election_config"][0]["candidates"][0]["candidate_id"]
    s = sealed_tally([plaintext.replace("{cid}", cid).encode()] * 2)
    failed, _, out, _, _ = run(s)
    assert failed is (not valid)
    assert out["tally_invalid_set_mismatches"] == (0 if valid else 2)
    assert out["tally_result_mismatches"] == (0 if valid else 1)


def test_legacy_invalid_ballots_are_allowed_when_invalid_set_and_counts_agree():
    s = sealed_tally([b"not json", b"not json"])
    tally = next(e for e in s["events"] if e["type"] == "TALLY_PUBLISHED")
    p = tally["payload"]
    p.update(ballot_count=0, invalid_ballots=2, invalid_ballot_hashes=sorted(r["ballot_hash"] for r in s["views"]["ballot_receipts"]))
    for row in p["results"]:
        row["votes"] = 0
    p["result_hash"] = result_hash({**p, "election_id": s["election_id"]})
    _rehash(tally)
    assert run(s)[:2] == (False, None)


@pytest.mark.parametrize("key", ["result_hash", "final_checkpoint", "ballot_set_root"])
def test_tally_commitments_checked_independently(key):
    s = sealed_tally()
    tally = next(e for e in s["events"] if e["type"] == "TALLY_PUBLISHED")
    tally["payload"][key] = 2 if key == "final_checkpoint" else H("forged")
    _rehash(tally)
    failed, _, out, _, _ = run(s)
    assert failed
    observed = {"result_hash": "tally_result_hash_mismatches", "final_checkpoint": "tally_final_checkpoint_mismatches",
                "ballot_set_root": "tally_root_mismatches"}[key]
    assert out[observed] == 1


def test_forged_results_still_fail_when_published_result_hash_is_recomputed():
    s = sealed_tally()
    tally = next(e for e in s["events"] if e["type"] == "TALLY_PUBLISHED")
    tally["payload"]["results"][0]["votes"] += 1
    tally["payload"]["result_hash"] = result_hash({**tally["payload"], "election_id": s["election_id"]})
    _rehash(tally)
    failed, _, out, _, _ = run(s)
    assert failed and out["tally_result_mismatches"] == 1
    assert out["tally_result_hash_mismatches"] == 0


def test_missing_key_is_not_a_failed_ciphertext_or_a_pass():
    s = sealed_tally()
    s["views"]["election_config"][0]["private_key"] = None
    failed, reason, out, _, _ = run(s)
    assert not failed and reason == "KEY_NOT_REVEALED"
    assert out["tally_invalid_set_mismatches"] is None and out["tally_result_mismatches"] is None


def test_wrong_private_key_is_failure_before_tally_decryption():
    s = sealed_tally()
    s["views"]["election_config"][0]["private_key"] = base64.b64encode(bytes(PrivateKey.generate())).decode()
    failed, _, out, _, _ = run(s)
    assert failed and out["key_mismatch"] == 1 and out["tally_result_mismatches"] is None


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_tally_event_required_with_clear_summary_detail(count):
    s = sealed_tally()
    tally = next(e for e in s["events"] if e["type"] == "TALLY_PUBLISHED")
    if count == 0:
        s["events"].remove(tally)
    else:
        other = deepcopy(tally)
        other["event_id"] = _Factory(1702).identity("evt")
        s["events"].append(other)
    failed, reason, out, _, detail = run(s)
    assert not failed and reason == "OTHER"
    assert "missing" in detail if count == 0 else "more than one" in detail
    assert out["tally_result_hash_mismatches"] is None


@pytest.mark.parametrize("table", ["roster", "token_status"])
def test_db_add_caught_even_if_checkpoint_snapshot_hash_was_forged(table):
    s = _base(_Factory(1703))
    row = deepcopy(s["views"][table][-1])
    row["row_key"] = _Factory(1704).identity("vtr") if table == "roster" else H("injected token")
    s["views"][table].append(row)
    _sync_current_commit(s)
    failed, _, out, _, _ = run(s)
    assert failed and out["root_mismatches"] > 0
    assert out["tables_mismatched"] == 0


def test_historical_root_checked_when_snapshot_hash_and_current_rows_agree():
    s = _base(_Factory(1705))
    past = s["history"]["0"]
    past["views"]["roster"][0]["row_key"] = _Factory(1706).identity("vtr")
    commit = next(e for e in s["events"] if e["type"] == "CHECKPOINT_COMMITTED" and e["checkpoint"] == 0)
    commit["payload"] = _commit_payload(past["views"], 0, "STATE_CHANGE")
    _rehash(commit)
    assert run(s)[2]["root_mismatches"] == 1


def test_configuration_hash_uses_creation_window_and_detects_changed_candidate():
    s = _base(_Factory(1707))
    s["views"]["election_config"][0]["window_end"] = "2026-11-03T10:30:00Z"
    assert run(s)[2]["config_mismatch"] == 0  # Effective end is a chain/state check.
    s["views"]["election_config"][0]["candidates"][0]["name"] = "Changed without a new election"
    assert run(s)[2]["config_mismatch"] == 1
