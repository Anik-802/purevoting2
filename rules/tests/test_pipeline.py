from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from jsonschema.exceptions import ValidationError

from purevoting_rules.findings import REQUIRED_OBSERVED, build_finding, validate_submission
from purevoting_rules.hashing import Hc, canonical, finding_record_digest
from purevoting_rules.models import RuleOutcome
from purevoting_rules.paths import reference_root
from purevoting_rules.publication import CORE_URL, GATEWAY_URL, LocalPublicationSimulator, PublicationError
from purevoting_rules.scheduler import run_decision
from purevoting_rules.signing import EIP191Signer
from purevoting_rules.storage import (
    FindingConflict, FindingStore, OutboxLocked, OutboxPersistenceError, StoredBodyCorrupt,
)


ELECTION = "elc_" + "1" * 26
OTHER_ELECTION = "elc_" + "2" * 26
CHECKPOINT_EVENT = "evt_" + "3" * 26
FINDING = "fnd_" + "4" * 26
SECOND_FINDING = "fnd_" + "5" * 26
THIRD_FINDING = "fnd_" + "6" * 26
NOW = "2026-10-27T12:00:00Z"


def outcome(result="PASS", *, rule_id="R1", observed=None, evidence=None):
    observations = dict.fromkeys(REQUIRED_OBSERVED[rule_id], 0)
    if observed:
        observations.update(observed)
    return RuleOutcome(
        rule_id, result, observations, "No accepted duplicate tokens.",
        "Local unit-test example.",
        evidence_refs=evidence if evidence is not None else [
            {"kind": "ledger_event", "ref": CHECKPOINT_EVENT},
        ],
        inconclusive_reason="OTHER" if result == "INCONCLUSIVE" else None,
    )


def body(*, finding_id=FINDING, election_id=ELECTION, checkpoint=2, result="PASS", observed=None):
    return build_finding(
        outcome(result, observed=observed),
        {"election_id": election_id, "checkpoint": checkpoint,
         "checkpoint_event_id": CHECKPOINT_EVENT, "ledger_last_seq": 10},
        finding_id=finding_id, checked_at=NOW,
    )


@pytest.mark.parametrize("path", sorted(
    (reference_root() / "contracts/examples/valid/finding.FindingSubmission").glob("*.json")
), ids=lambda path: path.name)
def test_frozen_valid_finding_examples_are_still_accepted(path):
    # In particular, the official no-node sample uses a sparse observed object.
    validate_submission(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", sorted(
    (reference_root() / "contracts/examples/invalid/finding.FindingSubmission").glob("*.json")
), ids=lambda path: path.name)
def test_frozen_invalid_finding_examples_are_rejected(path):
    with pytest.raises((ValidationError, ValueError, TypeError)):
        validate_submission(json.loads(path.read_text(encoding="utf-8")))


def test_builder_requires_rule_observations_and_rejects_floats():
    source = outcome()
    sparse = replace(source, observed={"accepted_duplicate_tokens": 0})
    context = {"election_id": ELECTION, "checkpoint": 2,
               "checkpoint_event_id": CHECKPOINT_EVENT, "ledger_last_seq": 10}
    with pytest.raises(ValueError, match="missing required observations"):
        build_finding(sparse, context, checked_at=NOW)
    with pytest.raises(ValueError, match="Floating-point"):
        body(observed={"accepted_duplicate_tokens": 0.0})
    with pytest.raises(ValueError, match="unknown required observations"):
        body(observed={"accepted_duplicate_tokens": None})


def test_builder_evidence_order_and_input_objects_are_stable():
    first = {"kind": "snapshot", "ref": "token_status@2"}
    second = {"kind": "ledger_event", "ref": CHECKPOINT_EVENT}
    source = outcome(evidence=[first, second, first])
    saved = copy.deepcopy(source.evidence_refs)
    context = {"election_id": ELECTION, "checkpoint": 2,
               "checkpoint_event_id": CHECKPOINT_EVENT, "ledger_last_seq": 10}
    finding = build_finding(source, context, finding_id=FINDING, checked_at=NOW)
    reordered = build_finding(replace(source, evidence_refs=[second, first]), context,
                             finding_id=FINDING, checked_at=NOW)
    assert canonical(finding) == canonical(reordered)
    assert len(finding["evidence_refs"]) == 2
    assert source.evidence_refs == saved
    finding["observed"]["unknown_tokens"] = 99
    assert source.observed["unknown_tokens"] == 0


def test_missing_failure_evidence_is_not_invented():
    context = {"election_id": ELECTION, "checkpoint": 2,
               "checkpoint_event_id": CHECKPOINT_EVENT, "ledger_last_seq": 10}
    with pytest.raises(ValidationError):
        build_finding(outcome("FAIL", evidence=[]), context, checked_at=NOW)


def test_partial_rules_cannot_pass_and_disclose_unimplemented_checks():
    with pytest.raises(ValueError, match="cannot return PASS"):
        replace(outcome(), coverage="partial", unimplemented_checks=("chain registry",))
    partial = replace(outcome("INCONCLUSIVE", rule_id="R4"), coverage="partial",
                      unimplemented_checks=("registry signatures", "roster transition chain"))
    context = {"election_id": ELECTION, "checkpoint": 2,
               "checkpoint_event_id": CHECKPOINT_EVENT, "ledger_last_seq": 10}
    finding = build_finding(partial, context, checked_at=NOW)
    assert finding["result"] == "INCONCLUSIVE"
    assert "registry signatures" in finding["summary"]
    assert "roster transition chain" in finding["summary"]
    assert "coverage" not in finding
    # A proven implemented violation may still FAIL despite partial coverage.
    failed = replace(partial, result="FAIL", observed={**partial.observed, "unapproved_changes": 1})
    assert build_finding(failed, context, checked_at=NOW)["result"] == "FAIL"


def test_manual_finding_requires_request_identity():
    context = {"election_id": ELECTION, "checkpoint": 2, "trigger": "MANUAL",
               "checkpoint_event_id": CHECKPOINT_EVENT, "ledger_last_seq": 10}
    with pytest.raises(ValueError, match="run_request_id"):
        build_finding(outcome(), context)
    context["run_request_id"] = "run_" + "7" * 26
    assert build_finding(outcome(), context)["run_request_id"] == context["run_request_id"]


def test_durable_bytes_survive_restart_and_body_503(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    signer = EIP191Signer()
    store = FindingStore(path)
    finding_id = store.enqueue(body())
    persisted = store.get(finding_id)["body_bytes"]
    simulator = LocalPublicationSimulator(store, signer, transient_body_failures=1)
    report = simulator.publish_all()
    backend = simulator.backend
    assert (report["registered"], report["stored"], report["pending"], report["retryable"]) == (1, 0, 1, 1)
    assert [(item["stage"], item["status"]) for item in report["requests"]] == [
        ("register", 201), ("body", 503),
    ]
    assert store.get(finding_id)["state"] == "REGISTERED"
    simulator.close()
    store.close()

    # Simulate a worker restart; the simulated remote services stayed alive.
    with FindingStore(path) as reopened:
        assert reopened.get(finding_id)["body_bytes"] == persisted
        with LocalPublicationSimulator(reopened, signer, backend=backend) as retry:
            report = retry.publish_all()
            assert (report["registered"], report["stored"], report["pending"]) == (0, 1, 0)
            assert report["identical_retry_bodies"] is True
            assert report["retried_body_count"] == 1
            assert backend.body_attempts[finding_id] == [persisted, persisted]
            assert reopened.get(finding_id)["state"] == "STORED"
            assert retry.publish_all()["requests"] == []


def test_multicase_outboxes_survive_process_exit_and_single_file_copy():
    """Exercise the actual project filesystem, not only pytest's /tmp area.

    The writer exits before a separate process reads each database and a copy
    containing only its main file. This catches a full-run persistence failure
    which short same-process reopen tests did not detect with WAL journaling.
    """
    project_root = Path(__file__).resolve().parents[2]
    rules_root = project_root / "rules"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(rules_root) + os.pathsep + environment.get("PYTHONPATH", "")
    writer = r'''
import json
import sys
from pathlib import Path
from purevoting_rules.engine import evaluate_all
from purevoting_rules.findings import build_finding
from purevoting_rules.fixtures import development_cases
from purevoting_rules.publication import LocalPublicationSimulator
from purevoting_rules.signing import EIP191Signer
from purevoting_rules.storage import FindingStore

directory = Path(sys.argv[1])
signer = EIP191Signer()
expected = {}
for seed in (1, 2, 3):
    for case in development_cases(seed):
        relative = f"seed_{seed}/{case['case_id']}/findings.sqlite3"
        with FindingStore(directory / relative) as store:
            for decision in evaluate_all(case['snapshot']):
                store.enqueue(build_finding(decision, case['snapshot']))
            with LocalPublicationSimulator(store, signer, transient_body_failures=1) as worker:
                for attempt in range(3):
                    if worker.publish_all()['pending'] == 0:
                        break
                assert not store.pending()
            records = store.records()
            assert len(records) == 5
            expected[relative] = {record['finding_id']: record['finding_hash'] for record in records}
print(json.dumps(expected))
'''
    reader = r'''
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

directory = Path(sys.argv[1])
expected = json.load(sys.stdin)
copies = directory / 'single_file_copies'
copies.mkdir()
total = 0
for index, (relative, hashes) in enumerate(expected.items()):
    original = directory / relative
    assert not Path(str(original) + '-wal').exists(), relative
    assert not Path(str(original) + '-shm').exists(), relative
    copy = copies / f'{index}.sqlite3'
    shutil.copy2(original, copy)  # No sidecars or shared-memory state are copied.
    for path in (original, copy):
        with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as connection:
            assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
            assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            rows = connection.execute('SELECT finding_id, state, body, finding_hash FROM findings').fetchall()
            assert len(rows) == len(hashes), relative
            assert {row[0] for row in rows} == set(hashes), relative
            for finding_id, state, raw, digest in rows:
                assert state == 'STORED', (relative, state)
                assert hashlib.sha256(raw).hexdigest() == digest == hashes[finding_id], relative
    total += len(hashes)
print(json.dumps({'databases': len(expected), 'findings': total}))
'''
    # Use the checkout's own mounted filesystem to expose filesystem-dependent
    # failures. Preserve failed artifacts for diagnosis; remove successful runs.
    temporary = tempfile.mkdtemp(prefix="outbox-persistence-", dir=project_root)
    try:
        written = subprocess.run(
            [sys.executable, "-c", writer, temporary], cwd=project_root,
            env=environment, capture_output=True, text=True, check=True, timeout=60,
        )
        (Path(temporary) / "expected.json").write_text(written.stdout, encoding="utf-8")
        reopened = subprocess.run(
            [sys.executable, "-c", reader, temporary], cwd=project_root,
            env=environment, input=written.stdout, capture_output=True, text=True,
            check=True, timeout=30,
        )
    except BaseException:
        print(f"Persistence regression artifacts preserved at {temporary}")
        raise
    else:
        shutil.rmtree(temporary)
    verified = json.loads(reopened.stdout)
    assert verified["databases"] >= 3 * 19
    assert verified["findings"] == verified["databases"] * 5


@pytest.mark.parametrize("stage", ["QUEUED", "REGISTERED", "STORED"])
def test_acknowledged_snapshot_survives_process_exit_without_close(tmp_path, stage):
    path = tmp_path / "outbox.sqlite3"
    writer = r'''
import json, os, sys
from pathlib import Path
from purevoting_rules.storage import FindingStore
store = FindingStore(Path(sys.argv[1]))
finding_id = store.enqueue(json.load(sys.stdin))
if sys.argv[2] in ('REGISTERED', 'STORED'):
    store.mark_registered(finding_id)
if sys.argv[2] == 'STORED':
    store.mark_stored(finding_id)
os._exit(0)  # No Python finalizers or FindingStore.close().
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    submitted = body()
    subprocess.run(
        [sys.executable, "-c", writer, str(path), stage], env=environment,
        input=json.dumps(submitted), text=True, capture_output=True, check=True, timeout=10,
    )
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        saved_state, raw = connection.execute("SELECT state,body FROM findings").fetchone()
        assert saved_state == stage
        assert raw == canonical(submitted)
    # The OS releases the advisory lock despite the unclean process exit.
    with FindingStore(path) as reopened:
        assert reopened.get(FINDING)["state"] == stage


def test_single_writer_lock_blocks_other_instances_and_processes(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    probe = r'''
import sys
from pathlib import Path
from purevoting_rules.storage import FindingStore, OutboxLocked
try:
    FindingStore(Path(sys.argv[1]))
except OutboxLocked:
    print('writer blocked')
else:
    raise AssertionError('A second writer acquired the same outbox')
'''
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    with FindingStore(path) as first:
        first.enqueue(body())
        with pytest.raises(OutboxLocked):
            FindingStore(path)
        other = subprocess.run(
            [sys.executable, "-c", probe, str(path)], env=environment,
            text=True, capture_output=True, check=True, timeout=10,
        )
        assert other.stdout.strip() == "writer blocked"
    with FindingStore(path) as reopened:
        assert reopened.get(FINDING)["state"] == "QUEUED"


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_legacy_sidecars_are_rejected_without_deleting_recovery_evidence(tmp_path, suffix):
    path = tmp_path / "outbox.sqlite3"
    with FindingStore(path) as store:
        store.enqueue(body())
    previous_image = path.read_bytes()
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"legacy recovery evidence")
    with pytest.raises(StoredBodyCorrupt, match="checked recovery"):
        FindingStore(path)
    assert path.read_bytes() == previous_image
    assert sidecar.read_bytes() == b"legacy recovery evidence"
    sidecar.unlink()  # Test cleanup only; the implementation never removes it.
    with FindingStore(path) as reopened:
        assert reopened.get(FINDING)["state"] == "QUEUED"


@pytest.mark.parametrize("operation", ["enqueue", "register", "store"])
def test_failed_atomic_replace_never_acknowledges_new_memory_state(tmp_path, monkeypatch, operation):
    path = tmp_path / "outbox.sqlite3"
    store = FindingStore(path)
    if operation in {"register", "store"}:
        store.enqueue(body())
    if operation == "store":
        store.mark_registered(FINDING)
    previous_image = path.read_bytes()
    previous_records = [(record["finding_id"], record["state"]) for record in store.records()]

    def fail_replace(*arguments):
        raise OSError("Simulated atomic replacement failure")

    with monkeypatch.context() as patch:
        patch.setattr("purevoting_rules.storage.os.replace", fail_replace)
        try:
            with pytest.raises(OutboxPersistenceError):
                if operation == "enqueue":
                    store.enqueue(body())
                elif operation == "register":
                    store.mark_registered(FINDING)
                else:
                    store.mark_stored(FINDING)
            with pytest.raises(OutboxPersistenceError):
                store.pending()
            assert path.read_bytes() == previous_image
            assert not list(tmp_path.glob(".*.checkpoint"))
        finally:
            store.close()  # Must not retry or publish the unpersisted state.
    with FindingStore(path) as reopened:
        assert [(record["finding_id"], record["state"]) for record in reopened.records()] == previous_records


def test_crash_after_gateway_ack_retries_idempotently(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    signer = EIP191Signer()
    store = FindingStore(path)
    finding_id = store.enqueue(body())
    simulator = LocalPublicationSimulator(store, signer)
    registration = simulator.registration_for(store.get(finding_id)["body"])
    assert simulator.client.post(GATEWAY_URL + "/v1/finding-records",
                                 content=canonical(registration)).status_code == 201
    assert store.get(finding_id)["state"] == "QUEUED"  # ACK not persisted yet.
    backend = simulator.backend
    simulator.close()
    store.close()
    with FindingStore(path) as reopened:
        with LocalPublicationSimulator(reopened, signer, backend=backend) as worker:
            report = worker.publish_all()
            assert [(item["stage"], item["status"]) for item in report["requests"]] == [
                ("register", 200), ("body", 201),
            ]
            assert report["pending"] == 0
            assert len(backend.registered_records) == len(backend.stored_bodies) == 1


def test_same_id_conflicts_but_equivalent_manual_rerun_deduplicates(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        original = body()
        assert store.enqueue(original) == FINDING
        assert store.enqueue(original) == FINDING
        changed = copy.deepcopy(original)
        changed["summary"] = "Changed text under the same ID."
        with pytest.raises(FindingConflict):
            store.enqueue(changed)
        manual = copy.deepcopy(original)
        manual.update(finding_id=SECOND_FINDING, trigger="MANUAL",
                      run_request_id="run_" + "7" * 26, checked_at="2026-10-27T12:01:00Z")
        assert store.enqueue(manual) == FINDING
        assert len(store.records()) == 1
        assert store.get(FINDING)["body"] == original


def test_election_identity_prevents_cross_election_deduplication(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        other = body(election_id=OTHER_ELECTION, finding_id=SECOND_FINDING)
        assert store.enqueue(other) == SECOND_FINDING
        assert len(store.pending()) == 2


def test_later_fail_links_previous_without_changing_prior_or_input_bytes(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        first = body(result="FAIL", observed={"accepted_duplicate_tokens": 1})
        store.enqueue(first)
        first_bytes = store.get(FINDING)["body_bytes"]
        later = body(finding_id=SECOND_FINDING, checkpoint=3, result="FAIL",
                     observed={"accepted_duplicate_tokens": 1})
        original_later = canonical(later)
        store.enqueue(later)
        assert canonical(later) == original_later
        assert "previous_finding_id" not in later
        final_later = store.get(SECOND_FINDING)["body"]
        assert final_later["previous_finding_id"] == FINDING
        assert store.get(FINDING)["body_bytes"] == first_bytes
        assert store.enqueue(final_later) == SECOND_FINDING
        other = body(election_id=OTHER_ELECTION, finding_id=THIRD_FINDING, checkpoint=4,
                     result="FAIL", observed={"accepted_duplicate_tokens": 1})
        store.enqueue(other)
        assert "previous_finding_id" not in store.get(THIRD_FINDING)["body"]


def test_state_machine_forbids_store_before_register(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        with pytest.raises(ValueError, match="registered before"):
            store.mark_stored(FINDING)
        store.mark_registered(FINDING)
        store.mark_stored(FINDING)
        store.mark_registered(FINDING)
        assert store.get(FINDING)["state"] == "STORED"


def test_stored_body_corruption_is_detected_before_publication(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    with FindingStore(path) as store:
        store.enqueue(body())
    with closing(sqlite3.connect(path)) as connection, connection:
        altered = body()
        altered["summary"] = "Tampered after durable storage."
        connection.execute("UPDATE findings SET body = ? WHERE finding_id = ?",
                           (canonical(altered), FINDING))
    with FindingStore(path) as store:
        with pytest.raises(StoredBodyCorrupt):
            store.pending()


def test_signature_and_registered_signer_are_both_verified(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        signer = EIP191Signer()
        with LocalPublicationSimulator(store, signer) as simulator:
            correct = simulator.registration_for(body())
            tampered = copy.deepcopy(correct)
            tampered["finding_hash"] = "a" * 64
            response = simulator.client.post(GATEWAY_URL + "/v1/finding-records",
                                             content=canonical(tampered))
            assert (response.status_code, response.json()["code"]) == (422, "SIGNATURE_INVALID")
            outsider = EIP191Signer()
            digest = finding_record_digest(FINDING, ELECTION, "R1", "PASS", 2, correct["finding_hash"])
            foreign = {**correct, "signature": outsider.sign_digest(digest)}
            response = simulator.client.post(GATEWAY_URL + "/v1/finding-records", content=canonical(foreign))
            assert response.json()["code"] == "SIGNATURE_INVALID"
            assert simulator.backend.registered_records == {}


def test_registration_conflict_and_body_hash_tampering_are_rejected(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        with LocalPublicationSimulator(store, EIP191Signer()) as simulator:
            registration = simulator.registration_for(body())
            assert simulator.client.post(GATEWAY_URL + "/v1/finding-records",
                                         content=canonical(registration)).status_code == 201
            changed = body()
            changed["summary"] = "Different body from the registered one."
            changed_registration = simulator.registration_for(changed)
            assert simulator.client.post(GATEWAY_URL + "/v1/finding-records",
                                         content=canonical(changed_registration)).status_code == 409
            response = simulator.client.post(CORE_URL + "/internal/v1/findings", content=canonical(changed))
            assert (response.status_code, response.json()["code"]) == (422, "FINDING_NOT_RECORDED")
            assert simulator.backend.stored_bodies == {}
            raw = store.get(FINDING)["body_bytes"]
            assert simulator.client.post(CORE_URL + "/internal/v1/findings", content=raw).status_code == 201
            assert simulator.client.post(CORE_URL + "/internal/v1/findings", content=raw).status_code == 200
            assert simulator.client.post(CORE_URL + "/internal/v1/findings",
                                         content=canonical(changed)).status_code == 409


def test_body_before_registration_is_retryable_not_accepted(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        with LocalPublicationSimulator(store, EIP191Signer()) as simulator:
            response = simulator.client.post(CORE_URL + "/internal/v1/findings", content=canonical(body()))
            assert response.status_code == 503
            assert simulator.backend.stored_bodies == {}


def test_service_key_and_frozen_registration_schema_are_enforced(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        with LocalPublicationSimulator(store, EIP191Signer()) as simulator:
            registration = simulator.registration_for(body())
            denied = simulator.client.post(GATEWAY_URL + "/v1/finding-records",
                                           content=canonical(registration), headers={"X-Service-Key": "wrong"})
            assert denied.status_code == 403
            invalid = {**registration, "unfrozen_new_field": True}
            rejected = simulator.client.post(GATEWAY_URL + "/v1/finding-records", content=canonical(invalid))
            assert rejected.status_code == 400
            assert simulator.backend.registered_records == {}


@pytest.mark.parametrize("invalid_id", [
    "evr_PRIVATE_MARKER_DO_NOT_LOG",
    {"secret": "PRIVATE_MARKER_DO_NOT_LOG"},
    FINDING + "\n",
])
def test_invalid_request_identifiers_never_enter_request_metadata(tmp_path, invalid_id):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        with LocalPublicationSimulator(store, EIP191Signer()) as simulator:
            response = simulator.client.post(
                GATEWAY_URL + "/v1/finding-records",
                content=canonical({"finding_id": invalid_id}),
            )
            assert response.status_code == 400
            assert simulator.backend.requests[-1]["finding_id"] is None
            assert "PRIVATE_MARKER_DO_NOT_LOG" not in json.dumps(simulator.backend.requests)


@pytest.mark.parametrize("acknowledgment", [
    httpx.Response(201, text="<html>Not a Gateway acknowledgment</html>"),
    httpx.Response(201, json={}),
    httpx.Response(201, json={"event_id": "evr_" + "3" * 26}),
])
def test_malformed_registration_ack_does_not_advance_durable_state(tmp_path, monkeypatch, acknowledgment):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        original_bytes = store.get(FINDING)["body_bytes"]
        with LocalPublicationSimulator(store, EIP191Signer()) as simulator:
            monkeypatch.setattr(simulator.backend, "_register", lambda submitted: acknowledgment)
            with pytest.raises(PublicationError) as failure:
                simulator.publish_all()
            assert failure.value.code == "INVALID_RESPONSE"
            assert store.get(FINDING)["state"] == "QUEUED"
            assert store.get(FINDING)["body_bytes"] == original_bytes
            assert [request["stage"] for request in simulator.backend.requests] == ["register"]
            assert simulator.backend.stored_bodies == {}


def test_empty_gateway_200_and_core_201_remain_valid_acknowledgments(tmp_path, monkeypatch):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        with LocalPublicationSimulator(store, EIP191Signer()) as simulator:
            registration = simulator.registration_for(body())
            assert simulator.client.post(
                GATEWAY_URL + "/v1/finding-records", content=canonical(registration)
            ).status_code == 201
            original_register = simulator.backend._register
            original_store = simulator.backend._store_body

            def empty_registration_ack(submitted):
                response = original_register(submitted)
                assert response.status_code == 200
                return httpx.Response(200)

            def empty_body_ack(submitted, raw):
                response = original_store(submitted, raw)
                assert response.status_code == 201
                return httpx.Response(201)

            monkeypatch.setattr(simulator.backend, "_register", empty_registration_ack)
            monkeypatch.setattr(simulator.backend, "_store_body", empty_body_ack)
            assert simulator.publish_all()["pending"] == 0
            assert store.get(FINDING)["state"] == "STORED"


@pytest.mark.parametrize("response", [
    httpx.Response(422, json={"code": {"secret": "PRIVATE_MARKER_DO_NOT_LOG"}}),
    httpx.Response(422, json={"code": "PRIVATE_MARKER_DO_NOT_LOG"}),
    httpx.Response(422, text="PRIVATE_MARKER_DO_NOT_LOG"),
])
def test_arbitrary_error_response_content_never_becomes_a_logged_code(tmp_path, monkeypatch, response):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        with LocalPublicationSimulator(store, EIP191Signer()) as simulator:
            monkeypatch.setattr(simulator.backend, "_register", lambda submitted: response)
            with pytest.raises(PublicationError) as failure:
                simulator.publish_all()
            assert failure.value.code == "HTTP_ERROR"
            assert "PRIVATE_MARKER_DO_NOT_LOG" not in str(failure.value)
            assert store.get(FINDING)["state"] == "QUEUED"


def test_future_checkpoint_is_permanent_error_and_stays_queued(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body(checkpoint=3))
        with LocalPublicationSimulator(store, EIP191Signer(), committed_checkpoints={ELECTION: 2}) as simulator:
            with pytest.raises(PublicationError) as failure:
                simulator.publish_all()
            assert failure.value.status == 422
            assert failure.value.code == "CHECKPOINT_NOT_FOUND"
            assert store.get(FINDING)["state"] == "QUEUED"
            assert simulator.backend.stored_bodies == {}


def test_gateway_outage_remains_queued_without_pretending_direct_chain_fallback(tmp_path):
    with FindingStore(tmp_path / "outbox.sqlite3") as store:
        store.enqueue(body())
        with LocalPublicationSimulator(store, EIP191Signer(), transient_gateway_failures=1) as simulator:
            first = simulator.publish_all()
            assert first["direct_chain_fallback"] is False
            assert (first["registered"], first["stored"], first["pending"]) == (0, 0, 1)
            assert store.get(FINDING)["state"] == "QUEUED"
            assert simulator.publish_all()["pending"] == 0


@pytest.mark.parametrize("accepted,available,anchored,elapsed,interval,expected", [
    (False, True, True, 1000, 60, {"run": False, "alert": "AUDIT_DELAYED", "anchor_timeout": False}),
    (True, False, True, 1000, 60, {"run": False, "alert": "LEDGER_UNAVAILABLE", "anchor_timeout": False}),
    (True, True, True, 0, 60, {"run": True, "alert": None, "anchor_timeout": False}),
    (True, True, False, 179.999, 60, {"run": False, "alert": "AUDIT_DELAYED", "anchor_timeout": False}),
    (True, True, False, 180, 60, {"run": True, "alert": None, "anchor_timeout": True}),
    (True, True, False, 10000, 60, {"run": True, "alert": None, "anchor_timeout": True}),
    (True, True, False, 119.999, 30, {"run": False, "alert": "AUDIT_DELAYED", "anchor_timeout": False}),
    (True, True, False, 120, 30, {"run": True, "alert": None, "anchor_timeout": True}),
])
def test_scheduler_readiness_and_exact_timeout_boundary(accepted, available, anchored, elapsed, interval, expected):
    assert run_decision(accepted, available, anchored, elapsed, interval) == expected


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf")])
def test_scheduler_rejects_invalid_elapsed_time(elapsed):
    with pytest.raises(ValueError):
        run_decision(True, True, False, elapsed)
