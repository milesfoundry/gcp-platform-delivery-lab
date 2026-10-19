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
    RunLedger,
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
        original = self.ledger.create(self.intent)
        candidate = deepcopy(original.manifest)
        candidate["state"] = "prepared"
        for control in candidate["controls"]:
            control["status"] = "present"
        updated = self.ledger.replace(
            RUN_ID, expected_generation=original.generation, manifest=candidate
        )
        self.assertGreater(updated.generation, original.generation)
        self.assertEqual(updated.manifest["state"], "prepared")
        self.assertEqual(
            self.ledger.replace(
                RUN_ID, expected_generation=original.generation, manifest=candidate
            ),
            updated,
        )
        changed = deepcopy(candidate)
        changed["finance"]["monthly_alert_threshold_gbp"] = 71
        with self.assertRaises(GenerationConflict):
            self.ledger.replace(
                RUN_ID, expected_generation=original.generation, manifest=changed
            )

    def test_retry_after_committed_replace_response_loss_reconciles_exact_generation(self):
        original = self.ledger.create(self.intent)
        candidate = deepcopy(original.manifest)
        candidate["state"] = "prepared"
        for control in candidate["controls"]:
            control["status"] = "present"
        self.store.fail_after_replace = True
        with self.assertRaises(ConnectionError):
            self.ledger.replace(
                RUN_ID, expected_generation=original.generation, manifest=candidate
            )
        reconciled = self.ledger.replace(
            RUN_ID, expected_generation=original.generation, manifest=candidate
        )
        self.assertEqual(reconciled.manifest["state"], "prepared")
        self.assertGreater(reconciled.generation, original.generation)

    def test_provider_side_race_is_rejected_by_storage_precondition(self):
        original = self.ledger.create(self.intent)
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
                RUN_ID, expected_generation=original.generation, manifest=candidate
            )
        self.assertEqual(self.ledger.read(RUN_ID).manifest["state"], "control_bootstrapping")

    def test_replacement_cannot_change_identity_or_skip_a_state(self):
        original = self.ledger.create(self.intent)
        changed = deepcopy(original.manifest)
        changed["scope"]["project_id"] = "other-lab-123"
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID, expected_generation=original.generation, manifest=changed
            )

        skipped = deepcopy(original.manifest)
        skipped["state"] = "ready"
        with self.assertRaises(LedgerConflict):
            self.ledger.replace(
                RUN_ID, expected_generation=original.generation, manifest=skipped
            )

    def test_corrupt_and_duplicate_key_objects_fail_closed(self):
        with self.assertRaises(LedgerError):
            from run_ledger import _decode_manifest

            _decode_manifest(b'{"state":"ready","state":"closed"}')

        self.store.objects[object_name(RUN_ID)] = (b"not json", 1)
        with self.assertRaises(LedgerError):
            self.ledger.read(RUN_ID)

    def test_ledger_has_no_bucket_list_or_delete_operation(self):
        self.assertFalse(hasattr(self.store, "list"))
        self.assertFalse(hasattr(self.store, "delete"))

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
