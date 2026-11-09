"""Finding-record signatures bind the raw digest and the expected public key."""

from __future__ import annotations

import json
import re

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_keys import keys
from eth_utils import keccak
from jsonschema import Draft202012Validator

from purevoting_rules.hashing import H, finding_record_digest
from purevoting_rules.paths import reference_root
from purevoting_rules.signing import EIP191Signer, verify_signature


def test_signature_matches_frozen_schema_and_raw_eip191_prefix():
    signer = EIP191Signer()
    digest = finding_record_digest("finding", "election", "R2", "FAIL", 3, H("body"))
    signature = signer.sign_digest(digest)
    common = json.loads((reference_root() / "contracts/common.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(common["$defs"]["Signature"]).validate(signature)
    assert signature["scheme"] == "eip191-secp256k1"
    assert re.fullmatch(r"0x[0-9a-f]{40}", signature["signer"])
    assert re.fullmatch(r"0x[0-9a-f]{130}", signature["value"])
    assert verify_signature(digest, signature)

    # Independently assemble the normative message prefix and use the lower
    # level recovery API, not the signer's encode_defunct call.
    raw = bytes.fromhex(signature["value"][2:])
    assert raw[-1] in (27, 28)
    recovered = keys.Signature(vrs=(
        raw[-1] - 27,
        int.from_bytes(raw[:32], "big"),
        int.from_bytes(raw[32:64], "big"),
    )).recover_public_key_from_msg_hash(
        keccak(b"\x19Ethereum Signed Message:\n32" + bytes.fromhex(digest))
    )
    assert recovered.to_checksum_address().lower() == signer.address
    assert Account.recover_message(encode_defunct(text=digest), signature=signature["value"]).lower() != signer.address


def test_digest_signature_and_claimed_signer_tampering_are_detected():
    signer = EIP191Signer()
    digest = H("original finding record")
    signature = signer.sign_digest(digest)
    assert not verify_signature(H("altered finding record"), signature)
    assert not verify_signature(digest, {**signature, "signer": EIP191Signer().address})
    changed = bytearray(bytes.fromhex(signature["value"][2:]))
    changed[5] ^= 1
    assert not verify_signature(digest, {**signature, "value": "0x" + changed.hex()})


@pytest.mark.parametrize("change", [
    {"scheme": "ed25519"},
    {"signer": "0x" + "A" * 40},
    {"signer": "0x" + "0" * 39},
    {"value": "0x" + "0" * 128 + "1b"},
    {"value": "0x" + "1" * 128 + "00"},
    {"value": "0x" + "1" * 128 + "1d"},
    {"value": "0x" + "1" * 129},
    {"value": "0x" + "G" * 128 + "1b"},
    {"value": None},
    {"unexpected": "not allowed by Signature schema"},
])
def test_noncanonical_or_malformed_signature_fails_closed(change):
    digest = H("finding")
    signature = EIP191Signer().sign_digest(digest)
    assert not verify_signature(digest, {**signature, **change})


@pytest.mark.parametrize("digest", ["a" * 63, "0x" + "a" * 64, "A" * 64, None, True])
def test_invalid_digest_is_not_signed_or_verified(digest):
    signer = EIP191Signer()
    with pytest.raises(ValueError):
        signer.sign_digest(digest)
    assert not verify_signature(digest, signer.sign_digest(H("valid")))


def test_missing_signature_fields_or_non_objects_fail_closed():
    digest = H("finding")
    for signature in ({}, {"scheme": "eip191-secp256k1"}, None, "not a signature", []):
        assert not verify_signature(digest, signature)


def test_ephemeral_keys_change_between_instances_and_repr_is_public_only():
    first, second = EIP191Signer(), EIP191Signer()
    assert first.address != second.address
    assert repr(first) == f"EIP191Signer(address={first.address!r})"
    assert not hasattr(first, "private_key")
    assert not hasattr(first, "key")


def test_in_memory_key_import_signs_consistently_without_exposing_key():
    account = Account.create()
    signer = EIP191Signer(account.key)
    assert signer.address == account.address.lower()
    digest = H("same finding")
    first, second = signer.sign_digest(digest), signer.sign_digest(digest)
    assert first == second
    assert verify_signature(digest, first)


def test_invalid_key_error_does_not_echo_input():
    invalid = "invalid-key-material-must-not-be-echoed"
    with pytest.raises(ValueError) as caught:
        EIP191Signer(invalid)
    assert invalid not in str(caught.value)
