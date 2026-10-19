"""Fake-provider contracts for ledger-backed operation submission and resume."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from run_contract import ContractError
from run_ledger import LedgerConflict, RunLedger
from recovery_controller import ProviderReceipt, RecoveryController, request_fingerprint
from independent_auditor import register_discovered_workload_resources
from test_independent_auditor import report as normalized_report
from test_run_contract import RUN_ID
from test_run_ledger import FakeObjectStore, current_intent, prepare_control_bootstrap


class FakeOperationProvider:
    def __init__(self):
        self.resources = set()
        self.submit_calls = []
        self.lookups = []
        self.polls = []
        self.submit_receipt = ProviderReceipt("running", "a" * 64, self.operation_name())
        self.poll_receipt = ProviderReceipt("succeeded", "b" * 64, self.operation_name())
        self.lose_create_response = False
        self.lose_delete_response = False
        self.create_before_loss = True
        self.lookup_receipt = None

    @staticmethod
    def operation_name():
        return "projects/sample-lab-123/regions/europe-west2/operations/op-123"

    def submit(self, action, resource_type, resource_id, request):
        self.submit_calls.append((action, resource_type, resource_id, deepcopy(request)))
        if action == "create" and self.create_before_loss:
            self.resources.add(resource_id)
        elif action == "delete":
            self.resources.discard(resource_id)
        if (
            action == "create" and self.lose_create_response
        ) or (action == "delete" and self.lose_delete_response):
            raise ConnectionError("simulated lost provider response")
        return self.submit_receipt

    def inspect_resource(self, resource_type, resource_id):
        self.lookups.append((resource_type, resource_id))
        if self.lookup_receipt is not None:
            return self.lookup_receipt
        state = "present" if resource_id in self.resources else "absent"
        return ProviderReceipt(state, "c" * 64)

    def poll_operation(self, operation_name):
        self.polls.append(operation_name)
        return self.poll_receipt


class FakeInventoryCollector:
    def __init__(self, resources):
        self.resources = resources

    def collect(self, manifest, *, observed_at, expected_auditor_email):
        evidence = normalized_report(
            run=manifest,
            resources=self.resources,
            observed_at=observed_at,
        )
        evidence["auditor_email"] = expected_auditor_email
        for resource in self.resources:
            evidence["coverage"][resource["surface"]]["resource_count"] += 1
        return evidence


class RecoveryControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.store = FakeObjectStore()
        self.ledger = RunLedger(self.store, clock=lambda: self.now)
        self.provider = FakeOperationProvider()
        self.controller = RecoveryController(
            self.ledger, self.provider, clock=lambda: self.now + timedelta(seconds=20)
        )
        self.intent = current_intent()
        resource_id = (
            f"projects/{self.intent['scope']['project_id']}/global/networks/"
            f"{RUN_ID}-network"
        )
        created = self.ledger.create(self.intent)
        snapshot = self.ledger.acquire_lease(
            RUN_ID, expected_generation=created.generation, owner="launcher", now=self.now
        )
        states = [
            "prepared", "project_create_requested", "project_created",
            "billing_link_requested", "project_linked", "api_enablement", "provisioning",
        ]
        for index, state in enumerate(states, start=1):
            candidate = deepcopy(snapshot.manifest)
            candidate["state"] = state
            if state == "prepared":
                snapshot = prepare_control_bootstrap(self.ledger, snapshot, self.now)
                continue
            if state == "provisioning":
                candidate["resources"] = [
                    {
                        "type": "gcp.compute.network",
                        "id": resource_id,
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
                now=self.now + timedelta(seconds=index + 1),
            )
        self.resource_id = resource_id

    def execute(self, *, attempt=1):
        return self.controller.execute(
            RUN_ID,
            lease_owner="launcher",
            attempt=attempt,
            action="create",
            resource_type="gcp.compute.network",
            resource_id=self.resource_id,
            request={"name": self.resource_id.rsplit("/", 1)[-1]},
        )

    def test_async_operation_is_recorded_then_polled_to_terminal(self):
        submitted = self.execute()
        self.assertEqual(submitted.state, "running")
        self.assertEqual(
            [event["kind"] for event in submitted.snapshot.manifest["operations"]],
            ["intent", "progress"],
        )
        self.assertNotIn("request", submitted.snapshot.manifest["operations"][0])

        completed = self.execute()
        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(len(self.provider.submit_calls), 1)
        self.assertEqual(self.provider.polls, [FakeOperationProvider.operation_name()])
        self.assertEqual(
            [event["kind"] for event in completed.snapshot.manifest["operations"]],
            ["intent", "progress", "result"],
        )

        repeated = self.execute()
        self.assertEqual(repeated, completed)
        self.assertEqual(len(self.provider.polls), 1)

    def test_lost_create_response_reconciles_exact_id_without_resubmitting(self):
        self.provider.lose_create_response = True
        with self.assertRaises(ConnectionError):
            self.execute()

        reconciled = self.execute()
        self.assertEqual(reconciled.state, "reconciled_present")
        self.assertEqual(len(self.provider.submit_calls), 1)
        self.assertEqual(
            self.provider.lookups,
            [("gcp.compute.network", self.resource_id)],
        )

    def test_absent_pending_create_remains_unresolved_without_retrying_create(self):
        self.provider.create_before_loss = False
        self.provider.lose_create_response = True
        with self.assertRaises(ConnectionError):
            self.execute()

        unresolved = self.execute()
        self.assertEqual(unresolved.state, "unresolved")
        self.assertEqual(len(self.provider.submit_calls), 1)
        self.assertFalse(
            any(
                event["kind"] == "result"
                for event in unresolved.snapshot.manifest["operations"]
            )
        )

    def test_operation_for_a_different_run_project_is_rejected_before_provider_call(self):
        foreign_id = self.resource_id.replace(
            self.intent["scope"]["project_id"], "another-lab-456", 1
        )
        with self.assertRaisesRegex(ContractError, "exact run inventory"):
            self.controller.execute(
                RUN_ID,
                lease_owner="launcher",
                attempt=1,
                action="create",
                resource_type="gcp.compute.network",
                resource_id=foreign_id,
                request={"name": foreign_id.rsplit("/", 1)[-1]},
            )
        self.assertEqual(self.provider.submit_calls, [])
        self.assertEqual(self.provider.lookups, [])

    def test_cleanup_refuses_provider_calls_without_a_fresh_discovery_receipt(self):
        current = self.ledger.read(RUN_ID)
        candidate = deepcopy(current.manifest)
        candidate["state"] = "teardown_requested"
        current = self.ledger.replace(
            RUN_ID,
            expected_generation=current.generation,
            lease_owner="launcher",
            manifest=candidate,
            now=self.now + timedelta(seconds=30),
        )
        self.controller.clock = lambda: self.now + timedelta(seconds=31)
        released = self.controller.begin_cleanup(
            RUN_ID, lease_owner="launcher"
        )
        leased = self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=released.generation,
            owner="launcher",
            now=self.now + timedelta(seconds=31),
        )
        with self.assertRaisesRegex(ContractError, "fresh independent inventory"):
            self.controller.cleanup(RUN_ID, lease_owner="launcher")
        self.assertEqual(self.provider.submit_calls, [])
        self.assertEqual(self.provider.lookups, [])

    def test_cleanup_is_idempotent_after_provider_confirms_delete(self):
        current = self.ledger.read(RUN_ID)
        candidate = deepcopy(current.manifest)
        candidate["state"] = "teardown_requested"
        self.provider.resources.add(self.resource_id)
        self.provider.submit_receipt = ProviderReceipt(
            "succeeded", "d" * 64
        )
        self.ledger.replace(
            RUN_ID,
            expected_generation=current.generation,
            lease_owner="launcher",
            manifest=candidate,
            now=self.now + timedelta(seconds=30),
        )
        self._prepare_independent_cleanup(observed=True, now_offset=31)

        snapshot, executions = self.controller.cleanup(
            RUN_ID, lease_owner="launcher"
        )
        self.assertEqual([item.state for item in executions], ["succeeded"])
        self.assertEqual(snapshot.manifest["resources"][0]["status"], "deleted")
        self.assertNotIn(self.resource_id, self.provider.resources)
        self.assertEqual(len(self.provider.submit_calls), 1)

        self._refresh_discovery(observed=False, now_offset=50)
        before_repeat = self.ledger.read(RUN_ID)
        repeated_snapshot, repeated_executions = self.controller.cleanup(
            RUN_ID, lease_owner="launcher"
        )
        self.assertEqual(repeated_snapshot, before_repeat)
        self.assertEqual(repeated_executions, [])
        self.assertEqual(len(self.provider.submit_calls), 1)

    def test_lost_delete_response_reconciles_absence_without_resubmitting(self):
        current = self.ledger.read(RUN_ID)
        candidate = deepcopy(current.manifest)
        candidate["state"] = "teardown_requested"
        self.provider.resources.add(self.resource_id)
        self.provider.lose_delete_response = True
        self.ledger.replace(
            RUN_ID,
            expected_generation=current.generation,
            lease_owner="launcher",
            manifest=candidate,
            now=self.now + timedelta(seconds=30),
        )
        self._prepare_independent_cleanup(observed=True, now_offset=31)

        with self.assertRaisesRegex(ConnectionError, "lost provider response"):
            self.controller.cleanup(RUN_ID, lease_owner="launcher")
        self.assertNotIn(self.resource_id, self.provider.resources)
        self.assertEqual(len(self.provider.submit_calls), 1)

        self.ledger.release_lease(
            RUN_ID,
            expected_generation=self.ledger.read(RUN_ID).generation,
            owner="launcher",
            now=self.now + timedelta(seconds=45),
        )
        self._refresh_discovery(observed=False, now_offset=46)
        snapshot, executions = self.controller.cleanup(
            RUN_ID, lease_owner="launcher"
        )
        self.assertEqual([item.state for item in executions], ["reconciled_absent"])
        self.assertEqual(snapshot.manifest["resources"][0]["status"], "deleted")
        self.assertEqual(self.provider.lookups, [
            ("gcp.compute.network", self.resource_id)
        ])
        self.assertEqual(len(self.provider.submit_calls), 1)

    def _prepare_independent_cleanup(self, *, observed, now_offset):
        self.controller.clock = lambda: self.now + timedelta(seconds=now_offset)
        self.controller.begin_cleanup(RUN_ID, lease_owner="launcher")
        resources = ([{
            "surface": "compute.networks",
            "project_id": self.intent["scope"]["project_id"],
            "id": self.resource_id,
        }] if observed else [])
        self._register_inventory(resources, now_offset + 1)
        self._acquire_reaper(now_offset + 2)

    def _refresh_discovery(self, *, observed, now_offset):
        current = self.ledger.read(RUN_ID)
        owner = current.metadata["lease"]["owner"]
        if owner is not None:
            self.ledger.release_lease(
                RUN_ID,
                expected_generation=current.generation,
                owner=owner,
                now=self.now + timedelta(seconds=now_offset),
            )
        resources = ([{
            "surface": "compute.networks",
            "project_id": self.intent["scope"]["project_id"],
            "id": self.resource_id,
        }] if observed else [])
        self._register_inventory(resources, now_offset)
        self._acquire_reaper(now_offset + 1)

    def _register_inventory(self, resources, now_offset):
        register_discovered_workload_resources(
            self.ledger,
            RUN_ID,
            FakeInventoryCollector(resources),
            expected_auditor_email="independent-reader@example.com",
            now=self.now + timedelta(seconds=now_offset),
        )

    def _acquire_reaper(self, now_offset):
        current = self.ledger.read(RUN_ID)
        self.ledger.acquire_lease(
            RUN_ID,
            expected_generation=current.generation,
            owner="launcher",
            now=self.now + timedelta(seconds=now_offset),
        )
        self.controller.clock = lambda: self.now + timedelta(seconds=now_offset + 1)

    def test_provider_call_requires_a_fresh_active_lease_check(self):
        with patch.object(
            self.ledger,
            "assert_active_lease",
            side_effect=LedgerConflict("simulated lost lease"),
        ), self.assertRaises(LedgerConflict):
            self.execute()
        self.assertEqual(self.provider.submit_calls, [])

    def test_request_fingerprint_is_canonical_and_rejects_non_json_values(self):
        self.assertEqual(
            request_fingerprint({"b": 2, "a": 1}),
            request_fingerprint({"a": 1, "b": 2}),
        )
        with self.assertRaises(ContractError):
            request_fingerprint({"amount": float("nan")})
        with self.assertRaises(ContractError):
            request_fingerprint(["not", "an", "object"])


if __name__ == "__main__":
    unittest.main()
