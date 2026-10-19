"""Fake-provider tests for the generation-conditional GCP run ledger."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sys
import types
import unittest
from unittest.mock import patch

from gcp_token_identity import IdentityVerificationError
from run_contract import ContractError, _audit_summary, operation_id
from recovery_controller import ProviderReceipt, RecoveryController
from run_ledger import (
    GenerationConflict,
    GcsObjectStore,
    LedgerConflict,
    LedgerError,
    LedgerNotFound,
    LeaseBusy,
    RunLedger,
    _decode_envelope,
    object_name,
)
from test_run_contract import RUN_ID, intent


class FakeIdentityResponse:
    status_code = 200

    def __init__(self, email):
        self.email = email

    def json(self):
        return {
            "sub": "12345678901234567890",
            "email": self.email,
            "email_verified": True,
        }


class FakeIdentityTransport:
    def __init__(self, email="project-reclaimer@example.com"):
        self.email = email
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeIdentityResponse(self.email)


def current_intent():
    value = intent()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    value["created_at"] = (
        now - timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    value["deadline_at"] = (
        now + timedelta(minutes=49)
    ).isoformat().replace("+00:00", "Z")
    value["finance"]["credit_observed_at"] = (
        now - timedelta(minutes=2)
    ).isoformat().replace("+00:00", "Z")
    value["finance"]["credit_expires_at"] = (
        now + timedelta(days=30)
    ).isoformat().replace("+00:00", "Z")
    return value


class FakeObjectStore:
    def __init__(self):
        self.objects = {}
        self.next_generation = 1
        self.list_calls = 0
        self.fail_after_create = False
        self.fail_after_replace = False
        self.before_replace = None

    def read(self, name):
        try:
            content, generation = self.objects[name]
        except KeyError as exc:
            raise LedgerNotFound(name) from exc
        return content, generation

    def create(self, name, content):
        if name in self.objects:
            raise GenerationConflict(name)
        generation = self.next_generation
        self.next_generation += 1
        self.objects[name] = (content, generation)
        if self.fail_after_create:
            self.fail_after_create = False
            # Models a committed conditional write whose response was lost.
            raise ConnectionError("simulated lost create response")
        return generation

    def replace(self, name, content, *, generation):
        if self.before_replace is not None:
            hook, self.before_replace = self.before_replace, None
            hook(self, name)
        try:
            _, current_generation = self.objects[name]
        except KeyError as exc:
            raise LedgerNotFound(name) from exc
        if current_generation != generation:
            raise GenerationConflict(name)
        new_generation = self.next_generation
        self.next_generation += 1
        self.objects[name] = (content, new_generation)
        if self.fail_after_replace:
            self.fail_after_replace = False
            raise ConnectionError("simulated lost replacement response")
        return new_generation


class FakeProvider:
    """Exact-ID fake provider that can lose a committed create response."""

    def __init__(self):
        self.resources = set()
        self.create_calls = 0
        self.lookups = []

    def create(self, resource_id, *, lose_response=False):
        self.create_calls += 1
        self.resources.add(resource_id)
        if lose_response:
            raise ConnectionError("simulated lost provider response")
        return {"name": resource_id, "status": "created"}

    def get(self, resource_id):
        self.lookups.append(resource_id)
        return {"name": resource_id, "status": "present"} if resource_id in self.resources else None


class RunLedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeObjectStore()
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.ledger = RunLedger(self.store, clock=lambda: self.now)
        self.intent = current_intent()

    def create_and_lease(self):
        created = self.ledger.create(self.intent)
        return self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=created.generation,
            owner="launcher",
            now=self.now,
        )

    def lease_in_provisioning(self):
        snapshot = self.create_and_lease()
        states = [
            "prepared", "project_create_requested", "project_created",
            "billing_link_requested", "project_linked", "api_enablement",
            "provisioning",
        ]
        for index, state in enumerate(states, start=1):
            candidate = deepcopy(snapshot.manifest)
            candidate["state"] = state
            if state == "prepared":
                for control in candidate["controls"]:
                    control["status"] = "present"
            if state == "provisioning":
                candidate["resources"] = [
                    {
                        "type": "gcp.compute.network",
                        "id": (
                            f"projects/{self.intent['scope']['project_id']}"
                            f"/global/networks/{RUN_ID}-network"
                        ),
                        "ownership_proof": "run_name",
                        "status": "expected",
                        "delete_attempts": 0,
                    }
                ]
            snapshot = self.ledger.replace(
                RUN_ID,
                expected_generation=snapshot.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=self.now + timedelta(seconds=index),
            )
        return snapshot

    def lease_in_auditing(self):
        snapshot = self.lease_in_provisioning()
        for index, state in enumerate(
            ["ready", "teardown_requested", "cleaning", "auditing"], start=20
        ):
            if state == "auditing":
                resource_id = snapshot.manifest["resources"][0]["id"]
                intent_snapshot = self.ledger.record_operation_intent(
                    RUN_ID,
                    expected_generation=snapshot.generation,
                    lease_owner="launcher",
                    attempt=1,
                    action="delete",
                    resource_type="gcp.compute.network",
                    resource_id=resource_id,
                    request_sha256="0" * 64,
                    now=self.now + timedelta(seconds=index),
                )
                operation = next(
                    event for event in intent_snapshot.manifest["operations"]
                    if event["kind"] == "intent"
                )
                snapshot = self.ledger.record_operation_result(
                    RUN_ID,
                    expected_generation=intent_snapshot.generation,
                    lease_owner="launcher",
                    operation_id_value=operation["operation_id"],
                    outcome="succeeded",
                    response_sha256="f" * 64,
                    now=self.now + timedelta(seconds=index),
                )
            candidate = deepcopy(snapshot.manifest)
            candidate["state"] = state
            snapshot = self.ledger.replace(
                RUN_ID,
                expected_generation=snapshot.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=self.now + timedelta(seconds=index),
            )
        snapshot = self.ledger.release_lease(
            RUN_ID,
            expected_generation=snapshot.generation,
            owner="launcher",
            now=self.now + timedelta(seconds=30),
        )
        return self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=snapshot.generation,
            owner="auditor:google-user-12345678901234567890",
            now=self.now + timedelta(seconds=31),
        )

    def test_object_path_is_exact_and_rejects_path_injection(self):
        self.assertEqual(object_name(RUN_ID), f"runs/{RUN_ID}/manifest.json")
        for invalid in ("../other", "lab-a/../../other", "LAB-UPPER", ""):
            with self.subTest(run_id=invalid), self.assertRaises(ContractError):
                object_name(invalid)

    def test_create_only_write_and_exact_path_read(self):
        created = self.ledger.create(self.intent)
        self.assertEqual(created.generation, 1)
        self.assertEqual(created.manifest["run_id"], RUN_ID)
        self.assertEqual(list(self.store.objects), [object_name(RUN_ID)])
        loaded = self.ledger.read(RUN_ID)
        self.assertEqual(loaded, created)
        self.assertEqual(created.metadata["event_sequence"], 1)
        self.assertEqual(created.metadata["events"][0]["type"], "run_created")

    def test_retry_after_committed_create_response_loss_reconciles_exact_object(self):
        self.store.fail_after_create = True
        with self.assertRaises(ConnectionError):
            self.ledger.create(self.intent)
        first = self.ledger.create(self.intent)
        self.assertEqual(first.generation, 1)
        self.assertEqual(self.ledger.read(RUN_ID), first)

    def test_expired_identical_retry_can_reconcile_but_cannot_create(self):
        created = self.ledger.create(self.intent)
        deadline = datetime.fromisoformat(
            self.intent["deadline_at"].replace("Z", "+00:00")
        )
        self.now = deadline + timedelta(seconds=1)
        self.assertEqual(self.ledger.create(self.intent), created)

        new_intent = deepcopy(self.intent)
        new_intent["run_id"] = "lab-20261019-b2c3"
        with self.assertRaises(ContractError):
            self.ledger.create(new_intent)

    def test_create_refuses_different_payload_for_same_run_id(self):
        self.ledger.create(self.intent)
        changed = deepcopy(self.intent)
        changed["source"]["revision"] = "c" * 40
        with self.assertRaises(LedgerConflict):
            self.ledger.create(changed)

    def test_missing_exact_manifest_fails_without_listing(self):
        with self.assertRaises(LedgerNotFound):
            self.ledger.read(RUN_ID)
        self.assertEqual(self.store.list_calls, 0)

    def test_compare_and_swap_updates_only_expected_generation(self):
        original = self.create_and_lease()
        candidate = deepcopy(original.manifest)
        candidate["state"] = "prepared"
        for control in candidate["controls"]:
            control["status"] = "present"
        updated = self.ledger.replace(
            RUN_ID,
            expected_generation=original.generation,
            lease_owner="launcher",
            manifest=candidate,
        )
        self.assertGreater(updated.generation, original.generation)
        self.assertEqual(updated.manifest["state"], "prepared")
        self.assertEqual(
            self.ledger.replace(
                RUN_ID,
                expected_generation=original.generation,
                lease_owner="launcher",
                manifest=candidate,
            ),
            updated,
        )
        changed = deepcopy(candidate)
        changed["finance"]["monthly_alert_threshold_gbp"] = 71
        with self.assertRaises(GenerationConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=original.generation,
                lease_owner="launcher",
                manifest=changed,
            )

    def test_resource_inventory_cannot_be_extended_or_status_changed_generically(self):
        snapshot = self.lease_in_provisioning()
        candidate = deepcopy(snapshot.manifest)
        candidate["resources"].append({
            "type": "gcp.compute.network",
            "id": (
                f"projects/{self.intent['scope']['project_id']}"
                f"/global/networks/{RUN_ID}-unregistered"
            ),
            "ownership_proof": "run_name",
            "status": "present",
            "delete_attempts": 0,
        })
        with self.assertRaisesRegex(LedgerConflict, "discovered by audit"):
            self.ledger.replace(
                RUN_ID,
                expected_generation=snapshot.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=self.now + timedelta(seconds=20),
            )

        candidate = deepcopy(snapshot.manifest)
        candidate["resources"][0]["status"] = "deleted"
        with self.assertRaisesRegex(LedgerConflict, "terminal provider result"):
            self.ledger.replace(
                RUN_ID,
                expected_generation=snapshot.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=self.now + timedelta(seconds=20),
            )

    def test_operation_intent_precedes_result_and_retries_reconcile_exactly(self):
        leased = self.lease_in_provisioning()
        resource_id = f"projects/{self.intent['scope']['project_id']}/global/networks/{RUN_ID}-network"
        request_sha = "c" * 64
        op_id = operation_id(
            RUN_ID, 1, "create", "gcp.compute.network", resource_id, request_sha,
            "provisioning",
        )
        recorded = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=leased.generation,
            lease_owner="launcher",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(seconds=8),
        )
        self.assertEqual(recorded.manifest["operations"][0]["operation_id"], op_id)
        self.assertEqual(recorded.manifest["operations"][0]["kind"], "intent")
        for bad_attempt, bad_fingerprint in ((True, request_sha), (1, "not-a-digest")):
            with self.subTest(attempt=bad_attempt, fingerprint=bad_fingerprint), self.assertRaises(ContractError):
                self.ledger.record_operation_intent(
                    RUN_ID,
                    expected_generation=recorded.generation,
                    lease_owner="launcher",
                    attempt=bad_attempt,
                    action="create",
                    resource_type="gcp.compute.network",
                    resource_id=resource_id,
                    request_sha256=bad_fingerprint,
                    now=self.now + timedelta(seconds=9),
                )

        # Lost ledger response retry returns the same pending intent and cannot
        # append a second request or change its deterministic identity.
        replayed = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=leased.generation,
            lease_owner="launcher",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(seconds=8),
        )
        self.assertEqual(replayed, recorded)
        self.assertEqual(len(replayed.manifest["operations"]), 1)

        resolved = self.ledger.record_operation_result(
            RUN_ID,
            expected_generation=recorded.generation,
            lease_owner="launcher",
            operation_id_value=op_id,
            outcome="reconciled_present",
            response_sha256="d" * 64,
            now=self.now + timedelta(seconds=9),
        )
        self.assertEqual(resolved.manifest["operations"][-1]["kind"], "result")
        retried_result = self.ledger.record_operation_result(
            RUN_ID,
            expected_generation=recorded.generation,
            lease_owner="launcher",
            operation_id_value=op_id,
            outcome="reconciled_present",
            response_sha256="d" * 64,
            now=self.now + timedelta(seconds=9),
        )
        self.assertEqual(retried_result, resolved)
        self.assertEqual(len(retried_result.manifest["operations"]), 2)

        next_attempt = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=resolved.generation,
            lease_owner="launcher",
            attempt=2,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(seconds=10),
        )
        self.assertNotEqual(next_attempt.manifest["operations"][-1]["operation_id"], op_id)

    def test_unresolved_operation_blocks_a_second_provider_attempt(self):
        leased = self.lease_in_provisioning()
        resource_id = f"projects/{self.intent['scope']['project_id']}/global/networks/{RUN_ID}-network"
        request_sha = "c" * 64
        recorded = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=leased.generation,
            lease_owner="launcher",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(seconds=8),
        )
        with self.assertRaises(LedgerConflict):
            self.ledger.record_operation_intent(
                RUN_ID,
                expected_generation=recorded.generation,
                lease_owner="launcher",
                attempt=1,
                action="create",
                resource_type="gcp.compute.network",
                resource_id=resource_id,
                request_sha256="d" * 64,
                now=self.now + timedelta(seconds=9),
            )
        with self.assertRaises(ContractError):
            self.ledger.record_operation_intent(
                RUN_ID,
                expected_generation=recorded.generation,
                lease_owner="launcher",
                attempt=2,
                action="create",
                resource_type="gcp.compute.network",
                resource_id=resource_id,
                request_sha256=request_sha,
                now=self.now + timedelta(seconds=9),
            )

    def test_pending_create_can_reconcile_after_deadline_but_new_attempt_cannot(self):
        self.intent["deadline_at"] = (
            self.now + timedelta(seconds=10)
        ).isoformat().replace("+00:00", "Z")
        leased = self.lease_in_provisioning()
        resource_id = (
            f"projects/{self.intent['scope']['project_id']}"
            f"/global/networks/{RUN_ID}-network"
        )
        request_sha = "c" * 64
        pending = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=leased.generation,
            lease_owner="launcher",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(seconds=8),
        )
        replayed = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=pending.generation,
            lease_owner="launcher",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(seconds=11),
        )
        self.assertEqual(replayed, pending)
        resolved = self.ledger.record_operation_result(
            RUN_ID,
            expected_generation=replayed.generation,
            lease_owner="launcher",
            operation_id_value=pending.manifest["operations"][0]["operation_id"],
            outcome="reconciled_present",
            response_sha256="d" * 64,
            now=self.now + timedelta(seconds=12),
        )
        with self.assertRaises(ContractError):
            self.ledger.record_operation_intent(
                RUN_ID,
                expected_generation=resolved.generation,
                lease_owner="launcher",
                attempt=2,
                action="create",
                resource_type="gcp.compute.network",
                resource_id=resource_id,
                request_sha256=request_sha,
                now=self.now + timedelta(seconds=13),
            )
    def test_long_running_operation_receipt_is_append_only_and_idempotent(self):
        leased = self.lease_in_provisioning()
        resource_id = f"projects/{self.intent['scope']['project_id']}/global/networks/{RUN_ID}-network"
        request_sha = "c" * 64
        recorded = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=leased.generation,
            lease_owner="launcher",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(seconds=8),
        )
        provider_operation_name = (
            f"projects/{self.intent['scope']['project_id']}/regions/"
            "europe-west2/operations/op-123"
        )
        progress = self.ledger.record_operation_progress(
            RUN_ID,
            expected_generation=recorded.generation,
            lease_owner="launcher",
            operation_id_value=recorded.manifest["operations"][0]["operation_id"],
            provider_operation_name=provider_operation_name,
            response_sha256="d" * 64,
            now=self.now + timedelta(seconds=9),
        )
        replayed = self.ledger.record_operation_progress(
            RUN_ID,
            expected_generation=recorded.generation,
            lease_owner="launcher",
            operation_id_value=recorded.manifest["operations"][0]["operation_id"],
            provider_operation_name=provider_operation_name,
            response_sha256="d" * 64,
            now=self.now + timedelta(seconds=9),
        )
        self.assertEqual(replayed, progress)
        with self.assertRaises(LedgerConflict):
            self.ledger.record_operation_progress(
                RUN_ID,
                expected_generation=progress.generation,
                lease_owner="launcher",
                operation_id_value=recorded.manifest["operations"][0]["operation_id"],
                provider_operation_name=provider_operation_name + "-changed",
                response_sha256="e" * 64,
                now=self.now + timedelta(seconds=10),
            )
        complete = self.ledger.record_operation_result(
            RUN_ID,
            expected_generation=progress.generation,
            lease_owner="launcher",
            operation_id_value=recorded.manifest["operations"][0]["operation_id"],
            outcome="succeeded",
            provider_operation_name=provider_operation_name,
            response_sha256="f" * 64,
            now=self.now + timedelta(seconds=10),
        )
        self.assertEqual(
            [event["kind"] for event in complete.manifest["operations"]],
            ["intent", "progress", "result"],
        )

    def test_takeover_reconciles_lost_create_response_by_exact_id(self):
        provider = FakeProvider()
        leased = self.lease_in_provisioning()
        resource_id = f"projects/{self.intent['scope']['project_id']}/global/networks/{RUN_ID}-network"
        request_sha = "c" * 64
        intent_at = self.now + timedelta(seconds=8)
        pending = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=leased.generation,
            lease_owner="launcher",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=intent_at,
        )
        checked = self.ledger.assert_active_lease(
            RUN_ID,
            expected_generation=pending.generation,
            owner="launcher",
            now=self.now + timedelta(seconds=9),
        )
        self.assertEqual(checked, pending)
        with self.assertRaises(ConnectionError):
            provider.create(resource_id, lose_response=True)

        takeover = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=pending.generation,
            owner="reaper",
            now=self.now + timedelta(minutes=3),
        )
        incomplete = deepcopy(takeover.manifest)
        incomplete["state"] = "incomplete"
        takeover = self.ledger.replace(
            RUN_ID,
            expected_generation=takeover.generation,
            lease_owner="reaper",
            manifest=incomplete,
            now=self.now + timedelta(minutes=3, seconds=1),
        )
        with self.assertRaises(LedgerConflict):
            self.ledger.assert_active_lease(
                RUN_ID,
                expected_generation=pending.generation,
                owner="launcher",
                now=self.now + timedelta(minutes=3, seconds=2),
            )
        reconciled_intent = self.ledger.record_operation_intent(
            RUN_ID,
            expected_generation=takeover.generation,
            lease_owner="reaper",
            attempt=1,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=resource_id,
            request_sha256=request_sha,
            now=self.now + timedelta(minutes=3, seconds=2),
        )
        self.assertEqual(reconciled_intent.manifest["operations"], pending.manifest["operations"])
        self.assertEqual(reconciled_intent.manifest["state"], "incomplete")
        found = provider.get(resource_id)
        self.assertEqual(found["status"], "present")
        completed = self.ledger.record_operation_result(
            RUN_ID,
            expected_generation=takeover.generation,
            lease_owner="reaper",
            operation_id_value=pending.manifest["operations"][0]["operation_id"],
            outcome="reconciled_present",
            response_sha256="e" * 64,
            now=self.now + timedelta(minutes=3, seconds=2),
        )
        self.assertEqual(completed.manifest["operations"][-1]["outcome"], "reconciled_present")
        self.assertEqual(provider.create_calls, 1)
        self.assertEqual(provider.lookups, [resource_id])

    def test_retry_after_committed_replace_response_loss_reconciles_exact_generation(self):
        original = self.create_and_lease()
        candidate = deepcopy(original.manifest)
        candidate["state"] = "prepared"
        for control in candidate["controls"]:
            control["status"] = "present"
        self.store.fail_after_replace = True
        with self.assertRaises(ConnectionError):
            self.ledger.replace(
                RUN_ID,
                expected_generation=original.generation,
                lease_owner="launcher",
                manifest=candidate,
            )
        reconciled = self.ledger.replace(
            RUN_ID,
            expected_generation=original.generation,
            lease_owner="launcher",
            manifest=candidate,
        )
        self.assertEqual(reconciled.manifest["state"], "prepared")
        self.assertGreater(reconciled.generation, original.generation)

    def test_provider_side_race_is_rejected_by_storage_precondition(self):
        original = self.create_and_lease()
        candidate = deepcopy(original.manifest)
        candidate["state"] = "prepared"
        for control in candidate["controls"]:
            control["status"] = "present"

        def concurrent_writer(store, name):
            _, old_generation = store.objects[name]
            newer_generation = store.next_generation
            store.next_generation += 1
            store.objects[name] = (store.objects[name][0], newer_generation)
            self.assertNotEqual(old_generation, newer_generation)

        self.store.before_replace = concurrent_writer
        with self.assertRaises(GenerationConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=original.generation,
                lease_owner="launcher",
                manifest=candidate,
            )
        self.assertEqual(self.ledger.read(RUN_ID).manifest["state"], "control_bootstrapping")

    def test_replacement_cannot_change_identity_or_skip_a_state(self):
        original = self.create_and_lease()
        changed = deepcopy(original.manifest)
        changed["scope"]["project_id"] = "other-lab-123"
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=original.generation,
                lease_owner="launcher",
                manifest=changed,
            )

        skipped = deepcopy(original.manifest)
        skipped["state"] = "ready"
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=original.generation,
                lease_owner="launcher",
                manifest=skipped,
            )

        moved_backend = deepcopy(original.manifest)
        moved_backend["terraform_state"]["backend"]["bucket"] = "other-state-bucket"
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=original.generation,
                lease_owner="launcher",
                manifest=moved_backend,
            )

    def test_restore_proof_history_is_append_only(self):
        original = self.create_and_lease()
        candidate = deepcopy(original.manifest)
        proof = {
            "snapshot": "current",
            "state_generation": "12",
            "backup_generation": "5",
            "restored_generation": "8",
            "verified_at": "2026-10-18T12:01:00Z",
            "result": "passed",
            "state_crc32c": "ImIEBA==",
            "backup_crc32c": "ImIEBA==",
            "restored_crc32c": "ImIEBA==",
        }
        candidate["terraform_state"]["restore_proofs"].append(proof)
        recorded = self.ledger.replace(
            RUN_ID,
            expected_generation=original.generation,
            lease_owner="launcher",
            manifest=candidate,
            now=self.now + timedelta(seconds=1),
        )

        erased = deepcopy(recorded.manifest)
        erased["terraform_state"]["restore_proofs"] = []
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=recorded.generation,
                lease_owner="launcher",
                manifest=erased,
                now=self.now + timedelta(seconds=2),
            )

    def test_independent_audit_receipt_is_generation_bound_and_append_only(self):
        snapshot = self.lease_in_auditing()
        checks = {"resource_inventory_complete": True, "resources_absent": True}
        audited = self.ledger.record_independent_audit(
            RUN_ID,
            expected_generation=snapshot.generation,
            lease_owner="auditor:google-user-12345678901234567890",
            auditor_id="google-user-12345678901234567890",
            phase="workload",
            result="passed",
            complete=True,
            inventory_sha256="a" * 64,
            evidence_sha256="b" * 64,
            checks=checks,
            now=self.now + timedelta(seconds=32),
        )
        receipt = audited.manifest["audit_receipts"][0]
        self.assertEqual(receipt["manifest_generation"], snapshot.generation)
        self.assertEqual(
            audited.manifest["independent_audit"]["phase"], "workload"
        )
        changed = deepcopy(audited.manifest)
        changed["audit_receipts"][0]["inventory_sha256"] = "c" * 64
        receipt_payload = {
            key: value
            for key, value in changed["audit_receipts"][0].items()
            if key != "receipt_id"
        }
        changed["audit_receipts"][0]["receipt_id"] = hashlib.sha256(
            json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        changed["independent_audit"] = _audit_summary(changed["audit_receipts"][0])
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=audited.generation,
                lease_owner="auditor:google-user-12345678901234567890",
                manifest=changed,
                now=self.now + timedelta(seconds=33),
            )

    def test_independent_audit_retry_reconciles_a_committed_lost_response(self):
        snapshot = self.lease_in_auditing()
        self.store.fail_after_replace = True
        arguments = {
            "expected_generation": snapshot.generation,
            "lease_owner": "auditor:google-user-12345678901234567890",
            "auditor_id": "google-user-12345678901234567890",
            "phase": "workload",
            "result": "passed",
            "complete": True,
            "inventory_sha256": "a" * 64,
            "evidence_sha256": "b" * 64,
            "checks": {"resource_inventory_complete": True, "resources_absent": True},
            "now": self.now + timedelta(seconds=32),
        }
        with self.assertRaises(ConnectionError):
            self.ledger.record_independent_audit(RUN_ID, **arguments)
        retried = self.ledger.record_independent_audit(RUN_ID, **arguments)
        self.assertEqual(
            retried.manifest["audit_receipts"][0]["manifest_generation"],
            snapshot.generation,
        )
        self.assertEqual(len(retried.manifest["audit_receipts"]), 1)
        with self.assertRaises(LedgerConflict):
            self.ledger.record_independent_audit(
                RUN_ID, **{**arguments, "evidence_sha256": "c" * 64}
            )

    def test_independent_audit_requires_a_separate_named_lease_and_exact_checks(self):
        snapshot = self.lease_in_auditing()
        arguments = {
            "expected_generation": snapshot.generation,
            "auditor_id": "google-user-12345678901234567890",
            "phase": "workload",
            "result": "passed",
            "complete": True,
            "inventory_sha256": "a" * 64,
            "evidence_sha256": "b" * 64,
            "checks": {"resource_inventory_complete": True, "resources_absent": True},
            "now": self.now + timedelta(seconds=32),
        }
        with self.assertRaises(LedgerConflict):
            self.ledger.record_independent_audit(
                RUN_ID, lease_owner="launcher", **arguments
            )
        with self.assertRaises(ContractError):
            self.ledger.record_independent_audit(
                RUN_ID,
                lease_owner="auditor:google-user-12345678901234567890",
                **{**arguments, "checks": {"resources_absent": True}},
            )

    def test_closure_receipt_requires_passed_workload_audit_and_precedes_closed(self):
        snapshot = self.lease_in_auditing()
        workload = self.ledger.record_independent_audit(
            RUN_ID,
            expected_generation=snapshot.generation,
            lease_owner="auditor:google-user-12345678901234567890",
            auditor_id="google-user-12345678901234567890",
            phase="workload",
            result="passed",
            complete=True,
            inventory_sha256="a" * 64,
            evidence_sha256="b" * 64,
            checks={"resource_inventory_complete": True, "resources_absent": True},
            now=self.now + timedelta(seconds=32),
        )
        workload = self.ledger.release_lease(
            RUN_ID,
            expected_generation=workload.generation,
            owner="auditor:google-user-12345678901234567890",
            now=self.now + timedelta(seconds=33),
        )
        operator = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=workload.generation,
            owner="operator:reclaimer",
            now=self.now + timedelta(seconds=34),
        )
        candidate = deepcopy(operator.manifest)
        candidate["state"] = "project_retiring"
        operator = self.ledger.replace(
            RUN_ID,
            expected_generation=operator.generation,
            lease_owner="operator:reclaimer",
            manifest=candidate,
            now=self.now + timedelta(seconds=35),
        )

        class RetirementProvider:
            lose_project_delete_response = True
            actor_id = "google-user-12345678901234567890"
            submit_calls = 0

            def authorize_actor(self, lease_owner, action, resource_type):
                if lease_owner != "operator:project-reclaimer":
                    raise LedgerConflict(
                        "project retirement requires the named project-reclaimer lease"
                    )
                return self.actor_id

            def submit(self, action, resource_type, resource_id, request):
                self.submit_calls += 1
                if (
                    resource_type == "gcp.resourcemanager.project"
                    and self.lose_project_delete_response
                ):
                    self.lose_project_delete_response = False
                    raise ConnectionError("simulated accepted project delete with lost response")
                return ProviderReceipt("succeeded", "e" * 64)

            def inspect_resource(self, resource_type, resource_id):
                if resource_type == "gcp.resourcemanager.project":
                    return ProviderReceipt(
                        "retiring", "f" * 64,
                        "resourcemanager-delete/projects/sample-lab-123/operations/accepted",
                    )
                return ProviderReceipt("absent", "e" * 64)

            def poll_operation(self, operation_name):
                return ProviderReceipt("succeeded", "e" * 64, operation_name)

        retirement_provider = RetirementProvider()
        controller = RecoveryController(
            self.ledger, retirement_provider,
            clock=lambda: self.now + timedelta(seconds=36),
        )
        with self.assertRaisesRegex(LedgerConflict, "named project-reclaimer"):
            controller.execute(
                RUN_ID,
                lease_owner="operator:reclaimer",
                attempt=1,
                action="delete",
                resource_type="gcp.billing.project_billing_info",
                resource_id=f"projects/{self.intent['scope']['project_id']}",
                request={"billing_account_name": ""},
            )
        operator = self.ledger.release_lease(
            RUN_ID,
            expected_generation=operator.generation,
            owner="operator:reclaimer",
            now=self.now + timedelta(seconds=36),
        )
        operator = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=operator.generation,
            owner="operator:project-reclaimer",
            now=self.now + timedelta(seconds=37),
        )
        retirement_provider = RetirementProvider()
        controller = RecoveryController(
            self.ledger, retirement_provider,
            clock=lambda: self.now + timedelta(seconds=38),
        )
        billing = controller.execute(
            RUN_ID,
            lease_owner="operator:project-reclaimer",
            attempt=1,
            action="delete",
            resource_type="gcp.billing.project_billing_info",
            resource_id=f"projects/{self.intent['scope']['project_id']}",
            request={"billing_account_name": ""},
        )
        self.assertEqual(billing.state, "succeeded")
        retirement_provider.actor_id = "google-user-98765432109876543210"
        calls_before_principal_change = retirement_provider.submit_calls
        with self.assertRaisesRegex(LedgerConflict, "same verified Google user"):
            controller.execute(
                RUN_ID,
                lease_owner="operator:project-reclaimer",
                attempt=1,
                action="delete",
                resource_type="gcp.resourcemanager.project",
                resource_id=f"projects/{self.intent['scope']['project_id']}",
                request={},
            )
        self.assertEqual(
            retirement_provider.submit_calls, calls_before_principal_change
        )
        retirement_provider.actor_id = "google-user-12345678901234567890"
        with self.assertRaisesRegex(ConnectionError, "lost response"):
            controller.execute(
                RUN_ID,
                lease_owner="operator:project-reclaimer",
                attempt=1,
                action="delete",
                resource_type="gcp.resourcemanager.project",
                resource_id=f"projects/{self.intent['scope']['project_id']}",
                request={},
            )
        pending = controller.execute(
            RUN_ID,
            lease_owner="operator:project-reclaimer",
            attempt=1,
            action="delete",
            resource_type="gcp.resourcemanager.project",
            resource_id=f"projects/{self.intent['scope']['project_id']}",
            request={},
        )
        self.assertEqual(pending.state, "running")
        project = controller.execute(
            RUN_ID,
            lease_owner="operator:project-reclaimer",
            attempt=1,
            action="delete",
            resource_type="gcp.resourcemanager.project",
            resource_id=f"projects/{self.intent['scope']['project_id']}",
            request={},
        )
        self.assertEqual(project.state, "succeeded")
        operator = self.ledger.read(RUN_ID)
        retirement_intents = [
            event for event in operator.manifest["operations"]
            if event["kind"] == "intent"
            and event["phase"] == "project_retiring"
        ]
        self.assertEqual(len(retirement_intents), 2)
        self.assertEqual(
            {event["actor_id"] for event in retirement_intents},
            {"google-user-12345678901234567890"},
        )
        candidate = deepcopy(operator.manifest)
        candidate["state"] = "controls_reclaiming"
        for control in candidate["controls"]:
            control["status"] = "deleted"
        operator = self.ledger.replace(
            RUN_ID,
            expected_generation=operator.generation,
            lease_owner="operator:project-reclaimer",
            manifest=candidate,
            now=self.now + timedelta(seconds=39),
        )
        replayed_project = controller.execute(
            RUN_ID,
            lease_owner="operator:project-reclaimer",
            attempt=1,
            action="delete",
            resource_type="gcp.resourcemanager.project",
            resource_id=f"projects/{self.intent['scope']['project_id']}",
            request={},
        )
        self.assertEqual(replayed_project.state, "succeeded")
        operator = self.ledger.release_lease(
            RUN_ID,
            expected_generation=operator.generation,
            owner="operator:project-reclaimer",
            now=self.now + timedelta(seconds=40),
        )
        auditor = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=operator.generation,
            owner="auditor:google-user-12345678901234567890",
            now=self.now + timedelta(seconds=41),
        )
        closure = self.ledger.record_independent_audit(
            RUN_ID,
            expected_generation=auditor.generation,
            lease_owner="auditor:google-user-12345678901234567890",
            auditor_id="google-user-12345678901234567890",
            phase="closure",
            result="passed",
            complete=True,
            inventory_sha256="c" * 64,
            evidence_sha256="d" * 64,
            checks={
                "resource_inventory_complete": True,
                "resources_absent": True,
                "project_absent": True,
                "project_services_reconciled": True,
                "billing_unlinked": True,
                "budget_reconciled": True,
                "recovery_controls_absent": True,
            },
            now=self.now + timedelta(seconds=42),
        )
        candidate = deepcopy(closure.manifest)
        candidate["state"] = "closed"
        closed = self.ledger.replace(
            RUN_ID,
            expected_generation=closure.generation,
            lease_owner="auditor:google-user-12345678901234567890",
            manifest=candidate,
            now=self.now + timedelta(seconds=43),
        )
        self.assertEqual(closed.manifest["state"], "closed")
        self.assertEqual(
            [item["phase"] for item in closed.manifest["audit_receipts"]],
            ["workload", "closure"],
        )
        changed = deepcopy(closed.manifest)
        changed["finance"]["available_credit_gbp"] += 1
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=closed.generation,
                lease_owner="auditor:google-user-12345678901234567890",
                manifest=changed,
                now=self.now + timedelta(seconds=44),
            )

    def test_corrupt_and_duplicate_key_objects_fail_closed(self):
        with self.assertRaises(LedgerError):
            _decode_envelope(b'{"schema":"bad","schema":"duplicate"}')

        self.store.objects[object_name(RUN_ID)] = (b"not json", 1)
        with self.assertRaises(LedgerError):
            self.ledger.read(RUN_ID)

    def test_ledger_has_no_bucket_list_or_delete_operation(self):
        self.assertFalse(hasattr(self.store, "list"))
        self.assertFalse(hasattr(self.store, "delete"))

    def test_lease_excludes_foreign_owner_and_allows_cas_takeover_after_expiry(self):
        created = self.ledger.create(self.intent)
        leased = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=created.generation,
            owner="launcher",
            now=self.now,
        )
        with self.assertRaises(LeaseBusy):
            self.ledger.acquire_lease(
                RUN_ID,
                expected_generation=leased.generation,
                owner="reaper",
                now=self.now + timedelta(seconds=1),
            )
        same_owner_retry = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=created.generation,
            owner="launcher",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(same_owner_retry, leased)

        reclaimed = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=leased.generation,
            owner="reaper",
            now=self.now + timedelta(minutes=3),
        )
        self.assertEqual(reclaimed.metadata["lease"]["owner"], "reaper")
        self.assertEqual(reclaimed.metadata["events"][-1]["type"], "lease_reclaimed")
        self.assertEqual(reclaimed.metadata["event_sequence"], 3)

    def test_concurrent_lease_race_has_one_winner(self):
        created = self.ledger.create(self.intent)
        other_ledger = RunLedger(self.store, clock=lambda: self.now)

        def competing_reaper_wins(store, name):
            other_ledger.acquire_lease(
                RUN_ID,
                expected_generation=created.generation,
                owner="reaper",
                now=self.now,
            )

        self.store.before_replace = competing_reaper_wins
        with self.assertRaises(GenerationConflict):
            self.ledger.acquire_lease(
                RUN_ID,
                expected_generation=created.generation,
                owner="launcher",
                now=self.now,
            )
        self.assertEqual(
            self.ledger.read(RUN_ID).metadata["lease"]["owner"], "reaper"
        )

    def test_heartbeat_and_release_are_owned_monotonic_and_recoverable(self):
        leased = self.create_and_lease()
        heartbeat_at = self.now + timedelta(seconds=30)
        self.store.fail_after_replace = True
        with self.assertRaises(ConnectionError):
            self.ledger.heartbeat(
                RUN_ID,
                expected_generation=leased.generation,
                owner="launcher",
                now=heartbeat_at,
                ttl=timedelta(minutes=3),
            )
        heartbeat = self.ledger.heartbeat(
            RUN_ID,
            expected_generation=leased.generation,
            owner="launcher",
            now=heartbeat_at,
            ttl=timedelta(minutes=3),
        )
        self.assertEqual(heartbeat.metadata["events"][-1]["type"], "lease_heartbeat")
        self.assertEqual(
            heartbeat.metadata["last_heartbeat_at"],
            heartbeat_at.isoformat().replace("+00:00", "Z"),
        )
        with self.assertRaises(LedgerConflict):
            self.ledger.heartbeat(
                RUN_ID,
                expected_generation=heartbeat.generation,
                owner="reaper",
                now=heartbeat_at + timedelta(seconds=1),
            )
        released = self.ledger.release_lease(
            RUN_ID,
            expected_generation=heartbeat.generation,
            owner="launcher",
            now=heartbeat_at + timedelta(seconds=1),
        )
        self.assertIsNone(released.metadata["lease"]["owner"])
        self.assertEqual(released.metadata["events"][-1]["type"], "lease_released")
        self.assertEqual(
            self.ledger.release_lease(
                RUN_ID,
                expected_generation=heartbeat.generation,
                owner="launcher",
                now=heartbeat_at + timedelta(seconds=1),
            ),
            released,
        )

    def test_expired_lease_cannot_heartbeat_or_mutate_manifest(self):
        leased = self.create_and_lease()
        expired_at = self.now + timedelta(minutes=2, seconds=1)
        with self.assertRaises(LedgerConflict):
            self.ledger.heartbeat(
                RUN_ID,
                expected_generation=leased.generation,
                owner="launcher",
                now=expired_at,
            )
        candidate = deepcopy(leased.manifest)
        candidate["state"] = "prepared"
        for control in candidate["controls"]:
            control["status"] = "present"
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID,
                expected_generation=leased.generation,
                lease_owner="launcher",
                manifest=candidate,
                now=expired_at,
            )

    def test_lease_ttl_and_owner_are_bounded(self):
        created = self.ledger.create(self.intent)
        with self.assertRaises(LedgerError):
            self.ledger.acquire_lease(
                RUN_ID,
                expected_generation=True,
                owner="launcher",
                now=self.now,
            )
        with self.assertRaises(LedgerError):
            self.ledger.acquire_lease(
                RUN_ID,
                expected_generation=created.generation,
                owner="not-an-owner",
                now=self.now,
            )
        with self.assertRaises(LedgerError):
            self.ledger.acquire_lease(
                RUN_ID,
                expected_generation=created.generation,
                owner="launcher",
                now=self.now,
                ttl=timedelta(minutes=6),
            )

    def test_gcs_adapter_uses_create_replace_and_read_generation_preconditions(self):
        class FakePreconditionFailed(Exception):
            pass

        class FakeNotFound(Exception):
            pass

        class Blob:
            generation = None

            def __init__(self):
                self.calls = []

            def upload_from_string(self, content, **kwargs):
                self.calls.append(("upload", content, kwargs))
                self.generation = 17

            def reload(self):
                self.generation = 23

            def download_as_bytes(self, **kwargs):
                self.calls.append(("download", kwargs))
                return b"{}"

        class Bucket:
            def __init__(self):
                self.value = Blob()

            def blob(self, name):
                self.name = name
                return self.value

        bucket = Bucket()
        adapter = GcsObjectStore.__new__(GcsObjectStore)
        adapter._bucket = bucket
        adapter._not_found = FakeNotFound
        adapter._precondition_failed = FakePreconditionFailed

        self.assertEqual(adapter.create("runs/x/manifest.json", b"{}"), 17)
        _, _, create_kwargs = bucket.value.calls[-1]
        self.assertEqual(create_kwargs["if_generation_match"], 0)
        self.assertEqual(create_kwargs["content_type"], "application/json")

        self.assertEqual(
            adapter.replace("runs/x/manifest.json", b"{}", generation=17), 17
        )
        _, _, replace_kwargs = bucket.value.calls[-1]
        self.assertEqual(replace_kwargs["if_generation_match"], 17)

        self.assertEqual(adapter.read("runs/x/manifest.json"), (b"{}", 23))
        _, download_kwargs = bucket.value.calls[-1]
        self.assertEqual(download_kwargs["if_generation_match"], 23)

    def test_gcs_client_verifies_same_explicit_google_user_before_each_request(self):
        class FakeCredentialsBase:
            def __init__(self):
                self.token = None

            def apply(self, headers):
                headers["Authorization"] = f"Bearer {self.token}"

        class FakeTokenSource:
            def access_token(self):
                return "short-lived-test-token"

        class FakeBucket:
            pass

        class FakeClient:
            def __init__(self, *, credentials):
                self.credentials = credentials

            def bucket(self, bucket_name):
                self.bucket_name = bucket_name
                return FakeBucket()

        client_holder = {}

        def client_factory(*, credentials):
            client_holder["client"] = FakeClient(credentials=credentials)
            return client_holder["client"]

        google = types.ModuleType("google")
        google.__path__ = []
        auth = types.ModuleType("google.auth")
        auth.__path__ = []
        auth_credentials = types.ModuleType("google.auth.credentials")
        auth_credentials.Credentials = FakeCredentialsBase
        api_core = types.ModuleType("google.api_core")
        api_core.__path__ = []
        exceptions = types.ModuleType("google.api_core.exceptions")
        exceptions.NotFound = type("NotFound", (Exception,), {})
        exceptions.PreconditionFailed = type("PreconditionFailed", (Exception,), {})
        cloud = types.ModuleType("google.cloud")
        cloud.__path__ = []
        storage = types.ModuleType("google.cloud.storage")
        storage.Client = client_factory
        cloud.storage = storage
        modules = {
            "google": google,
            "google.auth": auth,
            "google.auth.credentials": auth_credentials,
            "google.api_core": api_core,
            "google.api_core.exceptions": exceptions,
            "google.cloud": cloud,
            "google.cloud.storage": storage,
        }
        identity = FakeIdentityTransport()
        with patch.dict(sys.modules, modules):
            adapter = GcsObjectStore(
                "private-ledger-bucket",
                token_source=FakeTokenSource(),
                identity_transport=identity,
                expected_email="project-reclaimer@example.com",
                expected_auditor_id="google-user-12345678901234567890",
            )
            self.assertIsInstance(adapter._bucket, FakeBucket)
            self.assertEqual(client_holder["client"].bucket_name, "private-ledger-bucket")
            credentials = client_holder["client"].credentials
            headers = {}
            credentials.before_request(
                None, "POST", "https://storage.googleapis.com/", headers
            )
            self.assertEqual(
                headers["Authorization"], "Bearer short-lived-test-token"
            )
            self.assertEqual(len(identity.calls), 1)
            self.assertEqual(
                identity.calls[0][0],
                "https://openidconnect.googleapis.com/v1/userinfo",
            )

            identity.email = "someone-else@example.com"
            headers = {}
            with self.assertRaises(IdentityVerificationError):
                credentials.before_request(
                    None, "POST", "https://storage.googleapis.com/", headers
                )
            self.assertNotIn("Authorization", headers)
            self.assertEqual(len(identity.calls), 2)

            identity.email = "project-reclaimer@example.com"
            wrong_subject_adapter = GcsObjectStore(
                "private-ledger-bucket",
                token_source=FakeTokenSource(),
                identity_transport=identity,
                expected_email="project-reclaimer@example.com",
                expected_auditor_id="google-user-another-subject",
            )
            self.assertIsInstance(wrong_subject_adapter._bucket, FakeBucket)
            headers = {}
            with self.assertRaisesRegex(IdentityVerificationError, "subject"):
                client_holder["client"].credentials.before_request(
                    None, "POST", "https://storage.googleapis.com/", headers
                )


if __name__ == "__main__":
    unittest.main()
