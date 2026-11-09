"""Interoperability and corruption checks for the independent hash layer."""

from __future__ import annotations

import base64
import copy
import hashlib
import json

import pytest

from purevoting_rules.hashing import (
    H,
    Hc,
    ballot_hash,
    ballot_set_root,
    canonical,
    config_hash,
    finding_record_digest,
    h32,
    leaf_hash,
    merkle_root,
    node,
    public_key_hash,
    rejection_leaf,
    result_hash,
    set_root,
    snapshot_hash,
    target_hash,
    u,
)
from purevoting_rules.paths import reference_root
from purevoting_rules.vector_check import verify_vectors


def test_all_39_frozen_vectors_match_independent_implementation():
    results = verify_vectors(reference_root() / "tests/vectors/hash-vectors.json")
    assert len(results) == 39
    assert all(result["passed"] for result in results), [
        result for result in results if not result["passed"]
    ]


@pytest.mark.parametrize("value", [
    1.0,
    {"nested": [0, {"number": 0.5}]},
    {"number": float("nan")},
    [float("inf")],
    {"number": -0.0},
])
def test_hashed_json_rejects_floats_at_every_depth(value):
    with pytest.raises(ValueError, match="Floating-point"):
        canonical(value)


def test_booleans_are_canonical_json_booleans_not_numeric_codes():
    assert canonical({"yes": True, "no": False, "integer": 1}) == b'{"integer":1,"no":false,"yes":true}'
    assert Hc(True) != Hc(1)
    with pytest.raises(ValueError, match="unsigned 256-bit"):
        u(True)


def test_safe_integer_limits_are_lossless_and_oversized_numbers_fail():
    largest = (1 << 53) - 1
    assert canonical([largest, -largest]) == b"[9007199254740991,-9007199254740991]"
    for integer in (largest + 1, -largest - 1, 1 << 256):
        with pytest.raises(ValueError, match="JCS safe range"):
            canonical({"n": integer})
    # Binary signature encodings are uint256, separate from hashed JSON.
    assert u((1 << 256) - 1) == b"\xff" * 32
    with pytest.raises(ValueError):
        u(1 << 256)
    with pytest.raises(ValueError):
        u(-1)


def test_jcs_uses_utf16_key_order_without_unicode_normalization():
    assert canonical({"\ue000": 2, "\U0001f600": 1}) == '{"\U0001f600":1,"\ue000":2}'.encode("utf-8")
    assert Hc("\u00e9") != Hc("e\u0301")


def test_circular_and_non_json_values_are_rejected():
    circular = []
    circular.append(circular)
    with pytest.raises(ValueError, match="Circular"):
        canonical(circular)
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical({1: "numeric key"})
    with pytest.raises(TypeError, match="Unsupported JSON type"):
        canonical({"bytes": b"not JSON"})


def test_shared_but_acyclic_values_are_supported():
    repeated = {"valid": True}
    assert canonical([repeated, repeated]) == b'[{"valid":true},{"valid":true}]'


def test_hashing_text_is_utf8_and_bytes_are_not_reencoded():
    assert H("한글") == hashlib.sha256("한글".encode("utf-8")).hexdigest()
    assert H(b"\x00\xff") == hashlib.sha256(b"\x00\xff").hexdigest()
    with pytest.raises(TypeError):
        H(123)


def test_merkle_odd_leaf_is_carried_up_and_parent_hashes_hex_text():
    first, second, third = (hashlib.sha256(item).hexdigest() for item in (b"a", b"b", b"c"))
    pair = hashlib.sha256((first + second).encode("ascii")).hexdigest()
    expected = hashlib.sha256((pair + third).encode("ascii")).hexdigest()
    assert merkle_root([first, second, third]) == expected
    assert node(first, second) != hashlib.sha256(bytes.fromhex(first + second)).hexdigest()
    assert merkle_root([first]) == first
    assert merkle_root([]) == hashlib.sha256(b"").hexdigest()
    assert merkle_root([first, second]) != merkle_root([second, first])


def test_ballot_root_preserves_duplicate_events_but_set_root_deduplicates():
    first, second = H("ballot-a"), H("ballot-b")
    assert ballot_set_root([first, second, first]) == ballot_set_root([first, first, second])
    assert ballot_set_root([first, second, first]) != ballot_set_root([first, second])
    assert set_root([first, second, first]) == set_root([second, first])
    assert ballot_set_root([first, second, first]) != set_root([first, second, first])


@pytest.mark.parametrize("invalid", ["f" * 63, "F" * 64, "0x" + "f" * 64, "g" * 64, "f" * 64 + "\n", None])
def test_digest_encodings_fail_closed(invalid):
    with pytest.raises(ValueError, match="64 lowercase"):
        h32(invalid)
    with pytest.raises(ValueError):
        merkle_root([invalid])


def test_sealed_ballot_hash_decodes_canonical_base64():
    raw = b"\x00\x01\xff\xfe"
    encoded = base64.b64encode(raw).decode("ascii")
    assert ballot_hash(encoded) == H(raw) == ballot_hash(raw)
    assert ballot_hash(encoded) != H(encoded)
    for invalid in (encoded.rstrip("="), encoded + "\n", "AB=="):
        with pytest.raises(ValueError):
            ballot_hash(invalid)
    assert public_key_hash(bytes(32)) == H(bytes(32))
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        public_key_hash(bytes(31))


def test_leaf_hash_covers_payload_hash_but_excludes_response_metadata():
    submission = {"event_id": "event", "payload": {"x": 1}, "payload_hash": Hc({"x": 1})}
    stored = {**submission, "received_at": "2026-11-03T11:00:00Z", "anchor": {"id": "anchor"}}
    assert leaf_hash(stored) == Hc(submission)
    stored["payload_hash"] = H("altered")
    assert leaf_hash(stored) != Hc(submission)


def test_rejection_hash_defaults_missing_subject_and_ignores_unhashed_fields():
    vector = json.loads((reference_root() / "tests/vectors/hash-vectors.json").read_text(encoding="utf-8"))["vectors"]
    rejection = next(item["input"] for item in vector if item["name"] == "rejection_leaf_0")
    absent = {key: value for key, value in rejection.items() if key != "subject_hash"}
    assert rejection_leaf(absent) == rejection_leaf(rejection)
    assert rejection_leaf({**absent, "detail": "informational", "anchor_id": "later"}) == rejection_leaf(rejection)
    assert rejection_leaf({**absent, "reject_code": "DUPLICATE_CONTENT"}) != rejection_leaf(rejection)


def test_snapshot_sorts_all_rows_and_preserves_all_columns():
    rows = [{"row_key": "b", "sealed_ballot": "AA=="}, {"row_key": "a", "sealed_ballot": "AQ=="}]
    original = copy.deepcopy(rows)
    assert snapshot_hash(rows) == snapshot_hash(list(reversed(rows)))
    assert rows == original
    assert snapshot_hash(rows) != snapshot_hash(rows[:1])
    rows[0]["sealed_ballot"] = "Ag=="
    assert snapshot_hash(rows) != snapshot_hash(original)
    with pytest.raises(ValueError, match="row_key"):
        snapshot_hash([{"value": "missing key"}])


def test_config_uses_creation_window_and_normalizes_order_without_mutation():
    vectors = json.loads((reference_root() / "tests/vectors/hash-vectors.json").read_text(encoding="utf-8"))["vectors"]
    original = next(item["input"] for item in vectors if item["name"] == "config_hash")
    view = copy.deepcopy(original)
    view["candidates"].reverse()
    view["organizations"].reverse()
    view["organizations"][0]["signer"] = view["organizations"][0]["signer"].upper()
    view["config_window_start"] = view["window_start"]
    view["config_window_end"] = view["window_end"]
    view["window_end"] = "2026-11-03T12:00:00Z"
    before = copy.deepcopy(view)
    assert config_hash(view) == config_hash(original)
    assert view == before


def test_proposal_and_result_sort_required_lists_and_bind_semantics():
    proposal = {"election_id": "elc", "action_id": "act", "action_type": "ROSTER_AMEND", "target": {"add": ["c", "a"], "remove": []}}
    reordered = copy.deepcopy(proposal)
    reordered["target"]["add"].reverse()
    assert target_hash(proposal) == target_hash(reordered)
    reordered["target"]["remove"] = ["b"]
    assert target_hash(proposal) != target_hash(reordered)
    vectors = json.loads((reference_root() / "tests/vectors/hash-vectors.json").read_text(encoding="utf-8"))["vectors"]
    original = next(item["input"] for item in vectors if item["name"] == "result_hash")
    reordered_result = copy.deepcopy(original)
    reordered_result["results"].reverse()
    assert result_hash(original) == result_hash(reordered_result)
    reordered_result["results"] = [item for item in reordered_result["results"] if item["votes"]]
    assert result_hash(original) != result_hash(reordered_result)


def test_finding_digest_names_match_codes_and_bind_every_field():
    args = ["fnd_00000000000000000000000000", "elc_00000000000000000000000000", "R3", "FAIL", 4, H("f")]
    digest = finding_record_digest(*args)
    assert digest == "352c41ba0c51b025df9afd982471d2d9265a60ec0ce2174ba208c64f87197084"
    assert digest == finding_record_digest(args[0], args[1], 3, 1, 4, args[5])
    replacements = ["different finding", "different election", "R4", "PASS", 5, H("changed")]
    for index, replacement in enumerate(replacements):
        changed = list(args)
        changed[index] = replacement
        assert finding_record_digest(*changed) != digest
    with pytest.raises(ValueError, match="Unknown rule result"):
        finding_record_digest(args[0], args[1], "R3", True, 4, args[5])


def test_vector_checker_reports_corruption_and_refuses_unknown_or_empty_vectors(tmp_path):
    path = tmp_path / "vectors.json"
    document = {"vectors": [{"name": "H_empty", "input": "", "expected": "wrong"}]}
    path.write_text(json.dumps(document), encoding="utf-8")
    result = verify_vectors(path)[0]
    assert result["passed"] is False
    assert result["actual"] == H("")
    document["vectors"][0]["name"] = "unknown_vector"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="No independent implementation"):
        verify_vectors(path)
    path.write_text('{"vectors": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        verify_vectors(path)
