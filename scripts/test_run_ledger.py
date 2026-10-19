"""Fake-provider tests for the generation-conditional GCP run ledger."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from run_contract import ContractError
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


if __name__ == "__main__":
    unittest.main()
